#!/usr/bin/env python3
"""
fetch_intraday.py — Download 5-minute BankNifty intraday candles from Dhan API
================================================================================
Dhan retains ~90 days of intraday history. Saves to data/banknifty_5min.csv.

Usage:
    python3 fetch_intraday.py            # last 90 days
    python3 fetch_intraday.py --days 60  # last 60 days
"""

import os
import sys
import requests
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")

if not TOKEN or not CLIENT_ID:
    print("ERROR: DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID missing from .env")
    sys.exit(1)

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


def fetch_5min(from_date: str, to_date: str) -> pd.DataFrame | None:
    """Call Dhan intraday chart API for 5-min BankNifty candles."""
    payload = {
        "securityId":      "25",
        "exchangeSegment": "IDX_I",
        "instrument":      "INDEX",
        "interval":        "5",
        "fromDate":        from_date,
        "toDate":          to_date,
    }
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            headers=HEADERS, json=payload, timeout=30
        )
    except Exception as e:
        print(f"  Request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"  API error {resp.status_code}: {resp.text[:400]}")
        return None

    data = resp.json()

    timestamps = data.get("timestamp", [])
    opens      = data.get("open",      [])
    highs      = data.get("high",      [])
    lows       = data.get("low",       [])
    closes     = data.get("close",     [])

    if not timestamps:
        print("  No data returned from API.")
        return None

    # Dhan timestamps = Unix seconds at IST timezone → add 5h30m to correct
    dt = pd.to_datetime(timestamps, unit="s") + pd.Timedelta(hours=5, minutes=30)

    df = pd.DataFrame({
        "datetime": dt,
        "open":     opens,
        "high":     highs,
        "low":      lows,
        "close":    closes,
    })

    # Keep only market hours: 9:15 AM – 3:30 PM
    df["time"] = df["datetime"].dt.strftime("%H:%M")
    df = df[(df["time"] >= "09:15") & (df["time"] <= "15:30")].copy()
    df = df.drop(columns=["time"])
    df = df.sort_values("datetime").reset_index(drop=True)

    return df


def main():
    days = 90
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--days" and i + 2 <= len(sys.argv) - 1:
            try:
                days = int(sys.argv[i + 2])
            except ValueError:
                pass

    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"Fetching 5-min BankNifty candles: {from_date} → {to_date}  ({days} days)")

    # Dhan may limit range per call — fetch in 30-day chunks if needed
    chunks = []
    start = date.today() - timedelta(days=days)
    end   = date.today()
    chunk_size = timedelta(days=30)

    current = start
    while current < end:
        chunk_end = min(current + chunk_size, end)
        print(f"  Fetching {current} → {chunk_end}...", end=" ", flush=True)
        df_chunk = fetch_5min(
            current.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )
        if df_chunk is not None and not df_chunk.empty:
            chunks.append(df_chunk)
            print(f"{len(df_chunk)} candles")
        else:
            print("empty / error")
        current = chunk_end + timedelta(days=1)

    if not chunks:
        print("\nNo data fetched. Check token and retry.")
        sys.exit(1)

    df = pd.concat(chunks, ignore_index=True)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    out = f"{DATA_DIR}/banknifty_5min.csv"
    df.to_csv(out, index=False)

    # Summary
    df["date"] = pd.to_datetime(df["datetime"]).dt.date
    trading_days = df["date"].nunique()
    print(f"\nSaved {len(df)} candles across {trading_days} trading days → {out}")
    print(f"Date range: {df['date'].min()}  →  {df['date'].max()}")
    print(f"\nSample (last 5 candles):")
    print(df.tail(5).to_string(index=False))


if __name__ == "__main__":
    main()
