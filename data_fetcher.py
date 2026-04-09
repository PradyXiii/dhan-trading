import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import time

load_dotenv()
TOKEN = os.getenv("DHAN_ACCESS_TOKEN")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID")

HEADERS = {
    "access-token": TOKEN,
    "client-id": CLIENT_ID,
    "Content-Type": "application/json"
}

FROM_DATE = "2021-09-01"
TO_DATE   = "2026-04-09"
DATA_DIR  = "data"


def fetch_dhan_index(security_id, name, from_date, to_date):
    """Fetch daily OHLCV from Dhan API in 90-day chunks."""
    all_frames = []
    current = datetime.strptime(from_date, "%Y-%m-%d")
    end     = datetime.strptime(to_date,   "%Y-%m-%d")

    while current < end:
        chunk_end = min(current + timedelta(days=89), end)

        payload = {
            "securityId":      security_id,
            "exchangeSegment": "IDX_I",  # correct segment for NSE indices
            "instrument":      "INDEX",
            "expiryCode":      0,
            "fromDate":        current.strftime("%Y-%m-%d"),
            "toDate":          chunk_end.strftime("%Y-%m-%d")
        }

        resp = requests.post(
            "https://api.dhan.co/v2/charts/historical",
            headers=HEADERS,
            json=payload
        )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("open"):
                df = pd.DataFrame({
                    "date":   pd.to_datetime(data["timestamp"], unit="s").normalize(),
                    "open":   data["open"],
                    "high":   data["high"],
                    "low":    data["low"],
                    "close":  data["close"],
                    "volume": data["volume"]
                })
                all_frames.append(df)
                print(f"  {name}: {len(df)} rows  "
                      f"({current.strftime('%Y-%m-%d')} → {chunk_end.strftime('%Y-%m-%d')})")
        else:
            print(f"  {name}: ERROR {resp.status_code}  "
                  f"chunk {current.strftime('%Y-%m-%d')} — {resp.text[:120]}")

        current = chunk_end + timedelta(days=1)
        time.sleep(0.4)   # stay polite to the API

    if not all_frames:
        return pd.DataFrame()

    result = (pd.concat(all_frames)
                .drop_duplicates("date")
                .sort_values("date")
                .reset_index(drop=True))
    return result


def fetch_yfinance(ticker, name, from_date, to_date):
    """Fetch daily OHLCV from Yahoo Finance."""
    import yfinance as yf

    df = yf.download(ticker, start=from_date, end=to_date,
                     progress=False, auto_adjust=True)
    if df.empty:
        print(f"  {name}: no data returned")
        return pd.DataFrame()

    df = df.reset_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    # yfinance index is named 'Date' or 'Datetime' depending on version
    df.rename(columns={"datetime": "date", "Date": "date", "Datetime": "date"}, inplace=True)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = (df[["date", "open", "high", "low", "close", "volume"]]
            .sort_values("date")
            .reset_index(drop=True))

    print(f"  {name}: {len(df)} rows")
    return df


def fetch_nse_pcr(from_date, to_date):
    """
    Fetch BankNifty Put-Call Ratio from NSE historical data.

    NSE provides a daily bhav copy for F&O at:
    https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv  (lots reference)

    For PCR we use the NSE F&O bhavcopy which has OI data.
    However, NSE's historical PCR is not available as a direct endpoint.

    Strategy: use yfinance to approximate PCR via a proxy if direct NSE
    download fails. For now this fetches the NSE option chain PCR endpoint
    for each date — this works only for recent dates.

    NOTE: For historical PCR (2021-2026), users need to download NSE's
    historical F&O data manually from:
    https://www.nseindia.com/report-detail/fo_eq_security
    Select: BankNifty | Expiry: All | From/To dates → download CSV
    Then run: python3 data_fetcher.py --process-pcr <downloaded_file.csv>

    Returns empty DataFrame if data not available.
    """
    print("  PCR: NSE historical data requires manual download (see BACKTEST_LOG.md)")
    print("  Skipping PCR fetch — add pcr.csv manually to data/ folder to enable this signal.")
    return pd.DataFrame()


def fetch_nse_fii_dii(from_date, to_date):
    """
    Fetch FII/DII net cash-market buy/sell from NSE daily reports.

    NSE publishes FII/DII activity at:
    https://www.nseindia.com/api/fiidiiTradeReact?type=fiiDii
    (Current data only — not historical)

    For historical FII/DII data (2021–2026):
    Download from: https://www.nseindia.com/research/content/US_FiiDiiData.xlsx
    or from: https://www.nsdl.co.in/download/FPI_Monitor.zip

    Returns empty DataFrame if data not available.
    """
    print("  FII/DII: NSE historical data requires manual download (see BACKTEST_LOG.md)")
    print("  Skipping FII/DII fetch — add fii_dii.csv manually to data/ folder to enable this signal.")
    return pd.DataFrame()


def process_pcr_from_nse_bhavcopy(bhavcopy_file):
    """
    Process NSE F&O bhavcopy CSV to extract daily PCR for BankNifty.

    NSE bhavcopy columns include: SYMBOL, EXPIRY_DT, OPTION_TYP, OPEN_INT etc.
    PCR = sum(PUT open interest) / sum(CALL open interest) for all BN strikes on that date.

    Usage: python3 data_fetcher.py --process-pcr fo_mktlots_YYYYMMDD.csv
    """
    print(f"Processing NSE bhavcopy: {bhavcopy_file}")
    try:
        df = pd.read_csv(bhavcopy_file)
        df.columns = [c.strip().lower() for c in df.columns]

        # Filter BankNifty options
        bn = df[df["symbol"].str.strip() == "BANKNIFTY"].copy()
        if bn.empty:
            print("  No BANKNIFTY rows found in bhavcopy")
            return pd.DataFrame()

        bn["date"] = pd.to_datetime(bn["timestamp"].str.strip()
                                    if "timestamp" in bn.columns
                                    else bn["expiry_dt"].str.strip(),
                                    format="%d-%b-%Y", errors="coerce")

        # Daily PCR: sum PUT OI / sum CALL OI
        puts  = bn[bn["option_typ"].str.strip() == "PE"].groupby("date")["open_int"].sum()
        calls = bn[bn["option_typ"].str.strip() == "CE"].groupby("date")["open_int"].sum()
        pcr   = (puts / calls.replace(0, np.nan)).reset_index()
        pcr.columns = ["date", "pcr"]
        pcr = pcr.dropna().sort_values("date").reset_index(drop=True)
        print(f"  Extracted PCR for {len(pcr)} days")
        return pcr
    except Exception as e:
        print(f"  Error processing bhavcopy: {e}")
        return pd.DataFrame()


def main():
    import sys as _sys

    # Handle --process-pcr flag
    if len(_sys.argv) >= 3 and _sys.argv[1] == "--process-pcr":
        pcr = process_pcr_from_nse_bhavcopy(_sys.argv[2])
        if not pcr.empty:
            out = f"{DATA_DIR}/pcr.csv"
            os.makedirs(DATA_DIR, exist_ok=True)
            # Merge with existing pcr.csv if it exists
            if os.path.exists(out):
                existing = pd.read_csv(out, parse_dates=["date"])
                pcr = pd.concat([existing, pcr]).drop_duplicates("date").sort_values("date")
            pcr.to_csv(out, index=False)
            print(f"  → Saved {out}  ({len(pcr)} rows total)")
        return

    os.makedirs(DATA_DIR, exist_ok=True)

    # ── Dhan API indices ──────────────────────────────────────────────
    print("\n=== BankNifty (Dhan API) ===")
    bn = fetch_dhan_index("25", "BankNifty", FROM_DATE, TO_DATE)
    if not bn.empty:
        bn.to_csv(f"{DATA_DIR}/banknifty.csv", index=False)
        print(f"  → Saved banknifty.csv  ({len(bn)} rows total)\n")

    print("=== Nifty50 (Dhan API) ===")
    nf = fetch_dhan_index("13", "Nifty50", FROM_DATE, TO_DATE)
    if not nf.empty:
        nf.to_csv(f"{DATA_DIR}/nifty50.csv", index=False)
        print(f"  → Saved nifty50.csv  ({len(nf)} rows total)\n")

    # ── Yahoo Finance (international data) ───────────────────────────
    print("=== India VIX (Yahoo Finance) ===")
    vix = fetch_yfinance("^INDIAVIX", "India VIX", FROM_DATE, TO_DATE)
    if not vix.empty:
        vix.to_csv(f"{DATA_DIR}/india_vix.csv", index=False)
        print(f"  → Saved india_vix.csv  ({len(vix)} rows total)\n")

    print("=== S&P 500 (Yahoo Finance) ===")
    sp500 = fetch_yfinance("^GSPC", "S&P500", FROM_DATE, TO_DATE)
    if not sp500.empty:
        sp500.to_csv(f"{DATA_DIR}/sp500.csv", index=False)
        print(f"  → Saved sp500.csv  ({len(sp500)} rows total)\n")

    print("=== Nikkei 225 (Yahoo Finance) ===")
    nikkei = fetch_yfinance("^N225", "Nikkei225", FROM_DATE, TO_DATE)
    if not nikkei.empty:
        nikkei.to_csv(f"{DATA_DIR}/nikkei.csv", index=False)
        print(f"  → Saved nikkei.csv  ({len(nikkei)} rows total)\n")

    print("=== S&P 500 Futures (Yahoo Finance) ===")
    spf = fetch_yfinance("ES=F", "S&P Futures", FROM_DATE, TO_DATE)
    if not spf.empty:
        spf.to_csv(f"{DATA_DIR}/sp500_futures.csv", index=False)
        print(f"  → Saved sp500_futures.csv  ({len(spf)} rows total)\n")

    # ── Round 1: PCR and FII/DII (inform user about manual steps) ────
    print("=== PCR — BankNifty Put-Call Ratio ===")
    fetch_nse_pcr(FROM_DATE, TO_DATE)
    print()

    print("=== FII/DII Net Flows ===")
    fetch_nse_fii_dii(FROM_DATE, TO_DATE)
    print()

    print("=== All data files saved to data/ folder ===")


if __name__ == "__main__":
    main()
