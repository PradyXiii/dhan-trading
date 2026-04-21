#!/usr/bin/env python3
"""
Fetch 1-minute option premiums for each historical CALL/PUT trade day.

Two fetch modes:

  ATM mode (default):
    Fetches ATM CE for CALL days, ATM PE for PUT days.
    Cache: data/intraday_options_cache/{YYYY-MM-DD}_{CE|PE}.csv
    Used by backtest_engine.py --real-options.

  Spreads mode (--spreads):
    For each signal day, also fetches the legs needed for spread strategies:
      CALL day: ATM+3 CE (Bull Call Spread short leg)
                ATM-3 CE (Bear Call Spread long leg, for Iron Condor)
                ATM PE   (Long Straddle second leg)
      PUT  day: ATM-3 PE (Bear Put Spread short leg)
                ATM+3 PE (Bull Put Spread long leg, for Iron Condor)
                ATM CE   (Long Straddle second leg)
    Cache: data/intraday_options_cache/{YYYY-MM-DD}_{CE|PE}_{offset}.csv
      e.g. 2024-03-15_CE_p3.csv = ATM+3 CE on 2024-03-15
           2024-03-15_PE_m3.csv = ATM-3 PE on 2024-03-15
           2024-03-15_PE_straddle.csv = ATM PE on a CALL signal day

  API note: Dhan rollingoption supports "ATM", "ATM+1"..."ATM+3"/"ATM-1"..."ATM-3"
  for non-expiry-approaching contracts, up to "ATM+10"/"ATM-10" near expiry.
  BN strikes are 100-pt intervals, so ATM+3 = 300pts OTM.

  Caveat (ATM rolling):
    The "ATM" contract rolls when spot crosses a strike boundary. Big-move days
    may have the effective contract change mid-session; the `strike` column in
    each bar shows which actual strike was live at that minute.

Usage:
  python3 fetch_intraday_options.py                    # ATM only, last 365 days
  python3 fetch_intraday_options.py --start 2021-08-01 # ATM only, full history
  python3 fetch_intraday_options.py --spreads          # spread legs, last 365 days
  python3 fetch_intraday_options.py --spreads --start 2021-08-01  # full + spreads
  python3 fetch_intraday_options.py --dry-run          # list what would be fetched
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

# Spread legs to fetch for each signal type.
# Format: (strike_label, api_opt_type, file_suffix)
#   strike_label: Dhan API "strike" param value ("ATM", "ATM+3", etc.)
#   api_opt_type: "CALL" or "PUT" (drvOptionType)
#   file_suffix:  appended to {date}_{CE|PE}_{suffix}.csv; None = ATM (no suffix)
SPREAD_LEGS = {
    "CALL": [
        ("ATM+3", "CALL", "p3"),            # Bear Call short wing (ATM+3 CE)
        ("ATM-3", "CALL", "m3"),            # cross-strike
        ("ATM",   "PUT",  "straddle"),      # Straddle + IC short PE leg
        ("ATM-3", "PUT",  "m3_straddle"),   # Iron Condor: long PE wing on CALL days
    ],
    "PUT": [
        ("ATM-3", "PUT",  "m3"),            # Bull Put short wing (ATM-3 PE)
        ("ATM+3", "PUT",  "p3"),            # cross-strike
        ("ATM",   "CALL", "straddle"),      # Straddle + IC short CE leg
        ("ATM+3", "CALL", "p3_straddle"),   # Iron Condor: long CE wing on PUT days
    ],
}


def _cache_path(date_str: str, opt_code: str, suffix: str = None) -> str:
    """
    Return cache path for a given date, option type, and offset label.
      date_str : "YYYY-MM-DD"
      opt_code : "CE" or "PE" (maps to option type regardless of trade signal)
      suffix   : None → "{date}_{opt_code}.csv" (ATM, backward-compat)
                 str  → "{date}_{opt_code}_{suffix}.csv"
    """
    if suffix is None:
        return os.path.join(CACHE_DIR, f"{date_str}_{opt_code}.csv")
    return os.path.join(CACHE_DIR, f"{date_str}_{opt_code}_{suffix}.csv")


def _fetch_one_day(date_obj, api_opt_type: str,
                   strike: str = "ATM") -> pd.DataFrame:
    """
    Fetch 1-minute bars for ONE day, one option type.
    Uses expiryFlag=WEEK before Nov 20 2024, MONTH from that date on.
    Returns DataFrame: dt, open, high, low, close, volume, strike.

    strike: Dhan API strike param — "ATM", "ATM+1" … "ATM+3", "ATM-1" … "ATM-3"
    """
    expiry_flag = "WEEK" if date_obj < WEEKLY_BOUNDARY else "MONTH"
    next_day    = date_obj + timedelta(days=1)

    payload = {
        "exchangeSegment": "NSE_FNO",
        "interval":        1,
        "securityId":      25,           # BankNifty underlying
        "instrument":      "OPTIDX",
        "expiryFlag":      expiry_flag,
        "expiryCode":      1,            # nearest expiry
        "strike":          strike,
        "drvOptionType":   api_opt_type,
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


def fetch_all(start: str, end: str, dry_run: bool = False,
              spreads: bool = False) -> None:
    """
    For each CALL/PUT day in signals_ml.csv between [start, end]:
      - Always fetches ATM CE (CALL days) / ATM PE (PUT days).
      - If spreads=True, also fetches spread legs per SPREAD_LEGS dict above.
    Idempotent — skips days whose cache file already exists.
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

    # Build fetch list: (date, signal, api_opt_type, strike, opt_code, suffix, path)
    to_fetch = []
    for _, r in days.iterrows():
        date_str = r["date"].strftime("%Y-%m-%d")
        opt_code = "CE" if r["signal"] == "CALL" else "PE"

        # ATM leg (always)
        atm_path = _cache_path(date_str, opt_code)
        if not os.path.exists(atm_path):
            api_type = "CALL" if opt_code == "CE" else "PUT"
            to_fetch.append((r["date"], r["signal"], api_type, "ATM",
                             opt_code, None, atm_path))

        # Spread legs (optional)
        if spreads:
            for strike_label, api_type, suffix in SPREAD_LEGS[r["signal"]]:
                sc = "CE" if api_type == "CALL" else "PE"
                sp_path = _cache_path(date_str, sc, suffix)
                if not os.path.exists(sp_path):
                    to_fetch.append((r["date"], r["signal"], api_type,
                                     strike_label, sc, suffix, sp_path))

    total_days = len(days)
    total_atm  = sum(1 for _, r in days.iterrows()
                     if not os.path.exists(
                         _cache_path(r["date"].strftime("%Y-%m-%d"),
                                     "CE" if r["signal"] == "CALL" else "PE")))
    print(f"Trade days in range:   {total_days}  (CALL/PUT)")
    print(f"ATM cache hits:        {total_days - total_atm}")
    print(f"Total to fetch:        {len(to_fetch)} calls")
    if spreads:
        print(f"  (ATM + spread legs — 3 extra calls per day)")

    if dry_run:
        for d, sig, api_t, stk, oc, suf, _ in to_fetch[:15]:
            label = f"{stk} {api_t}" + (f" [{suf}]" if suf else "")
            print(f"  {d}  {sig:<4}  {label}")
        if len(to_fetch) > 15:
            print(f"  ... and {len(to_fetch) - 15} more")
        return

    if not TOKEN:
        print("ERROR: DHAN_ACCESS_TOKEN missing in .env")
        sys.exit(1)

    ok = err = empty = 0
    for i, (date_obj, signal, api_type, strike, opt_code, suffix, path) in \
            enumerate(to_fetch, 1):
        label = f"{strike} {api_type}" + (f" [{suffix}]" if suffix else "")
        print(f"[{i}/{len(to_fetch)}] {date_obj} {signal} {label} ...",
              end=" ", flush=True)
        try:
            df = _fetch_one_day(date_obj, api_type, strike=strike)
            if df.empty:
                print("empty")
                empty += 1
            else:
                df.to_csv(path, index=False)
                print(f"{len(df)} bars  → {os.path.basename(path)}")
                ok += 1
        except Exception as e:
            print(f"error: {e}")
            err += 1
        time.sleep(0.5)   # 2 req/s — 5x headroom

    print(f"\nDone: {ok} saved, {empty} empty (pre-2021 normal), {err} errors")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start",   default=None,
                    help="YYYY-MM-DD (default: 1 year ago)")
    ap.add_argument("--end",     default=None,
                    help="YYYY-MM-DD (default: today)")
    ap.add_argument("--spreads", action="store_true",
                    help="Also fetch OTM/straddle legs for spread strategies")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be fetched — no API calls")
    args = ap.parse_args()

    end   = args.end   or datetime.today().strftime("%Y-%m-%d")
    start = args.start or (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    mode = "ATM + spread legs" if args.spreads else "ATM only"
    print(f"Intraday options fetch ({mode}): {start} → {end}")
    print(f"Cache: {CACHE_DIR}/")
    fetch_all(start, end, dry_run=args.dry_run, spreads=args.spreads)
    print("Done.")


if __name__ == "__main__":
    main()
