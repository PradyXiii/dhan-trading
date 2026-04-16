#!/usr/bin/env python3
"""
backfill_live_trades.py — One-time backfill of live_trades.csv from Dhan trade history.

Pulls all BANKNIFTY NSE_FNO trades for a date range, groups BUY+SELL fills by
trade date, and appends rows to data/live_trades.csv (skipping dates already present).

Fields not available from Dhan history are left blank:
  oracle_premium, sl_price*, tp_price*, spot_at_signal, signal_score, iv_at_entry
  (* sl/tp approximated from entry × SL_PCT / TP_PCT)

Usage:
    python3 backfill_live_trades.py                      # April 1 – today
    python3 backfill_live_trades.py --from 2026-03-01    # custom start date
    python3 backfill_live_trades.py --dry-run            # print rows, don't write
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
import requests

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")

_IST     = timezone(timedelta(hours=5, minutes=30))
_HERE    = Path(__file__).parent.resolve()
DATA_DIR = _HERE / "data"

JOURNAL_CSV = DATA_DIR / "live_trades.csv"
JOURNAL_FIELDS = [
    "date", "signal", "strike", "lots", "dte", "spot_at_signal",
    "oracle_premium", "actual_entry", "entry_slippage_pct",
    "sl_price", "tp_price",
    "actual_exit", "exit_reason", "actual_pnl", "actual_pnl_pct",
    "oracle_correct",
    "signal_score", "iv_at_entry",
]

# Must match auto_trader.py constants
SL_PCT  = 0.15
RR      = 2.5
TP_PCT  = SL_PCT * RR   # 0.375


# ── Dhan API ──────────────────────────────────────────────────────────────────

HEADERS = {
    "access-token": TOKEN,
    "Accept": "application/json",
}


def fetch_trade_history(from_date: str, to_date: str) -> list[dict]:
    """Fetch all pages of trade history between from_date and to_date."""
    all_trades = []
    page = 0
    while True:
        url = f"https://api.dhan.co/v2/trades/{from_date}/{to_date}/{page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
        except Exception as e:
            print(f"  API error on page {page}: {e}")
            break

        if r.status_code == 401:
            print("  401 Unauthorized — token expired. Run: python3 renew_token.py")
            sys.exit(1)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:200]}")
            break

        data = r.json()
        batch = data if isinstance(data, list) else data.get("data", [])
        if not batch:
            break

        all_trades.extend(batch)
        print(f"  Page {page}: {len(batch)} trades (total so far: {len(all_trades)})")

        # Dhan paginates in batches of 100
        if len(batch) < 100:
            break
        page += 1

    return all_trades


def fetch_todays_tradebook() -> list[dict]:
    """Fetch today's fills via GET /v2/trades (no date params = today only)."""
    try:
        r = requests.get("https://api.dhan.co/v2/trades", headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"  Tradebook API error: {e}")
        return []

    if r.status_code == 401:
        print("  401 Unauthorized — token expired. Run: python3 renew_token.py")
        sys.exit(1)
    if r.status_code != 200:
        print(f"  Tradebook HTTP {r.status_code}: {r.text[:200]}")
        return []

    data = r.json()
    trades = data if isinstance(data, list) else data.get("data", [])
    print(f"  Today's tradebook: {len(trades)} total fills")
    return trades


def filter_banknifty_fno(trades: list[dict]) -> list[dict]:
    """Keep only BANKNIFTY options in NSE_FNO segment."""
    out = []
    for t in trades:
        seg = str(t.get("exchangeSegment", ""))
        sym = str(t.get("tradingSymbol", t.get("customSymbol", ""))).upper()
        if seg == "NSE_FNO" and "BANKNIFTY" in sym:
            out.append(t)
    return out


# ── Fill parsing ──────────────────────────────────────────────────────────────

def _wavg(fills: list[dict]) -> tuple[float, int]:
    """Quantity-weighted average price and total quantity."""
    total_qty = sum(int(f.get("tradedQuantity", 0)) for f in fills)
    if total_qty == 0:
        return 0.0, 0
    price = sum(float(f.get("tradedPrice", 0)) * int(f.get("tradedQuantity", 0))
                for f in fills) / total_qty
    return round(price, 2), total_qty


def _parse_time(raw: str) -> datetime | None:
    """Parse Dhan time string to IST datetime."""
    if not raw or raw == "NA":
        return None
    try:
        raw = raw.strip().replace(" ", "T")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt.astimezone(_IST)
    except Exception:
        return None


def _trade_date(fills: list[dict]) -> date | None:
    """Extract trade date from fills (exchangeTime preferred, fall back to createTime)."""
    for f in fills:
        raw = f.get("exchangeTime") or f.get("createTime") or f.get("updateTime") or ""
        dt = _parse_time(raw)
        if dt:
            return dt.date()
    return None


def _infer_exit_reason(buy_price: float, sell_price: float, sell_time: datetime | None) -> str:
    """Infer SL / TP / EOD / TRAIL from exit price and time."""
    if sell_price <= 0:
        return "UNKNOWN"
    sl = buy_price * (1 - SL_PCT)
    tp = buy_price * (1 + TP_PCT)
    if abs(sell_price - sl) <= sl * 0.03:
        return "SL"
    if abs(sell_price - tp) <= tp * 0.03:
        return "TP"
    if sell_time:
        h, m = sell_time.hour, sell_time.minute
        if h > 15 or (h == 15 and m >= 10):
            return "EOD"
    if sl < sell_price < tp:
        return "TRAIL"
    # sell_price outside SL-TP range but no time — best guess
    if sell_price < buy_price:
        return "SL"
    return "TRAIL"


def _oracle_correct(exit_reason: str, buy_price: float, sell_price: float):
    if exit_reason == "TP":
        return True
    if exit_reason == "SL":
        return False
    if exit_reason in ("TRAIL", "EOD"):
        return sell_price > buy_price
    return None


# ── DTE helper ────────────────────────────────────────────────────────────────

def _dte(trade_dt: date, expiry_str: str) -> float:
    """Days to expiry from trade_date."""
    try:
        expiry = date.fromisoformat(str(expiry_str)[:10])
        return float((expiry - trade_dt).days)
    except Exception:
        return 0.0


def _lot_size(trade_dt: date) -> int:
    """BankNifty lot size for the given date (mirrors backtest_engine.get_lot_size)."""
    overrides_path = DATA_DIR / "lot_size_overrides.json"
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text())
            for entry in sorted(overrides, key=lambda x: x["from"], reverse=True):
                if str(trade_dt) >= entry["from"]:
                    return int(entry["lot_size"])
        except Exception:
            pass
    # Hardcoded timeline
    if trade_dt >= date(2026, 1, 1):
        return 30
    if trade_dt >= date(2025, 6, 1):
        return 35
    if trade_dt >= date(2024, 11, 1):
        return 30
    return 15


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _load_existing_dates() -> set[str]:
    """Return set of dates already in live_trades.csv."""
    if not JOURNAL_CSV.exists():
        return set()
    try:
        with open(JOURNAL_CSV) as f:
            return {row["date"] for row in csv.DictReader(f) if row.get("date")}
    except Exception:
        return set()


def _append_rows(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    header_needed = not JOURNAL_CSV.exists()
    with open(JOURNAL_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if header_needed:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ── Core logic ────────────────────────────────────────────────────────────────

def build_rows(trades: list[dict], existing_dates: set[str]) -> list[dict]:
    """
    Group fills by (date, option_type, strike) → one row per trading day.
    Handles multiple fills per side (partial fills, trail SL + EOD close).
    """
    # Group by (date_str, option_type)
    groups: dict[tuple, dict] = defaultdict(lambda: {"buys": [], "sells": [], "expiry": ""})

    for t in trades:
        opt_type = str(t.get("drvOptionType", "")).upper()  # CALL or PUT

        # Live tradebook may return null drvOptionType — infer from tradingSymbol
        if opt_type not in ("CALL", "PUT"):
            sym = str(t.get("tradingSymbol", t.get("customSymbol", ""))).upper()
            if sym.endswith("CE"):
                opt_type = "CALL"
            elif sym.endswith("PE"):
                opt_type = "PUT"
            else:
                continue  # can't determine option type

        tx = str(t.get("transactionType", "")).upper()
        strike = float(t.get("drvStrikePrice", 0))
        expiry = str(t.get("drvExpiryDate", ""))

        # Determine trade date from exchangeTime
        raw_time = t.get("exchangeTime") or t.get("createTime") or ""
        dt = _parse_time(raw_time)
        if not dt:
            continue
        trade_dt = dt.date()
        key = (str(trade_dt), opt_type, round(strike))

        if tx == "BUY":
            groups[key]["buys"].append(t)
        elif tx == "SELL":
            groups[key]["sells"].append(t)
        groups[key]["expiry"] = expiry

    rows = []
    for (date_str, opt_type, strike), data in sorted(groups.items()):
        if date_str in existing_dates:
            print(f"  SKIP  {date_str} {opt_type} {strike}  (already in live_trades.csv)")
            continue

        buys  = data["buys"]
        sells = data["sells"]

        if not buys:
            print(f"  SKIP  {date_str} {opt_type} {strike}  (no BUY fills)")
            continue

        buy_price, buy_qty = _wavg(buys)
        sell_price, sell_qty = _wavg(sells) if sells else (0.0, 0)

        trade_dt = date.fromisoformat(date_str)
        lot_sz   = _lot_size(trade_dt)
        lots     = round(buy_qty / lot_sz) if lot_sz > 0 else 0

        # Determine sell time for exit reason inference
        sell_time = None
        if sells:
            raw = sells[-1].get("exchangeTime") or sells[-1].get("updateTime") or ""
            sell_time = _parse_time(raw)

        sl_approx = round(buy_price * (1 - SL_PCT), 2) if buy_price > 0 else ""
        tp_approx = round(buy_price * (1 + TP_PCT), 2) if buy_price > 0 else ""

        if sell_price > 0:
            exit_reason = _infer_exit_reason(buy_price, sell_price, sell_time)
            actual_pnl     = round((sell_price - buy_price) * sell_qty, 2)
            actual_pnl_pct = round((sell_price - buy_price) / buy_price * 100, 2)
            correct        = _oracle_correct(exit_reason, buy_price, sell_price)
        else:
            exit_reason    = "OPEN"
            actual_pnl     = ""
            actual_pnl_pct = ""
            correct        = None

        dte = _dte(trade_dt, data["expiry"])

        row = {
            "date":               date_str,
            "signal":             opt_type,        # CALL or PUT
            "strike":             strike,
            "lots":               lots,
            "dte":                dte,
            "spot_at_signal":     "",              # not available from history
            "oracle_premium":     "",              # not available
            "actual_entry":       buy_price,
            "entry_slippage_pct": "",              # no oracle_premium to compare
            "sl_price":           sl_approx,
            "tp_price":           tp_approx,
            "actual_exit":        sell_price if sell_price > 0 else "",
            "exit_reason":        exit_reason,
            "actual_pnl":         actual_pnl,
            "actual_pnl_pct":     actual_pnl_pct,
            "oracle_correct":     correct if correct is not None else "",
            "signal_score":       "",
            "iv_at_entry":        "",
        }
        rows.append(row)
        print(f"  ADD   {date_str}  {opt_type}  strike={strike}  entry=₹{buy_price}  "
              f"exit=₹{sell_price or 'OPEN'}  {exit_reason}  "
              f"oracle_correct={correct}  pnl=₹{actual_pnl or '?'}")

    return rows


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2026-04-01",
                        help="Start date YYYY-MM-DD (default: 2026-04-01)")
    parser.add_argument("--to", dest="to_date",
                        default=date.today().isoformat(),
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print rows but do not write to CSV")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: DHAN_ACCESS_TOKEN not set in .env")
        sys.exit(1)

    # Historical trades (from_date → yesterday via dated API)
    today_str = date.today().isoformat()
    history_to = args.to_date if args.to_date < today_str else (
        date.today() - timedelta(days=1)
    ).isoformat()

    all_trades = []
    if args.from_date <= history_to:
        print(f"\nFetching Dhan trade history: {args.from_date} → {history_to}")
        all_trades = fetch_trade_history(args.from_date, history_to)
        print(f"History trades fetched: {len(all_trades)}")

    # Today's fills via tradebook (GET /v2/trades — no date params)
    if args.to_date >= today_str:
        print(f"\nFetching today's tradebook ({today_str})...")
        todays = fetch_todays_tradebook()
        all_trades.extend(todays)

    print(f"\nTotal trades: {len(all_trades)}")
    bn_trades = filter_banknifty_fno(all_trades)
    print(f"BANKNIFTY NSE_FNO trades: {len(bn_trades)}")

    if not bn_trades:
        print("No BANKNIFTY FNO trades found in this period.")
        return

    existing = _load_existing_dates()
    print(f"Dates already in live_trades.csv: {sorted(existing)}\n")

    rows = build_rows(bn_trades, existing)

    print(f"\n{'─'*55}")
    print(f"Rows to write: {len(rows)}")

    if not rows:
        print("Nothing new to add.")
        return

    if args.dry_run:
        print("\n[Dry-run] Not writing to CSV.")
        return

    _append_rows(rows)
    print(f"\n✓ Written {len(rows)} rows to {JOURNAL_CSV}")
    print(f"\nCurrent live_trades.csv contents:")
    with open(JOURNAL_CSV) as f:
        for line in f:
            print(" ", line.rstrip())


if __name__ == "__main__":
    main()
