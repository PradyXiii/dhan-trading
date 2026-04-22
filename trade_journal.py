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
SPREAD_CSV      = f"{DATA_DIR}/live_spread_trades.csv"

SPREAD_STRATEGIES   = {"bear_call_credit", "bull_put_credit"}
IC_STRATEGIES       = {"nf_iron_condor"}
STRADDLE_STRATEGIES = {"nf_short_straddle"}

IC_CSV    = f"{DATA_DIR}/live_ic_trades.csv"
IC_FIELDS = [
    "date", "strategy", "signal",
    "ce_short_strike", "ce_long_strike",
    "pe_short_strike", "pe_long_strike",
    "spread_width", "lots", "lot_size", "dte", "spot_at_signal",
    "ce_short_entry", "ce_long_entry", "ce_net_credit",
    "pe_short_entry", "pe_long_entry", "pe_net_credit",
    "net_credit",
    "exit_reason", "exit_time",
    "pnl_inr", "pnl_pct_of_credit",
    "oracle_correct", "signal_score", "ml_conf",
]

STRADDLE_CSV    = f"{DATA_DIR}/live_straddle_trades.csv"
STRADDLE_FIELDS = [
    "date", "strategy", "signal",
    "atm_strike", "lots", "lot_size", "dte", "spot_at_signal",
    "ce_entry", "pe_entry", "net_credit",
    "exit_cost", "exit_reason", "exit_time",
    "pnl_inr", "pnl_pct_of_credit",
    "oracle_correct", "signal_score", "ml_conf",
]

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

SPREAD_FIELDS = [
    "date", "strategy", "signal",
    "short_strike", "long_strike", "spread_width",
    "lots", "lot_size", "dte", "spot_at_signal",
    "short_entry", "long_entry", "net_credit",
    "short_exit", "long_exit", "exit_spread",
    "exit_reason", "exit_time",
    "pnl_inr", "pnl_pct_of_credit",
    "oracle_correct", "signal_score", "ml_conf",
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
        resp = requests.get("https://api.dhan.co/v2/trades",
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
    def _is_nf_or_bnf(t):
        sym = str(t.get("tradingSymbol", t.get("securityId", ""))).upper()
        return ("NIFTY" in sym) and t.get("exchangeSegment", "") == "NSE_FNO"
    return [t for t in trades if _is_nf_or_bnf(t)]


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

def _append_spread_row(row: dict):
    """Append one row to live_spread_trades.csv."""
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(SPREAD_CSV)
    with open(SPREAD_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SPREAD_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _journal_ic(intent: dict, today_label: str):
    """Journal a Nifty50 Iron Condor trade (4-leg IC schema)."""
    paper         = (intent.get("order_mode") == "PAPER" or intent.get("mode") == "PAPER")
    signal        = intent["signal"]
    lots          = int(intent.get("lots", 1))
    lot_size      = int(intent.get("lot_size", 65))
    dte           = float(intent.get("dte", 0))
    spot          = float(intent.get("spot_at_signal", 0))
    net_credit    = float(intent.get("net_credit", 0))
    ce_short_strike = float(intent.get("ce_short_strike", 0))
    ce_long_strike  = float(intent.get("ce_long_strike",  0))
    pe_short_strike = float(intent.get("pe_short_strike", 0))
    pe_long_strike  = float(intent.get("pe_long_strike",  0))
    ce_short_entry  = float(intent.get("ce_short_entry",  0))
    ce_long_entry   = float(intent.get("ce_long_entry",   0))
    pe_short_entry  = float(intent.get("pe_short_entry",  0))
    pe_long_entry   = float(intent.get("pe_long_entry",   0))
    ce_net_credit   = float(intent.get("ce_net_credit",   0))
    pe_net_credit   = float(intent.get("pe_net_credit",   0))
    score         = int(intent.get("signal_score", 0))
    ml_conf       = float(intent.get("ml_conf", 0))

    exit_reason   = intent.get("exit_reason", "") or "OPEN"
    exit_time     = intent.get("exit_time", "")
    pnl_inr       = float(intent.get("pnl_inr", 0))

    pnl_pct = (pnl_inr / (net_credit * lots * lot_size) * 100) if net_credit > 0 else 0

    oracle_correct = None
    if exit_reason == "TP":
        oracle_correct = True
    elif exit_reason == "SL":
        oracle_correct = False
    elif exit_reason in ("EOD", "OPEN"):
        oracle_correct = pnl_inr > 0

    row = {
        "date":              date.today().isoformat(),
        "strategy":          "nf_iron_condor",
        "signal":            signal,
        "ce_short_strike":   ce_short_strike,
        "ce_long_strike":    ce_long_strike,
        "pe_short_strike":   pe_short_strike,
        "pe_long_strike":    pe_long_strike,
        "spread_width":      150,
        "lots":              lots,
        "lot_size":          lot_size,
        "dte":               dte,
        "spot_at_signal":    spot,
        "ce_short_entry":    ce_short_entry,
        "ce_long_entry":     ce_long_entry,
        "ce_net_credit":     ce_net_credit,
        "pe_short_entry":    pe_short_entry,
        "pe_long_entry":     pe_long_entry,
        "pe_net_credit":     pe_net_credit,
        "net_credit":        net_credit,
        "exit_reason":       exit_reason,
        "exit_time":         exit_time,
        "pnl_inr":           pnl_inr,
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    oracle_correct if oracle_correct is not None else "",
        "signal_score":      score,
        "ml_conf":           round(ml_conf, 4),
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    if DRY_RUN:
        notify.log(f"IC journal: credit ₹{net_credit:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f} [DRY RUN — CSV not written]")
    else:
        file_exists = os.path.exists(IC_CSV)
        with open(IC_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=IC_FIELDS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        notify.log(f"IC journal: credit ₹{net_credit:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f}")

    emoji    = {"TP": "🟢", "SL": "🔴", "EOD": "⏹", "OPEN": "🔓"}.get(exit_reason, "❓")
    pnl_sign = "+" if pnl_inr >= 0 else ""
    mode_tag = "[PAPER] " if paper else ""

    lines = [
        f"📓 <b>{mode_tag}IC Journal · {today_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Nifty50 Iron Condor  ·  {signal}  ·  {lots} lot",
        f"CE  SELL {int(ce_short_strike)} / BUY {int(ce_long_strike)}  → credit ₹{ce_net_credit:.0f}",
        f"PE  SELL {int(pe_short_strike)} / BUY {int(pe_long_strike)}  → credit ₹{pe_net_credit:.0f}",
        f"Total credit  ₹{net_credit:.0f} / lot",
        "",
        (f"Exit  {emoji} {exit_reason}" + (f"  at {exit_time} IST" if exit_time else ""))
        if exit_reason != "OPEN" else "Exit  🔓 position still open",
        "",
        f"<b>P&amp;L  {pnl_sign}₹{pnl_inr:,.0f}  ({pnl_sign}{pnl_pct:.0f}% of max credit)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    notify.send("\n".join(lines))


def _journal_spread(intent: dict, today_label: str):
    """
    Journal a credit-spread trade.
    For LIVE trades: fetch actual fills from tradebook (entry + exit).
    For PAPER trades: use fields already written by auto_trader + spread_monitor.
    """
    strategy = intent["strategy"]
    paper    = (intent.get("order_mode") == "PAPER" or
                intent.get("mode") == "PAPER")

    signal        = intent["signal"]
    short_strike  = float(intent["short_strike"])
    long_strike   = float(intent["long_strike"])
    lots          = int(intent["lots"])
    lot_size      = int(intent.get("lot_size", 65))
    dte           = float(intent.get("dte", 0))
    spot          = float(intent.get("spot_at_signal", 0))
    net_credit    = float(intent["net_credit"])
    short_entry   = float(intent.get("short_entry", 0))
    long_entry    = float(intent.get("long_entry", 0))
    score         = int(intent.get("signal_score", 0))
    ml_conf       = float(intent.get("ml_conf", 0))

    # Exit fields — written by spread_monitor.py OR exit_positions.py
    short_exit    = float(intent.get("exit_short_ltp", 0))
    long_exit     = float(intent.get("exit_long_ltp", 0))
    exit_spread   = float(intent.get("exit_spread", 0))
    exit_reason   = intent.get("exit_reason", "")
    exit_time     = intent.get("exit_time", "")
    pnl_inr       = float(intent.get("pnl_inr", 0))

    # If no exit recorded yet (trade still open at 3:30 PM journal run),
    # estimate: assume EOD squareoff close at last known spread cost.
    # Mark exit_reason=OPEN so it's visible in the Telegram report.
    if not exit_reason:
        exit_reason = "OPEN"

    pnl_pct = (pnl_inr / (net_credit * lots * lot_size) * 100) if net_credit > 0 else 0

    oracle_correct = None
    if exit_reason == "TP":
        oracle_correct = True
    elif exit_reason == "SL":
        oracle_correct = False
    elif exit_reason in ("EOD", "OPEN"):
        oracle_correct = pnl_inr > 0

    row = {
        "date":              date.today().isoformat(),
        "strategy":          strategy,
        "signal":            signal,
        "short_strike":      short_strike,
        "long_strike":       long_strike,
        "spread_width":      abs(long_strike - short_strike),
        "lots":              lots,
        "lot_size":          lot_size,
        "dte":               dte,
        "spot_at_signal":    spot,
        "short_entry":       short_entry,
        "long_entry":        long_entry,
        "net_credit":        net_credit,
        "short_exit":        short_exit,
        "long_exit":         long_exit,
        "exit_spread":       exit_spread,
        "exit_reason":       exit_reason,
        "exit_time":         exit_time,
        "pnl_inr":           pnl_inr,
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    oracle_correct if oracle_correct is not None else "",
        "signal_score":      score,
        "ml_conf":           round(ml_conf, 4),
    }
    if DRY_RUN:
        notify.log(f"Spread journal [DRY RUN — CSV not written]: {strategy} | credit ₹{net_credit:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f}")
    else:
        _append_spread_row(row)
        notify.log(
            f"Spread journal appended: {strategy} | credit ₹{net_credit:.0f} | "
            f"exit ₹{exit_spread:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f}"
        )

    # Telegram report
    strategy_name = ("Bear Call Spread" if strategy == "bear_call_credit"
                     else "Bull Put Spread")
    emoji = {"TP": "🟢", "SL": "🔴", "EOD": "⏹", "OPEN": "🔓"}.get(exit_reason, "❓")
    pnl_sign = "+" if pnl_inr >= 0 else ""
    mode_tag = "[PAPER] " if paper else ""

    lines = [
        f"📓 <b>{mode_tag}Spread Journal · {today_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Strategy      {strategy_name}",
        f"Legs          SELL {int(short_strike)} / BUY {int(long_strike)}  ({signal})",
        f"Lots          {lots}  ·  {lots * lot_size} shares",
        "",
        f"Entry credit  ₹{net_credit:.0f} / share",
    ]
    if exit_spread > 0:
        lines.append(f"Exit cost     ₹{exit_spread:.0f} / share   {emoji} {exit_reason}")
        if exit_time:
            lines.append(f"Exit time     {exit_time} IST")
    else:
        lines.append(f"Exit          {emoji} {exit_reason} — no exit data yet")
    lines += [
        "",
        f"<b>P&amp;L          {pnl_sign}₹{pnl_inr:,.0f}  ({pnl_sign}{pnl_pct:.0f}% of max credit)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    notify.send("\n".join(lines))


def _journal_straddle(intent: dict, today_label: str):
    """Journal a Short Straddle trade (2-leg: SELL CE + SELL PE, no wings)."""
    paper      = (intent.get("order_mode") == "PAPER" or intent.get("mode") == "PAPER")
    signal     = intent.get("signal", "CALL")
    atm_strike = float(intent.get("atm_strike", 0))
    lots       = int(intent.get("lots", 1))
    lot_size   = int(intent.get("lot_size", 65))
    dte        = float(intent.get("dte", 0))
    spot       = float(intent.get("spot_at_signal", 0))
    ce_entry   = float(intent.get("ce_entry", intent.get("ce_ltp", 0)))
    pe_entry   = float(intent.get("pe_entry", intent.get("pe_ltp", 0)))
    net_credit = float(intent.get("net_credit", ce_entry + pe_entry))
    score      = int(intent.get("signal_score", 0))
    ml_conf    = float(intent.get("ml_conf", 0))

    exit_cost   = float(intent.get("exit_cost", 0))
    exit_reason = intent.get("exit_reason", "") or "OPEN"
    exit_time   = intent.get("exit_time", "")
    pnl_inr     = float(intent.get("pnl_inr", 0))

    pnl_pct = (pnl_inr / (net_credit * lots * lot_size) * 100) if net_credit > 0 else 0

    oracle_correct = None
    if exit_reason == "SL":
        oracle_correct = False
    elif exit_reason in ("EOD", "OPEN"):
        oracle_correct = pnl_inr > 0

    row = {
        "date":              date.today().isoformat(),
        "strategy":          "nf_short_straddle",
        "signal":            signal,
        "atm_strike":        atm_strike,
        "lots":              lots,
        "lot_size":          lot_size,
        "dte":               dte,
        "spot_at_signal":    spot,
        "ce_entry":          ce_entry,
        "pe_entry":          pe_entry,
        "net_credit":        net_credit,
        "exit_cost":         exit_cost,
        "exit_reason":       exit_reason,
        "exit_time":         exit_time,
        "pnl_inr":           pnl_inr,
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    oracle_correct if oracle_correct is not None else "",
        "signal_score":      score,
        "ml_conf":           round(ml_conf, 4),
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    if DRY_RUN:
        notify.log(f"Straddle journal [DRY RUN — CSV not written]: credit ₹{net_credit:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f}")
    else:
        file_exists = os.path.exists(STRADDLE_CSV)
        with open(STRADDLE_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=STRADDLE_FIELDS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        notify.log(f"Straddle journal: credit ₹{net_credit:.0f} | {exit_reason} | P&L ₹{pnl_inr:,.0f}")

    emoji    = {"SL": "🔴", "EOD": "⏹", "OPEN": "🔓"}.get(exit_reason, "❓")
    pnl_sign = "+" if pnl_inr >= 0 else ""
    mode_tag = "[PAPER] " if paper else ""

    lines = [
        f"📓 <b>{mode_tag}Straddle Journal · {today_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Short Straddle  ·  {lots} lot",
        f"SELL {int(atm_strike)} CE @ ₹{ce_entry:.0f}  +  SELL {int(atm_strike)} PE @ ₹{pe_entry:.0f}",
        f"Total credit  ₹{net_credit:.0f} / share",
        "",
        (f"Exit  {emoji} {exit_reason}" + (f"  at {exit_time} IST" if exit_time else "")
         if exit_reason != "OPEN" else "Exit  🔓 position still open"),
    ]
    if exit_cost > 0:
        lines.append(f"Buyback cost  ₹{exit_cost:.0f} / share")
    lines += [
        "",
        f"<b>P&L  {pnl_sign}₹{pnl_inr:,.0f}  ({pnl_sign}{pnl_pct:.0f}% of max credit)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    notify.send("\n".join(lines))


def main():
    today_label = date.today().strftime("%d %b %Y")
    notify.log(f"Trade journal — {today_label}")

    # 1. Load oracle intent
    intent = _load_intent()
    if intent is None:
        notify.log("No today_trade.json found — no trade was placed today or file is missing. Nothing to journal.")
        return

    # Branch by strategy
    if intent.get("strategy") in IC_STRATEGIES:
        _journal_ic(intent, today_label)
        return
    if intent.get("strategy") in STRADDLE_STRATEGIES:
        _journal_straddle(intent, today_label)
        return
    if intent.get("strategy") in SPREAD_STRATEGIES:
        _journal_spread(intent, today_label)
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
            notify.log("No NIFTY trades found in tradebook — position may have been placed as AMO or API issue.")
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
