# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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
TO_DATE   = datetime.today().strftime("%Y-%m-%d")
DATA_DIR  = "data"


def _last_csv_date(path):
    """
    Return the day AFTER the last date in an existing CSV (as YYYY-MM-DD string),
    so callers can use it directly as from_date for an incremental fetch.
    Returns None if file doesn't exist or is unreadable.
    """
    try:
        if os.path.exists(path):
            df = pd.read_csv(path, usecols=["date"])
            if not df.empty:
                last = pd.to_datetime(df["date"]).max()
                return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _merge_and_save(csv_path, df_new):
    """Merge new rows into existing CSV, deduplicate by date, sort, save."""
    if df_new is None or df_new.empty:
        return
    if os.path.exists(csv_path):
        existing = pd.read_csv(csv_path, parse_dates=["date"])
        combined = (pd.concat([existing, df_new])
                      .drop_duplicates("date")
                      .sort_values("date")
                      .reset_index(drop=True))
    else:
        combined = df_new
    combined.to_csv(csv_path, index=False)


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
                # Dhan timestamps are midnight IST (UTC+5:30), not UTC.
                # Parsing as UTC shifts every date 1 day earlier (Mon → Sun, etc.)
                # Fix: add the IST offset before normalising to recover the correct date.
                _ts = (pd.to_datetime(data["timestamp"], unit="s")
                       + pd.Timedelta(hours=5, minutes=30)).normalize()
                df = pd.DataFrame({
                    "date":   _ts,
                    "open":   data["open"],
                    "high":   data["high"],
                    "low":    data["low"],
                    "close":  data["close"],
                    "volume": data["volume"]
                })
                all_frames.append(df)
                print(f"  {name}: {len(df)} rows  "
                      f"({current.strftime('%Y-%m-%d')} → {chunk_end.strftime('%Y-%m-%d')})")
        elif resp.status_code == 400 and "DH-905" in resp.text:
            # DH-905 on a short/recent range = no trading data (weekend or holiday) — not an error
            print(f"  {name}: no new trading data ({current.strftime('%Y-%m-%d')} — weekend/holiday)")
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


def fetch_gold(from_date, to_date):
    """Fetch Gold Futures (GC=F) daily OHLCV from Yahoo Finance."""
    return fetch_yfinance("GC=F", "Gold Futures", from_date, to_date)


def fetch_crude(from_date, to_date):
    """Fetch Crude Oil Futures (CL=F) daily OHLCV from Yahoo Finance."""
    return fetch_yfinance("CL=F", "Crude Oil", from_date, to_date)


def fetch_usdinr(from_date, to_date):
    """Fetch USD/INR exchange rate from Yahoo Finance."""
    return fetch_yfinance("USDINR=X", "USD/INR", from_date, to_date)


def fetch_dxy(from_date, to_date):
    """Fetch US Dollar Index (DXY) from Yahoo Finance."""
    return fetch_yfinance("DX-Y.NYB", "DXY", from_date, to_date)


def fetch_us10y(from_date, to_date):
    """Fetch US 10-Year Treasury Yield (^TNX) from Yahoo Finance."""
    return fetch_yfinance("^TNX", "US 10Y Yield", from_date, to_date)


def fetch_fii_today():
    """
    Fetch today's FII/DII net flows from NSE live API and append to data/fii_dii.csv.

    NSE publishes same-day FII/DII cash market and derivatives data at:
    https://www.nseindia.com/api/fiidiiTradeReact?type=fiiDii

    Columns saved: date, fii_net_cash (₹ Cr), fii_net_fut (₹ Cr), dii_net_cash (₹ Cr)
    """
    url = "https://www.nseindia.com/api/fiidiiTradeReact?type=fiiDii"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    }
    out_path = f"{DATA_DIR}/fii_dii.csv"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"  FII/DII: NSE API returned {resp.status_code} — skipping")
            return pd.DataFrame()

        data = resp.json()
        rows = []
        for item in (data if isinstance(data, list) else []):
            try:
                date = pd.to_datetime(item.get("date", ""), dayfirst=True).normalize()
                fii_cash = float(str(item.get("fiiBuySell", 0)).replace(",", "") or 0)
                fii_fut  = float(str(item.get("fiiFutBuySell", 0)).replace(",", "") or 0)
                dii_cash = float(str(item.get("diiBuySell", 0)).replace(",", "") or 0)
                rows.append({"date": date, "fii_net_cash": fii_cash,
                             "fii_net_fut": fii_fut, "dii_net_cash": dii_cash})
            except Exception:
                continue

        if not rows:
            print("  FII/DII: no rows parsed from NSE response — skipping")
            return pd.DataFrame()

        new_df = pd.DataFrame(rows).dropna(subset=["date"])

        if os.path.exists(out_path):
            existing = pd.read_csv(out_path, parse_dates=["date"])
            combined = (pd.concat([existing, new_df])
                          .drop_duplicates("date")
                          .sort_values("date")
                          .reset_index(drop=True))
        else:
            combined = new_df.sort_values("date").reset_index(drop=True)

        combined.to_csv(out_path, index=False)
        print(f"  FII/DII: fetched {len(new_df)} rows → {out_path} ({len(combined)} total)")
        return new_df

    except Exception as e:
        print(f"  FII/DII: error — {e}")
        return pd.DataFrame()


def fetch_pcr_dhan_today():
    """
    Fetch BankNifty option chain from Dhan API, compute PCR, append to data/pcr_live.csv.

    Dhan option chain endpoint: POST /v2/optionchain
    PCR = sum(PUT OI) / sum(CALL OI) across all strikes for near expiry.

    Response structure: data["data"]["oc"]["55900.000000"]["pe"]["oi"]
    Must call expirylist first — Expiry field cannot be empty.
    """
    from datetime import date as _date

    out_path = f"{DATA_DIR}/pcr_live.csv"
    today = _date.today()

    # Step 1: get nearest expiry (cannot send empty Expiry string)
    expiry_str = ""
    try:
        el_resp = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        if el_resp.status_code == 200:
            expiries = el_resp.json().get("data", [])
            if expiries:
                expiry_str = str(expiries[0])
    except Exception as e:
        print(f"  PCR (Dhan): expirylist error — {e}")

    if not expiry_str:
        print("  PCR (Dhan): could not get expiry date — skipping")
        return pd.DataFrame()

    # Step 2: fetch option chain with valid expiry
    payload = {
        "UnderlyingScrip": 25,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry_str,
    }
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"  PCR (Dhan): API returned {resp.status_code} — skipping")
            return pd.DataFrame()

        data = resp.json()

        # Response: data["data"] may be {oc: {...}} directly, or {"811": {oc: {...}}}
        inner = data.get("data") or {}
        oc = inner.get("oc") if isinstance(inner, dict) else None
        if not oc:
            # Try nested key (some response shapes nest under security ID like "811")
            for v in (inner.values() if isinstance(inner, dict) else []):
                if isinstance(v, dict) and "oc" in v:
                    oc = v["oc"]
                    break

        if not oc:
            print("  PCR (Dhan): could not find oc in option chain response — skipping")
            return pd.DataFrame()

        # Sum OI across all strikes
        put_oi  = 0.0
        call_oi = 0.0
        for strike_data in oc.values():
            if not isinstance(strike_data, dict):
                continue
            pe = strike_data.get("pe") or strike_data.get("PE") or {}
            ce = strike_data.get("ce") or strike_data.get("CE") or {}
            put_oi  += float(pe.get("oi") or pe.get("openInterest") or pe.get("open_int") or 0)
            call_oi += float(ce.get("oi") or ce.get("openInterest") or ce.get("open_int") or 0)

        pcr_val = round(put_oi / call_oi, 4) if call_oi > 0 else np.nan

        new_row = pd.DataFrame([{"date": pd.Timestamp(today), "pcr": pcr_val}])

        if os.path.exists(out_path):
            existing = pd.read_csv(out_path, parse_dates=["date"])
            combined = (pd.concat([existing, new_row])
                          .drop_duplicates("date")
                          .sort_values("date")
                          .reset_index(drop=True))
        else:
            combined = new_row

        combined.to_csv(out_path, index=False)
        print(f"  PCR (Dhan): {pcr_val:.3f}  → {out_path}")
        return new_row

    except Exception as e:
        print(f"  PCR (Dhan): error — {e}")
        return pd.DataFrame()


# Nov 20 2024: BN weekly options discontinued → monthly only from this date
_WEEKLY_DISCONTINUED = datetime(2024, 11, 20)


def fetch_rollingoption(from_date, to_date):
    """
    Fetch ATM CALL + PUT 9:15 AM open premiums from Dhan /charts/rollingoption.
    Saves to data/options_atm_daily.csv:
      date, call_premium, call_strike, put_premium, put_strike

    Uses WEEK expiryFlag before Nov 20 2024, MONTH from Nov 20 2024 onwards.
    30-day chunks to respect the API max-range limit. ~1 min for 5 years.
    Falls back gracefully if individual chunks fail.
    """
    from datetime import date as _dt
    out_path = f"{DATA_DIR}/options_atm_daily.csv"

    start    = datetime.strptime(from_date, "%Y-%m-%d").date()
    end      = datetime.strptime(to_date,   "%Y-%m-%d").date()
    boundary = _WEEKLY_DISCONTINUED.date()   # WEEK → MONTH transition

    all_rows = []
    current  = start

    while current < end:
        # Determine expiryFlag — cap chunk at phase boundary if it straddles it
        if current < boundary:
            expiry_flag = "WEEK"
            chunk_end   = min(current + timedelta(days=28),
                              min(end, boundary - timedelta(days=1)))
        else:
            expiry_flag = "MONTH"
            chunk_end   = min(current + timedelta(days=28), end)

        chunk_calls: dict = {}
        chunk_puts:  dict = {}

        for opt_type in ["CALL", "PUT"]:
            payload = {
                "exchangeSegment": "NSE_FNO",
                "interval":        15,         # 15-min bars; first bar open = 9:15 AM open
                "securityId":      25,
                "instrument":      "OPTIDX",
                "expiryFlag":      expiry_flag,
                "expiryCode":      1,          # nearest expiry (1 = current; 0 treated as missing)
                "strike":          "ATM",
                "drvOptionType":   opt_type,
                "requiredData":    ["open", "strike"],
                "fromDate":        current.strftime("%Y-%m-%d"),
                "toDate":          chunk_end.strftime("%Y-%m-%d"),
            }
            try:
                resp = requests.post(
                    "https://api.dhan.co/v2/charts/rollingoption",
                    headers=HEADERS,
                    json=payload,
                    timeout=30,
                )
                if resp.status_code != 200:
                    print(f"  rollingoption {opt_type} [{current}→{chunk_end}]: "
                          f"HTTP {resp.status_code} — {resp.text[:400]}")
                    time.sleep(0.4)
                    continue

                d        = resp.json().get("data", {})
                # CALL → data["ce"],  PUT → data["pe"]  (fallback to "ce")
                opt_data = (d.get("ce") if opt_type == "CALL" else d.get("pe")) or \
                           d.get("ce") or {}

                if not opt_data or not opt_data.get("timestamp"):
                    print(f"  rollingoption {opt_type} [{current}→{chunk_end}]: empty")
                    time.sleep(0.4)
                    continue

                # Convert Unix epoch → IST, group by IST date, take first row
                ts_ist = (pd.to_datetime(opt_data["timestamp"], unit="s")
                          + pd.Timedelta(hours=5, minutes=30))
                df_c   = pd.DataFrame({
                    "dt":     ts_ist,
                    "open":   opt_data["open"],
                    "strike": opt_data.get("strike") or [None] * len(ts_ist),
                })
                df_c["date"] = df_c["dt"].dt.normalize()
                daily = (df_c.groupby("date", sort=True)
                              .first()
                              .reset_index()[["date", "open", "strike"]])

                col_p = "call_premium" if opt_type == "CALL" else "put_premium"
                col_s = "call_strike"  if opt_type == "CALL" else "put_strike"
                dest  = chunk_calls    if opt_type == "CALL" else chunk_puts

                for _, r in daily.iterrows():
                    dest[r["date"]] = {col_p: r["open"], col_s: r["strike"]}

                print(f"  rollingoption ATM {opt_type} [{current}→{chunk_end}]: "
                      f"{len(daily)} days  (flag={expiry_flag})")

            except Exception as e:
                print(f"  rollingoption {opt_type} [{current}→{chunk_end}]: error — {e}")

            time.sleep(0.4)   # stay within data API rate limit (5/sec)

        # Merge CALL + PUT for this chunk
        all_dates = sorted(set(list(chunk_calls.keys()) + list(chunk_puts.keys())))
        for d in all_dates:
            row = {"date": d}
            row.update(chunk_calls.get(d, {"call_premium": None, "call_strike": None}))
            row.update(chunk_puts.get(d,  {"put_premium":  None, "put_strike":  None}))
            all_rows.append(row)

        current = chunk_end + timedelta(days=1)

    if not all_rows:
        print("  rollingoption: no data fetched — check Dhan credentials/subscription")
        return pd.DataFrame()

    new_df = (pd.DataFrame(all_rows)
                .drop_duplicates("date")
                .sort_values("date")
                .reset_index(drop=True))

    # Merge with existing (incremental updates on subsequent runs)
    if os.path.exists(out_path):
        existing = pd.read_csv(out_path, parse_dates=["date"])
        combined = (pd.concat([existing, new_df])
                      .drop_duplicates("date")
                      .sort_values("date")
                      .reset_index(drop=True))
    else:
        combined = new_df

    os.makedirs(DATA_DIR, exist_ok=True)
    combined.to_csv(out_path, index=False)
    real_count = combined["call_premium"].notna().sum()
    print(f"  → Saved options_atm_daily.csv  ({len(combined)} rows, "
          f"{real_count} with real CALL premium)")
    return new_df


def fetch_pcr_historical(from_date="2022-01-01", to_date=None, workers=8):
    """
    Download NSE F&O bhavcopy archives and compute BankNifty daily PCR.

    Archives: https://nsearchives.nseindia.com/content/historical/DERIVATIVES/
              YYYY/MON/foDDMONYYYYbhav.csv.zip

    PCR = sum(BANKNIFTY PE open interest) / sum(BANKNIFTY CE open interest).
    Appends new rows to data/pcr.csv. Skips dates already present.
    Uses ThreadPoolExecutor for parallel downloads (~8x faster than sequential).
    """
    import io, zipfile, threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if to_date is None:
        to_date = datetime.today().strftime("%Y-%m-%d")

    out_path = f"{DATA_DIR}/pcr.csv"
    os.makedirs(DATA_DIR, exist_ok=True)

    # Dates already in pcr.csv — skip them
    existing_dates = set()
    if os.path.exists(out_path):
        try:
            ex = pd.read_csv(out_path, parse_dates=["date"])
            existing_dates = set(ex["date"].dt.date)
        except Exception:
            pass

    # Build list of all weekday dates not yet fetched
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end   = datetime.strptime(to_date,   "%Y-%m-%d")
    todo  = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d.date() not in existing_dates:
            todo.append(d)
        d += timedelta(days=1)

    if not todo:
        print(f"  → pcr.csv: no new dates to fetch (all already present)")
        return []

    est_min = max(1, round(len(todo) / workers / 4))   # ~4 req/s per worker
    print(f"  Fetching NSE bhavcopy PCR: {from_date} → {to_date}")
    print(f"  ↳ {len(todo)} trading days to fetch · {workers} parallel workers · "
          f"~{est_min} min estimated")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Referer":    "https://nsearchives.nseindia.com/",
        "Accept":     "*/*",
    }

    _lock = threading.Lock()
    found_count = 0

    def fetch_one_day(day: datetime):
        date_str = day.strftime("%d%b%Y").upper()   # e.g. 13APR2026
        year_str = day.strftime("%Y")
        mon_str  = day.strftime("%b").upper()        # e.g. APR
        url = (f"https://nsearchives.nseindia.com/content/historical/"
               f"DERIVATIVES/{year_str}/{mon_str}/fo{date_str}bhav.csv.zip")

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code in (404, 403):
                return None    # holiday / non-trading day
            if resp.status_code != 200:
                return None

            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    df = pd.read_csv(f)

            df.columns = [c.strip().lower() for c in df.columns]

            sym_col = next((c for c in df.columns if "symbol"     in c), None)
            oi_col  = next((c for c in df.columns if "open_int"   in c or c == "oi"), None)
            typ_col = next((c for c in df.columns if "option_typ" in c or "optiontype" in c), None)
            if sym_col is None or oi_col is None or typ_col is None:
                return None

            bn = df[df[sym_col].str.strip() == "BANKNIFTY"].copy()
            if bn.empty:
                return None

            bn[oi_col] = pd.to_numeric(bn[oi_col], errors="coerce").fillna(0)
            puts  = bn[bn[typ_col].str.strip() == "PE"][oi_col].sum()
            calls = bn[bn[typ_col].str.strip() == "CE"][oi_col].sum()

            if calls > 0:
                pcr_val = round(float(puts) / float(calls), 4)
                return {"date": pd.Timestamp(day.date()), "pcr": pcr_val}

        except zipfile.BadZipFile:
            pass    # NSE returns HTML for holidays — treat as non-trading
        except Exception:
            pass

        return None

    new_rows = []
    done = 0
    total = len(todo)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one_day, day): day for day in todo}
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result is not None:
                with _lock:
                    new_rows.append(result)
                    found_count = len(new_rows)
            # Print in-place progress every 10 completions or at end
            if done % 10 == 0 or done == total:
                pct = int(done / total * 100)
                print(f"  ↳ {done}/{total} ({pct}%)  found {found_count} PCR values",
                      end="\r", flush=True)

    print()   # newline after final \r progress

    if new_rows:
        new_df = pd.DataFrame(new_rows).sort_values("date").reset_index(drop=True)
        _merge_and_save(out_path, new_df)
        total_rows = len(pd.read_csv(out_path))
        print(f"  → pcr.csv: +{len(new_rows)} new rows ({total_rows} total)")
    else:
        print(f"  → pcr.csv: no new rows (all dates were holidays or already present)")

    return new_rows


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
    print("  PCR: NSE historical data requires manual download (see README.md for instructions)")
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
    print("  FII/DII: NSE historical data requires manual download (see README.md for instructions)")
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


def fix_dhan_dates():
    """
    One-time patch for the Dhan API timezone bug.

    Root cause: Dhan timestamps are midnight IST (UTC+5:30).  When parsed as UTC
    the date rolls back to the previous calendar day, so every weekday shifts:
        Mon NSE data → stored as Sun
        Tue NSE data → stored as Mon
        Wed NSE data → stored as Tue  (expiry day — wrongly included as "Tuesday")
        Thu NSE data → stored as Wed  (wrongly excluded as "Wednesday"/expiry)
        Fri NSE data → stored as Thu

    Fix: add +1 day to all dates in banknifty.csv and nifty50.csv.
    Weekend dates that result (from the 4 garbage Friday / 2 Saturday rows) are dropped.
    """
    for fname in [f"{DATA_DIR}/banknifty.csv", f"{DATA_DIR}/nifty50.csv"]:
        if not os.path.exists(fname):
            print(f"  {fname}: not found, skipping")
            continue
        df = pd.read_csv(fname, parse_dates=["date"])
        before = df["date"].dt.day_name().value_counts().sort_index()
        df["date"] = df["date"] + pd.Timedelta(days=1)
        df = df[df["date"].dt.weekday < 5]           # drop any resulting weekend rows
        df = (df.drop_duplicates("date")
                .sort_values("date")
                .reset_index(drop=True))
        after = df["date"].dt.day_name().value_counts().sort_index()
        df.to_csv(fname, index=False)
        print(f"\n  {fname}  ({len(df)} rows)")
        print(f"  {'Day':<12} {'Before':>8}  {'After':>8}")
        print(f"  {'─'*32}")
        for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
            b = before.get(day, 0)
            a = after.get(day, 0)
            print(f"  {day:<12} {b:>8}  {a:>8}")
    print(f"\n  Done. Re-run: python3 signal_engine.py && python3 backtest_engine.py")


def main():
    import sys as _sys

    # Handle --fix-dates flag (one-time timezone patch for existing CSVs)
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--fix-dates":
        print("=== Fixing Dhan API timezone bug in existing CSV files ===")
        fix_dhan_dates()
        return

    # Handle --fetch-options: fetch historical ATM option premiums only
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--fetch-options":
        print("=== Historical ATM Option Premiums (Dhan rollingoption) ===")
        os.makedirs(DATA_DIR, exist_ok=True)
        df = fetch_rollingoption(FROM_DATE, TO_DATE)
        if not df.empty:
            print(f"  Done. {len(df)} new rows fetched.")
        return

    # Handle --fetch-pcr-historical flag
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--fetch-pcr-historical":
        from_date = _sys.argv[2] if len(_sys.argv) >= 3 else "2022-01-01"
        print(f"=== Historical BankNifty PCR from NSE bhavcopy ({from_date} → today) ===")
        os.makedirs(DATA_DIR, exist_ok=True)
        fetch_pcr_historical(from_date=from_date)
        return

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

    # ── Dhan API indices — incremental (only fetch since last CSV date) ──────
    print("\n=== Dhan API indices (incremental) ===")
    for sec_id, name, csv_file in [("25", "BankNifty", "banknifty.csv"),
                                    ("13", "Nifty50",   "nifty50.csv")]:
        path      = f"{DATA_DIR}/{csv_file}"
        from_date = _last_csv_date(path) or FROM_DATE
        if from_date >= TO_DATE:
            print(f"  {name}: up to date (last row = {from_date})")
            continue
        df = fetch_dhan_index(sec_id, name, from_date, TO_DATE)
        _merge_and_save(path, df)
        if not df.empty:
            total = len(pd.read_csv(path))
            print(f"  → {csv_file}  (+{len(df)} new rows, {total} total)")

    # ── Yahoo Finance — all tickers fetched in parallel, incremental ─────────
    print("\n=== Yahoo Finance (parallel, incremental) ===")
    yf_sources = [
        ("^INDIAVIX", "India VIX",   "india_vix.csv"),
        ("^GSPC",     "S&P 500",     "sp500.csv"),
        ("^N225",     "Nikkei 225",  "nikkei.csv"),
        ("ES=F",      "S&P Futures", "sp500_futures.csv"),
        ("GC=F",      "Gold",        "gold.csv"),
        ("CL=F",      "Crude",       "crude.csv"),
        ("USDINR=X",  "USD/INR",     "usdinr.csv"),
        ("DX-Y.NYB",  "DXY",         "dxy.csv"),
        ("^TNX",      "US 10Y",      "us10y.csv"),
    ]

    def _update_yf(ticker, name, csv_file):
        path      = f"{DATA_DIR}/{csv_file}"
        from_date = _last_csv_date(path) or FROM_DATE
        if from_date >= TO_DATE:
            print(f"  {name}: up to date")
            return
        df = fetch_yfinance(ticker, name, from_date, TO_DATE)
        _merge_and_save(path, df)
        if not df.empty:
            total = len(pd.read_csv(path))
            print(f"  → {csv_file}  (+{len(df)} new rows, {total} total)")

    with ThreadPoolExecutor(max_workers=len(yf_sources)) as pool:
        futures = {pool.submit(_update_yf, t, n, f): n
                   for t, n, f in yf_sources}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"  {futures[fut]}: ERROR — {e}")

    # ── Live snapshots (today only) ──────────────────────────────────────────
    print("\n=== FII/DII live snapshot ===")
    fetch_fii_today()

    print("\n=== PCR live snapshot (BankNifty) ===")
    fetch_pcr_dhan_today()

    # NOTE: fetch_rollingoption (historical ATM option premiums) is intentionally
    # NOT run here — it takes several minutes and is only needed for backtesting.
    # Run manually: python3 data_fetcher.py --fetch-options

    print("\n=== All data files updated ===")


if __name__ == "__main__":
    main()
