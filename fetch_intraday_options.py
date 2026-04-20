#!/usr/bin/env python3
"""
Fetch 1-minute ATM option premiums for each historical CALL/PUT trade day.
Used by backtest_entry_time.py to simulate different entry times (9:15 vs 9:30 vs 10:00 etc.).

What we pull:
  Dhan /v2/charts/rollingoption, interval=1 (minute bars), strike="ATM",
  CE for CALL days, PE for PUT days. Each bar carries its own `strike`
  field so we can see when the ATM contract rolls to a new strike.

Caveat (important):
  "ATM" is RELATIVE — if spot moves enough, the ATM at 10:00 may be a
  different contract than the ATM at 9:15. For most days intraday moves
  stay within one strike, but on big-move days this understates the
  edge of earlier entry (the original contract went much higher than
  the rolling-ATM series shows). So: if rolling-ATM numbers already
  favour earlier entry, the real edge is at least that large.

Cache: data/intraday_options_cache/{YYYY-MM-DD}_{CE|PE}.csv  (gitignored)

Usage:
  python3 fetch_intraday_options.py                                # last 365 days
  python3 fetch_intraday_options.py --start 2024-01-01 --end 2025-04-01
  python3 fetch_intraday_options.py --dry-run                      # list days, no API
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID")

HEADERS = {
    "access-token":   TOKEN,
    "client-id":      CLIENT_ID,
    "Content-Type":   "application/json",
}

DATA_DIR        = "data"
CACHE_DIR       = os.path.join(DATA_DIR, "intraday_options_cache")
SIGNALS_PATH    = os.path.join(DATA_DIR, "signals_ml.csv")
WEEKLY_BOUNDARY = datetime(2024, 11, 20).date()   # WEEK → MONTH phase transition


def _cache_path(date_str: str, opt_code: str) -> str:
    return os.path.join(CACHE_DIR, f"{date_str}_{opt_code}.csv")


def _fetch_one_day(date_obj, api_opt_type: str) -> pd.DataFrame:
    """
    Fetch 1-minute ATM bars for ONE day, one option type (CALL or PUT).
    Uses expiryFlag=WEEK before Nov 20 2024, MONTH from that date on.
    Returns DataFrame: dt, open, high, low, close, volume, strike.
    """
    expiry_flag = "WEEK" if date_obj < WEEKLY_BOUNDARY else "MONTH"
    next_day    = date_obj + timedelta(days=1)

    payload = {
        "exchangeSegment": "NSE_FNO",
        "interval":        1,                        # 1-minute bars
        "securityId":      25,                       # BankNifty underlying
        "instrument":      "OPTIDX",
        "expiryFlag":      expiry_flag,
        "expiryCode":      1,                        # nearest expiry (0 is treated as missing)
        "strike":          "ATM",
        "drvOptionType":   api_opt_type,             # "CALL" or "PUT"
        "requiredData":    ["open", "high", "low", "close", "volume", "strike"],
        "fromDate":        date_obj.strftime("%Y-%m-%d"),
        "toDate":          next_day.strftime("%Y-%m-%d"),
    }

    resp = requests.post(
        "https://api.dhan.co/v2/charts/rollingoption",
        headers=HEADERS,
        json=payload,
        timeout=45,
    )
    if resp.status_code != 200:
        print(f"    HTTP {resp.status_code}: {resp.text[:200]}")
        return pd.DataFrame()

    d        = resp.json().get("data", {})
    opt_data = (d.get("ce") if api_opt_type == "CALL" else d.get("pe")) or {}
    if not opt_data or not opt_data.get("timestamp"):
        return pd.DataFrame()

    ts_ist = (pd.to_datetime(opt_data["timestamp"], unit="s")
              + pd.Timedelta(hours=5, minutes=30))
    df = pd.DataFrame({
        "dt":     ts_ist,
        "open":   opt_data["open"],
        "high":   opt_data["high"],
        "low":    opt_data["low"],
        "close":  opt_data["close"],
        "volume": opt_data.get("volume", [0] * len(ts_ist)),
        "strike": opt_data.get("strike", [None] * len(ts_ist)),
    })
    df = df[df["dt"].dt.date == date_obj]
    return df.reset_index(drop=True)


def fetch_all(start: str, end: str, dry_run: bool = False) -> None:
    """
    For each CALL/PUT day in signals_ml.csv between [start, end],
    cache its 1-min ATM CE (for CALL) or PE (for PUT) series.
    Idempotent — skips days whose cache file exists.
    """
    if not os.path.exists(SIGNALS_PATH):
        print(f"ERROR: {SIGNALS_PATH} missing — run `python3 ml_engine.py` first.")
        sys.exit(1)

    signals = pd.read_csv(SIGNALS_PATH, parse_dates=["date"])
    signals["date"] = signals["date"].dt.date
    lo    = datetime.strptime(start, "%Y-%m-%d").date()
    hi    = datetime.strptime(end,   "%Y-%m-%d").date()
    mask  = (signals["date"] >= lo) & (signals["date"] <= hi) \
            & (signals["signal"].isin(["CALL", "PUT"]))
    days  = signals[mask][["date", "signal"]].sort_values("date")

    os.makedirs(CACHE_DIR, exist_ok=True)

    to_fetch = []
    for _, r in days.iterrows():
        opt_code = "CE" if r["signal"] == "CALL" else "PE"
        path     = _cache_path(r["date"].strftime("%Y-%m-%d"), opt_code)
        if os.path.exists(path):
            continue
        to_fetch.append((r["date"], r["signal"], opt_code, path))

    print(f"Trade days in range: {len(days)}  (CALL/PUT)")
    print(f"Cache hits:          {len(days) - len(to_fetch)}")
    print(f"To fetch:            {len(to_fetch)} days")
    if dry_run:
        for d, sig, _, _ in to_fetch[:10]:
            print(f"  {d}  {sig}")
        if len(to_fetch) > 10:
            print(f"  ... and {len(to_fetch) - 10} more")
        return

    if not TOKEN:
        print("ERROR: DHAN_ACCESS_TOKEN missing in .env")
        sys.exit(1)

    for i, (date_obj, signal, opt_code, path) in enumerate(to_fetch, 1):
        api_type = "CALL" if opt_code == "CE" else "PUT"
        print(f"[{i}/{len(to_fetch)}] {date_obj} {signal} ATM {api_type} ...",
              end=" ", flush=True)
        try:
            df = _fetch_one_day(date_obj, api_type)
            if df.empty:
                print("empty")
            else:
                df.to_csv(path, index=False)
                print(f"{len(df)} bars  → {os.path.basename(path)}")
        except Exception as e:
            print(f"error: {e}")
        time.sleep(0.5)   # 2 req/s — 5x headroom below 10 req/s data API limit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: 1 year ago)")
    ap.add_argument("--end",   default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end   = args.end   or datetime.today().strftime("%Y-%m-%d")
    start = args.start or (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    print(f"Intraday options fetch: {start} → {end}")
    print(f"Cache:  {CACHE_DIR}/")
    fetch_all(start, end, dry_run=args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()
