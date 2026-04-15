#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
trade_journal.py — EOD live trade capture + oracle scorecard
=============================================================
Runs at 3:30 PM IST (10:00 AM UTC) — 15 min after exit_positions.py.

What it does:
  1. Reads data/today_trade.json  — oracle intent (signal, strike, oracle premium, SL/TP)
  2. Reads Dhan tradebook         — actual BUY and SELL fills for today
  3. Computes slippage, actual P&L, exit reason (SL / TP / EOD / TRAIL)
  4. Appends one row to data/live_trades.csv  (VM-only, gitignored)
  5. Sends Telegram EOD journal with: oracle vs actual, slippage %, outcome

Over time live_trades.csv feeds into model_evolver.py which reports
live oracle accuracy vs backtest accuracy — the true feedback loop.

Cron (3:30 PM IST = 10:00 AM UTC):
  0 10 * * 1-5 cd ~/dhan-trading && python3 trade_journal.py >> logs/journal.log 2>&1
"""
import os
import sys
import csv
import json
import requests
from datetime import date, datetime, timezone, timedelta
from dotenv import load_dotenv

import notify
from backtest_engine import get_lot_size

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")
DRY_RUN   = "--dry-run" in sys.argv

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR        = "data"
INTENT_FILE     = f"{DATA_DIR}/today_trade.json"
JOURNAL_CSV     = f"{DATA_DIR}/live_trades.csv"

_IST = timezone(timedelta(hours=5, minutes=30))

# Time of day thresholds (IST hour, minute)
EOD_SQUAREOFF_HOUR = 15
EOD_SQUAREOFF_MIN  = 10   # anything >= 3:10 PM = EOD squareoff

JOURNAL_FIELDS = [
    "date", "signal", "strike", "lots", "dte", "spot_at_signal",
    "oracle_premium", "actual_entry", "entry_slippage_pct",
    "sl_price", "tp_price",
    "actual_exit", "exit_reason", "actual_pnl", "actual_pnl_pct",
    "oracle_correct",
    "signal_score", "iv_at_entry",
]


# ─────────────────────────────────────────────────────────────────────────────

def _load_intent():
    """Load today's oracle intent from today_trade.json. Returns dict or None."""
    if not os.path.exists(INTENT_FILE):
        return None
    try:
        with open(INTENT_FILE) as f:
            d = json.load(f)
        if d.get("date") != date.today().isoformat():
            notify.log(f"today_trade.json is from {d.get('date')}, not today — skipping")
            return None
        return d
    except Exception as e:
        notify.log(f"Could not read today_trade.json: {e}")
        return None


def _get_tradebook():
    """Fetch today's tradebook from Dhan. Returns list of trades or []."""
    try:
        resp = requests.get("https://api.dhan.co/v2/tradebook",
                            headers=HEADERS, timeout=10)
    except Exception as e:
        notify.log(f"Tradebook API unreachable: {e}")
        return []

    if resp.status_code == 401:
        notify.log("Tradebook API: token expired (401)")
        return []
    if resp.status_code != 200:
        notify.log(f"Tradebook API: HTTP {resp.status_code} — {resp.text[:120]}")
        return []

    data = resp.json()
    trades = data if isinstance(data, list) else data.get("data", [])
    return [
        t for t in trades
        if "BANKNIFTY" in str(t.get("tradingSymbol", t.get("securityId", ""))).upper()
        and t.get("exchangeSegment", "") == "NSE_FNO"
    ]


def _parse_fills(trades):
    """
    Extract best BUY fill and best SELL fill from trade list.
    Returns (buy_price, buy_qty, sell_price, sell_qty, sell_time_ist).
    """
    buys  = [t for t in trades if str(t.get("transactionType", "")).upper() == "BUY"]
    sells = [t for t in trades if str(t.get("transactionType", "")).upper() == "SELL"]

    def _wavg(fills):
        """Quantity-weighted average price."""
        total_qty = sum(float(f.get("tradedQuantity", 0)) for f in fills)
        if total_qty == 0:
            return 0.0, 0
        price = sum(float(f.get("tradedPrice", 0)) * float(f.get("tradedQuantity", 0))
                    for f in fills) / total_qty
        return round(price, 2), int(total_qty)

    buy_price, buy_qty   = _wavg(buys)
    sell_price, sell_qty = _wavg(sells)

    # Parse sell time to detect EOD squareoff
    sell_time = None
    if sells:
        # Dhan tradebook uses updateTime or createTime — pick latest
        raw_time = sells[-1].get("updateTime") or sells[-1].get("createTime") or ""
        try:
            # Format: "2026-04-13 15:16:45" or ISO
            dt = datetime.fromisoformat(raw_time.replace(" ", "T"))
            # Assume IST if no tzinfo
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IST)
            sell_time = dt.astimezone(_IST)
        except Exception:
            pass

    return buy_price, buy_qty, sell_price, sell_qty, sell_time


def _infer_exit_reason(sell_price, sell_time, sl_price, tp_price):
    """
    Infer exit reason from exit price and time.
    Tolerance: ±2% of SL/TP level to account for slippage at trigger.
    """
    if sell_price <= 0:
        return "UNKNOWN"

    sl_band = sl_price * 0.02
    tp_band = tp_price * 0.02

    if abs(sell_price - sl_price) <= sl_band:
        return "SL"
    if abs(sell_price - tp_price) <= tp_band:
        return "TP"

    if sell_time:
        h, m = sell_time.hour, sell_time.minute
        if h > EOD_SQUAREOFF_HOUR or (h == EOD_SQUAREOFF_HOUR and m >= EOD_SQUAREOFF_MIN):
            return "EOD"

    # Between SL and TP, during session → trail stop
    if sl_price < sell_price < tp_price:
        return "TRAIL"

    return "UNKNOWN"


def _oracle_correct(signal, exit_reason, actual_entry, actual_exit):
    """
    Oracle was correct if:
    - TP hit (direction was right, reached target)
    - TRAIL hit with profit (locked in gains)
    - SL hit = oracle was wrong
    - EOD squareoff = mark as profit/loss based on P&L
    """
    if exit_reason == "TP":
        return True
    if exit_reason == "SL":
        return False
    if exit_reason in ("TRAIL", "EOD"):
        return actual_exit > actual_entry   # profitable exit = directionally correct
    return None   # UNKNOWN — don't count


def _append_to_csv(row: dict):
    """Append one row to live_trades.csv, creating with header if needed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(JOURNAL_CSV)
    with open(JOURNAL_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _journal_stats():
    """Return (total_trades, oracle_wins, avg_slippage_pct) from live_trades.csv."""
    if not os.path.exists(JOURNAL_CSV):
        return 0, 0, 0.0
    try:
        with open(JOURNAL_CSV) as f:
            rows = list(csv.DictReader(f))
        total  = len(rows)
        wins   = sum(1 for r in rows if str(r.get("oracle_correct", "")).lower() == "true")
        slips  = [float(r["entry_slippage_pct"]) for r in rows
                  if r.get("entry_slippage_pct") not in ("", None)]
        avg_slip = round(sum(slips) / len(slips), 2) if slips else 0.0
        return total, wins, avg_slip
    except Exception:
        return 0, 0, 0.0


# ─────────────────────────────────────────────────────────────────────────────

def main():
    today_label = date.today().strftime("%d %b %Y")
    notify.log(f"Trade journal — {today_label}")

    # 1. Load oracle intent
    intent = _load_intent()
    if intent is None:
        notify.log("No today_trade.json found — no trade was placed today or file is missing. Nothing to journal.")
        return

    signal         = intent["signal"]
    strike         = float(intent["strike"])
    lots           = int(intent["lots"])
    dte            = float(intent["dte"])
    spot           = float(intent["spot_at_signal"])
    oracle_premium = float(intent["oracle_premium"])
    sl_price       = float(intent["sl_price"])
    tp_price       = float(intent["tp_price"])
    score          = int(intent.get("signal_score", 0))
    iv             = float(intent.get("iv_at_entry", 0.0))

    lot_size = get_lot_size(date.today())

    # 2. Fetch tradebook
    if DRY_RUN:
        notify.log("DRY RUN — skipping tradebook fetch, using oracle premium as placeholder")
        buy_price, buy_qty   = oracle_premium, lots * lot_size
        sell_price, sell_qty = oracle_premium, lots * lot_size
        sell_time            = None
    else:
        trades = _get_tradebook()
        if not trades:
            notify.log("No BANKNIFTY trades found in tradebook — position may have been placed as AMO or API issue.")
            return
        buy_price, buy_qty, sell_price, sell_qty, sell_time = _parse_fills(trades)

    # 3. Compute metrics
    if buy_price <= 0:
        notify.log(f"Could not determine entry price from tradebook (buy_price={buy_price}). Skipping.")
        return

    if oracle_premium <= 0:
        notify.log(f"oracle_premium is {oracle_premium} in today_trade.json — skipping slippage calc.")
        return

    entry_slippage_pct = round((buy_price - oracle_premium) / oracle_premium * 100, 2)

    actual_pnl  = 0.0
    actual_pnl_pct = 0.0
    exit_reason = "OPEN"   # no sell yet
    correct     = None

    if sell_price > 0 and sell_qty > 0:
        actual_pnl     = round((sell_price - buy_price) * sell_qty, 2)
        actual_pnl_pct = round((sell_price - buy_price) / buy_price * 100, 2)
        exit_reason    = _infer_exit_reason(sell_price, sell_time, sl_price, tp_price)
        correct        = _oracle_correct(signal, exit_reason, buy_price, sell_price)

    # 4. Append to CSV
    row = {
        "date":               date.today().isoformat(),
        "signal":             signal,
        "strike":             strike,
        "lots":               lots,
        "dte":                dte,
        "spot_at_signal":     spot,
        "oracle_premium":     oracle_premium,
        "actual_entry":       buy_price,
        "entry_slippage_pct": entry_slippage_pct,
        "sl_price":           sl_price,
        "tp_price":           tp_price,
        "actual_exit":        sell_price if sell_price > 0 else "",
        "exit_reason":        exit_reason,
        "actual_pnl":         actual_pnl if sell_price > 0 else "",
        "actual_pnl_pct":     actual_pnl_pct if sell_price > 0 else "",
        "oracle_correct":     correct if correct is not None else "",
        "signal_score":       score,
        "iv_at_entry":        iv,
    }
    _append_to_csv(row)
    notify.log(f"Appended to live_trades.csv: {signal} | entry ₹{buy_price} | exit ₹{sell_price or 'open'} | {exit_reason} | P&L ₹{actual_pnl:,.0f}")

    # 5. Running stats
    total_trades, oracle_wins, avg_slip = _journal_stats()
    live_wr = round(oracle_wins / total_trades * 100) if total_trades > 0 else 0

    # 6. Send Telegram report
    slip_sign  = "+" if entry_slippage_pct >= 0 else ""
    pnl_sign   = "+" if actual_pnl >= 0 else ""
    exit_emoji = {"TP": "✅", "SL": "❌", "TRAIL": "🔒", "EOD": "⏹", "OPEN": "🔓", "UNKNOWN": "❓"}.get(exit_reason, "❓")
    correct_str = "Oracle ✓ (right direction)" if correct is True else \
                  "Oracle ✗ (wrong direction)" if correct is False else \
                  "Oracle ? (inconclusive)"

    lines = [
        f"📓  <b>Trade Journal  ·  {today_label}</b>",
        "─────────────────────",
        f"Signal    <b>{signal}</b>  (score {score:+d})  ·  Strike {int(strike)}  ·  DTE {dte:.1f}",
        "",
        f"Oracle premium  ₹{oracle_premium:.0f}",
        f"Actual entry    ₹{buy_price:.2f}   slippage {slip_sign}{entry_slippage_pct:.1f}%",
    ]
    if sell_price > 0:
        lines += [
            f"Actual exit     ₹{sell_price:.2f}   {exit_emoji} {exit_reason}",
            f"P&amp;L           <b>{pnl_sign}₹{actual_pnl:,.0f}</b>  ({pnl_sign}{actual_pnl_pct:.1f}%)",
            "",
            correct_str,
        ]
    else:
        lines.append("Exit: position still open (no sell in tradebook)")

    if total_trades >= 3:
        lines += [
            "",
            "─────────────────────",
            f"Live oracle accuracy  {live_wr}%  ({oracle_wins}/{total_trades} trades)",
            f"Avg entry slippage    {avg_slip:+.1f}%",
        ]

    notify.send("\n".join(lines))


if __name__ == "__main__":
    main()
