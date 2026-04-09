"""
Round 2 data fetcher: NSE F&O Bhavcopy + Participant OI
========================================================
Downloads NSE archives for every Tuesday and Friday in the backtest period.
From each file, extracts:
  - BankNifty PCR (Put-Call Ratio)        → data/pcr.csv
  - BankNifty Max Pain strike             → data/max_pain.csv
  - BankNifty PUT vs CALL OI change       → data/oi_buildup.csv
  - FII net index futures position        → data/fii_fo.csv

Run: python3 fetch_round2_data.py
Safe to interrupt and re-run — progress is saved every 20 dates.
"""

import pandas as pd
import numpy as np
import requests
import zipfile
import io
import os
import time

DATA_DIR  = "data"
FROM_DATE = "2021-09-01"
TO_DATE   = "2026-04-09"


# ── NSE requires a browser-like session to avoid 403 ─────────────────────────

def get_nse_session():
    """Create a requests Session that looks like a browser to NSE servers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent":      ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.nseindia.com/",
        "Connection":      "keep-alive",
    })
    # Visit the homepage first to collect cookies
    try:
        session.get("https://www.nseindia.com/", timeout=15)
        time.sleep(1)
    except Exception:
        pass
    return session


# ── F&O Bhavcopy downloader ───────────────────────────────────────────────────

def download_fo_bhavcopy(session, date):
    """
    Download NSE F&O bhavcopy CSV for one date.
    URL: https://nsearchives.nseindia.com/content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip
    Returns DataFrame or None (holiday / file not available).
    """
    dd   = date.strftime("%d")
    mon  = date.strftime("%b").upper()
    yyyy = date.strftime("%Y")
    fname = f"fo{dd}{mon}{yyyy}bhav.csv.zip"
    url   = (f"https://nsearchives.nseindia.com/content/historical/"
             f"DERIVATIVES/{yyyy}/{mon}/{fname}")
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        z      = zipfile.ZipFile(io.BytesIO(resp.content))
        csvfile = next(n for n in z.namelist() if n.endswith(".csv"))
        df     = pd.read_csv(z.open(csvfile))
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception:
        return None


# ── Participant OI downloader ─────────────────────────────────────────────────

def download_participant_oi(session, date):
    """
    Download NSE F&O participant-wise OI.
    URL: https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
    Returns dict with fii_net_futures or None.
    """
    dd   = date.strftime("%d")
    mm   = date.strftime("%m")
    yyyy = date.strftime("%Y")
    url  = (f"https://archives.nseindia.com/content/nsccl/"
            f"fao_participant_oi_{dd}{mm}{yyyy}.csv")
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return None

        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip().replace(" ", "_").upper() for c in df.columns]

        # Find the FII row
        client_col = df.columns[0]
        fii_row = df[df[client_col].astype(str).str.strip().str.upper() == "FII"]
        if fii_row.empty:
            return None

        fii = fii_row.iloc[0]

        def to_int(val):
            try:
                return int(str(val).replace(",", "").strip())
            except (ValueError, TypeError):
                return 0

        # Net index futures position: long - short
        fl = to_int(fii.get("FUTURE_INDEX_LONG",  0))
        fs = to_int(fii.get("FUTURE_INDEX_SHORT", 0))
        fii_net_futures = fl - fs

        return {"fii_net_futures": fii_net_futures}
    except Exception:
        return None


# ── Metric computation ────────────────────────────────────────────────────────

def compute_banknifty_pcr(df):
    """PCR = Total PUT OI / Total CALL OI for BankNifty index options."""
    bn = df[(df["SYMBOL"].str.strip()     == "BANKNIFTY") &
            (df["INSTRUMENT"].str.strip() == "OPTIDX")].copy()
    if bn.empty:
        return np.nan
    put_oi  = bn[bn["OPTION_TYP"].str.strip() == "PE"]["OPEN_INT"].sum()
    call_oi = bn[bn["OPTION_TYP"].str.strip() == "CE"]["OPEN_INT"].sum()
    return round(put_oi / call_oi, 3) if call_oi > 0 else np.nan


def compute_oi_direction(df):
    """
    Compare net CHG_IN_OI for PUTs vs CALLs.
    Positive = calls building faster (bullish); Negative = puts building faster (bearish).
    """
    bn = df[(df["SYMBOL"].str.strip()     == "BANKNIFTY") &
            (df["INSTRUMENT"].str.strip() == "OPTIDX")].copy()
    if bn.empty:
        return {"put_oi_chg": 0, "call_oi_chg": 0}
    put_chg  = int(bn[bn["OPTION_TYP"].str.strip() == "PE"]["CHG_IN_OI"].sum())
    call_chg = int(bn[bn["OPTION_TYP"].str.strip() == "CE"]["CHG_IN_OI"].sum())
    return {"put_oi_chg": put_chg, "call_oi_chg": call_chg}


def compute_max_pain(df):
    """
    Max Pain = the strike price where total option WRITER losses are minimised.
    Theory: market gravitates toward max pain by expiry.

    Algorithm for each candidate strike S:
      Call writer pain = sum of (S - K) × OI  for all CALLs with K < S  (ITM calls)
      Put  writer pain = sum of (K - S) × OI  for all PUTs  with K > S  (ITM puts)
      Total pain at S = call writer pain + put writer pain
    Max pain = S with the minimum total pain.
    """
    bn = df[(df["SYMBOL"].str.strip()     == "BANKNIFTY") &
            (df["INSTRUMENT"].str.strip() == "OPTIDX")].copy()
    if len(bn) < 10:
        return np.nan

    # Use nearest expiry only
    bn["EXPIRY_DT"] = pd.to_datetime(
        bn["EXPIRY_DT"].astype(str).str.strip(),
        format="%d-%b-%Y", errors="coerce"
    )
    bn = bn.dropna(subset=["EXPIRY_DT"])
    if bn.empty:
        return np.nan
    bn = bn[bn["EXPIRY_DT"] == bn["EXPIRY_DT"].min()].copy()

    calls = bn[bn["OPTION_TYP"].str.strip() == "CE"].groupby("STRIKE_PR")["OPEN_INT"].sum()
    puts  = bn[bn["OPTION_TYP"].str.strip() == "PE"].groupby("STRIKE_PR")["OPEN_INT"].sum()
    strikes = sorted(set(calls.index) | set(puts.index))
    if len(strikes) < 5:
        return np.nan

    min_pain   = float("inf")
    max_pain_s = strikes[len(strikes) // 2]

    for s in strikes:
        call_pain = sum((s - k) * oi for k, oi in calls.items() if k < s)
        put_pain  = sum((k - s) * oi for k, oi in puts.items()  if k > s)
        total     = call_pain + put_pain
        if total < min_pain:
            min_pain   = total
            max_pain_s = s

    return float(max_pain_s)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _save_progress(pcr_rows, max_pain_rows, oi_rows, fii_rows):
    """Write accumulated rows to CSVs (merge with any existing data)."""
    def save(rows, path, key="date"):
        if not rows:
            return
        new_df = pd.DataFrame(rows).drop_duplicates(key).sort_values(key)
        if os.path.exists(path):
            old_df = pd.read_csv(path)
            combined = (pd.concat([old_df, new_df])
                          .drop_duplicates(key)
                          .sort_values(key)
                          .reset_index(drop=True))
            combined.to_csv(path, index=False)
        else:
            new_df.to_csv(path, index=False)

    save(pcr_rows,       f"{DATA_DIR}/pcr.csv")
    save(max_pain_rows,  f"{DATA_DIR}/max_pain.csv")
    save(oi_rows,        f"{DATA_DIR}/oi_buildup.csv")
    save(fii_rows,       f"{DATA_DIR}/fii_fo.csv")


def build_all_round2_data(from_date, to_date):
    os.makedirs(DATA_DIR, exist_ok=True)

    # All Tuesdays and Fridays in range
    all_bdays    = pd.date_range(from_date, to_date, freq="B")
    trade_dates  = [d for d in all_bdays if d.weekday() in [1, 4]]

    # Resume support: skip dates already in pcr.csv
    done_dates = set()
    if os.path.exists(f"{DATA_DIR}/pcr.csv"):
        existing = pd.read_csv(f"{DATA_DIR}/pcr.csv", parse_dates=["date"])
        done_dates = set(existing["date"].dt.date)
        print(f"  Resuming — {len(done_dates)} dates already processed, "
              f"{len(trade_dates) - len(done_dates)} remaining.\n")

    pcr_rows, max_pain_rows, oi_rows, fii_rows = [], [], [], []
    session  = get_nse_session()
    new_count = 0

    for i, date in enumerate(trade_dates):
        if date.date() in done_dates:
            continue

        # ── F&O bhavcopy ──────────────────────────────────────────────────────
        df = download_fo_bhavcopy(session, date)
        if df is not None:
            pcr      = compute_banknifty_pcr(df)
            oi_dir   = compute_oi_direction(df)
            mp       = compute_max_pain(df)

            pcr_rows.append({"date": date.date(), "pcr": pcr})
            max_pain_rows.append({"date": date.date(), "max_pain": mp})
            oi_rows.append({
                "date":        date.date(),
                "put_oi_chg":  oi_dir["put_oi_chg"],
                "call_oi_chg": oi_dir["call_oi_chg"],
            })
            tag = f"PCR={pcr:.2f}  MaxPain={mp:.0f}  PUT_chg={oi_dir['put_oi_chg']:+,}"
        else:
            tag = "no bhavcopy (holiday or NSE blocked)"

        # ── Participant OI (FII futures) ───────────────────────────────────────
        fii = download_participant_oi(session, date)
        if fii:
            fii_rows.append({"date": date.date(), **fii})
            tag += f"  FII_fut={fii['fii_net_futures']:+,}"

        progress = i + 1
        remaining = len(trade_dates) - progress
        print(f"  [{progress:>3}/{len(trade_dates)}] {date.date()}  {tag}"
              f"  (≈{remaining * 0.9 / 60:.0f} min left)")
        new_count += 1

        # Save progress every 20 new dates
        if new_count % 20 == 0:
            _save_progress(pcr_rows, max_pain_rows, oi_rows, fii_rows)
            pcr_rows, max_pain_rows, oi_rows, fii_rows = [], [], [], []
            print(f"\n  → Checkpoint saved ({progress} total dates processed)\n")

        # Refresh session every 60 requests (cookies can expire)
        if new_count % 60 == 0:
            session = get_nse_session()

        time.sleep(0.8)   # be polite — ~0.8s per file

    # Final save
    _save_progress(pcr_rows, max_pain_rows, oi_rows, fii_rows)

    print(f"\n{'='*60}")
    print(f"  Done!  {new_count} new dates processed.")
    for fname in ["pcr.csv", "max_pain.csv", "oi_buildup.csv", "fii_fo.csv"]:
        fpath = f"{DATA_DIR}/{fname}"
        if os.path.exists(fpath):
            n = len(pd.read_csv(fpath))
            print(f"  {fname:<20}: {n} rows")
    print(f"{'='*60}")
    print(f"\nNext: python3 signal_engine.py && python3 backtest_engine.py")


def main():
    print("=== NSE Round 2 Data Fetcher ===")
    print(f"Period : {FROM_DATE} → {TO_DATE}")
    print(f"Targets: ~450 Tuesday + Friday dates")
    print(f"Output : data/pcr.csv, max_pain.csv, oi_buildup.csv, fii_fo.csv")
    print(f"Time   : ~6 minutes (0.8s per file, resumable)\n")
    build_all_round2_data(FROM_DATE, TO_DATE)


if __name__ == "__main__":
    main()
