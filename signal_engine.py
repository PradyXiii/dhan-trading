import pandas as pd
import numpy as np
import os
import sys

DATA_DIR = "data"

# ── Change this to test different thresholds (1, 2, 3, 4) ────────────────────
SIGNAL_THRESHOLD = int(sys.argv[1]) if len(sys.argv) > 1 else 1


# ── Event calendar — hard NO-TRADE override ───────────────────────────────────
# These are the announcement/decision days only (not the full meeting period).
# Trades on these dates are forced to NONE regardless of score.
# Sources: RBI MPC calendar + Union Budget dates (2021–2026)

_RBI_MPC_DECISION_DAYS = {
    # 2021
    "2021-10-08", "2021-12-08",
    # 2022
    "2022-02-10", "2022-04-08", "2022-06-08", "2022-08-05",
    "2022-09-30", "2022-12-07",
    # 2023
    "2023-02-08", "2023-04-06", "2023-06-08", "2023-08-10",
    "2023-10-06", "2023-12-08",
    # 2024
    "2024-02-08", "2024-04-05", "2024-06-07", "2024-08-08",
    "2024-10-09", "2024-12-06",
    # 2025
    "2025-02-07", "2025-04-09", "2025-06-06", "2025-08-07",
    "2025-10-08", "2025-12-05",
    # 2026
    "2026-02-06",
}

_BUDGET_DAYS = {
    "2022-02-01",
    "2023-02-01",
    "2024-02-01",   # interim budget
    "2024-07-23",   # full Union Budget 2024
    "2025-02-01",
    "2026-02-01",
}

EVENT_DATES = {
    pd.Timestamp(d).date()
    for d in (_RBI_MPC_DECISION_DAYS | _BUDGET_DAYS)
}


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data():
    """Load all CSVs and merge on date using BankNifty calendar as master."""
    bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",     parse_dates=["date"])
    nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",       parse_dates=["date"])
    vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",     parse_dates=["date"])
    sp  = pd.read_csv(f"{DATA_DIR}/sp500.csv",         parse_dates=["date"])
    nk  = pd.read_csv(f"{DATA_DIR}/nikkei.csv",        parse_dates=["date"])
    spf = pd.read_csv(f"{DATA_DIR}/sp500_futures.csv", parse_dates=["date"])

    bn  = bn [["date","open","high","low","close"]].rename(
              columns={"open":"bn_open","high":"bn_high","low":"bn_low","close":"bn_close"})
    nf  = nf [["date","close"]].rename(columns={"close":"nf_close"})
    vix = vix[["date","close"]].rename(columns={"close":"vix_close"})
    sp  = sp [["date","close"]].rename(columns={"close":"sp_close"})
    nk  = nk [["date","close"]].rename(columns={"close":"nk_close"})
    spf = spf[["date","open","close"]].rename(columns={"open":"spf_open","close":"spf_close"})

    df = bn.copy()
    for other in [nf, vix, sp, nk, spf]:
        df = df.merge(other, on="date", how="left")

    # ── Optional data files — all gracefully skipped if missing ──────────────
    optional = {
        "pcr":          (f"{DATA_DIR}/pcr.csv",          ["date", "pcr"]),
        "max_pain":     (f"{DATA_DIR}/max_pain.csv",      ["date", "max_pain"]),
        "oi_buildup":   (f"{DATA_DIR}/oi_buildup.csv",    ["date", "put_oi_chg", "call_oi_chg"]),
        "fii_fo":       (f"{DATA_DIR}/fii_fo.csv",        ["date", "fii_net_futures"]),
        "fii_dii":      (f"{DATA_DIR}/fii_dii.csv",       ["date", "fii_net"]),
    }
    loaded = {}
    for key, (path, cols) in optional.items():
        if os.path.exists(path):
            tmp = pd.read_csv(path, parse_dates=["date"])
            available = [c for c in cols if c in tmp.columns]
            df = df.merge(tmp[available], on="date", how="left")
            loaded[key] = True
        else:
            for col in cols[1:]:          # skip "date"
                if col not in df.columns:
                    df[col] = np.nan
            loaded[key] = False

    df = df.sort_values("date").reset_index(drop=True)

    # Forward-fill global market data (handles weekends/holidays)
    ff_cols = ["nf_close", "vix_close", "sp_close", "nk_close", "spf_open", "spf_close"]
    df[ff_cols] = df[ff_cols].ffill(limit=3)

    # Forward-fill optional data (1 day only — stale beyond that)
    for col in ["pcr", "max_pain", "put_oi_chg", "call_oi_chg",
                "fii_net_futures", "fii_net"]:
        if col in df.columns and not df[col].isna().all():
            df[col] = df[col].ffill(limit=1)

    return df.dropna(subset=["bn_close", "nf_close", "vix_close",
                              "sp_close", "nk_close", "spf_open", "spf_close"]), loaded


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_rsi(series, period=14):
    """RSI using Wilder's smoothing (same as TradingView default)."""
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_indicators(df):
    """Add all indicator columns to the dataframe."""
    d = df.copy()

    # ── Core 8 ────────────────────────────────────────────────────────────────
    d["ema20"]     = d["bn_close"].ewm(span=20, adjust=False).mean()
    d["rsi14"]     = compute_rsi(d["bn_close"], period=14)
    d["trend5"]    = (d["bn_close"] - d["bn_close"].shift(5)) / d["bn_close"].shift(5) * 100
    d["vix_dir"]   = d["vix_close"] - d["vix_close"].shift(1)
    d["sp500_chg"] = (d["sp_close"] - d["sp_close"].shift(1)) / d["sp_close"].shift(1) * 100
    d["nikkei_chg"]= (d["nk_close"] - d["nk_close"].shift(1)) / d["nk_close"].shift(1) * 100
    d["spf_gap"]   = (d["spf_open"] - d["spf_close"].shift(1)) / d["spf_close"].shift(1) * 100
    bn_chg         = (d["bn_close"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100
    nf_chg         = (d["nf_close"] - d["nf_close"].shift(1)) / d["nf_close"].shift(1) * 100
    d["bn_nf_div"] = bn_chg - nf_chg

    # ── Round 1: HV20 + BN overnight gap ──────────────────────────────────────
    log_ret    = np.log(d["bn_close"] / d["bn_close"].shift(1))
    d["hv20"]  = log_ret.rolling(20).std() * np.sqrt(252) * 100
    d["bn_gap"]= (d["bn_open"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100

    # ── Round 2: IV Rank (52-week HV percentile) — no new data needed ─────────
    # IV Rank < 30 = vol is low relative to past year = calm = +1 (trends work)
    # IV Rank > 70 = vol is elevated = stressed market = -1 (chop/reversal risk)
    hv_min       = d["hv20"].rolling(252, min_periods=60).min()
    hv_max       = d["hv20"].rolling(252, min_periods=60).max()
    hv_range     = (hv_max - hv_min).replace(0, np.nan)
    d["iv_rank"] = (d["hv20"] - hv_min) / hv_range * 100

    return d.dropna(subset=["ema20", "rsi14", "trend5", "vix_dir",
                             "sp500_chg", "nikkei_chg", "spf_gap", "bn_nf_div",
                             "hv20", "bn_gap"])


# ── Scoring ───────────────────────────────────────────────────────────────────

def _get(row, col, default=float("nan")):
    """Safe column access that works for both dict-like and Series rows."""
    try:
        v = row[col] if col in row.index else default
        return default if pd.isna(v) else v
    except Exception:
        return default


def score_row(row):
    """Score all available indicators. Returns (total_score, {indicator: score})."""
    s = {}

    # ── Core 8 ────────────────────────────────────────────────────────────────
    s["s_ema20"]    = 1  if row["bn_close"] > row["ema20"] else -1
    s["s_rsi14"]    = (1  if row["rsi14"]     > 55   else
                      (-1 if row["rsi14"]     < 45   else 0))
    s["s_trend5"]   = (1  if row["trend5"]    > 1.0  else
                      (-1 if row["trend5"]    < -1.0 else 0))
    s["s_vix"]      = (1  if row["vix_dir"]   < 0    else
                      (-1 if row["vix_dir"]   > 0    else 0))
    s["s_sp500"]    = 1  if row["sp500_chg"]  > 0    else -1
    s["s_nikkei"]   = 1  if row["nikkei_chg"] > 0    else -1
    s["s_spf_gap"]  = (1  if row["spf_gap"]   > 0.2  else
                      (-1 if row["spf_gap"]   < -0.2 else 0))
    s["s_bn_nf_div"]= (1  if row["bn_nf_div"] > 0.5  else
                      (-1 if row["bn_nf_div"] < -0.5 else 0))

    # ── Round 1: HV20 + BN gap ────────────────────────────────────────────────
    s["s_hv20"]    = (1  if row["hv20"]   < 12.0 else
                     (-1 if row["hv20"]   > 20.0 else 0))
    s["s_bn_gap"]  = (1  if row["bn_gap"] > 0.3  else
                     (-1 if row["bn_gap"] < -0.3  else 0))

    # ── Round 2: IV Rank (always computed) ───────────────────────────────────
    iv = _get(row, "iv_rank")
    if not pd.isna(iv):
        s["s_iv_rank"] = 1 if iv < 30 else (-1 if iv > 70 else 0)

    # ── Optional (need bhavcopy download) ────────────────────────────────────

    # PCR: contrarian — high PCR (fear) = bullish; low PCR (greed) = bearish
    pcr = _get(row, "pcr")
    if not pd.isna(pcr):
        s["s_pcr"] = 1 if pcr > 1.2 else (-1 if pcr < 0.8 else 0)

    # OI direction: CALL OI building faster = bullish; PUT OI faster = bearish
    call_oi_chg = _get(row, "call_oi_chg")
    put_oi_chg  = _get(row, "put_oi_chg")
    if not (pd.isna(call_oi_chg) or pd.isna(put_oi_chg)):
        s["s_oi_dir"] = (1  if call_oi_chg > put_oi_chg else
                        (-1 if put_oi_chg  > call_oi_chg else 0))

    # Max Pain: BN price below max pain = bullish (market may drift up to pain level)
    max_pain = _get(row, "max_pain")
    if not pd.isna(max_pain) and max_pain > 0:
        dist_pct = (row["bn_close"] - max_pain) / max_pain * 100
        s["s_max_pain"] = (1  if dist_pct < -1.0 else    # price below max pain → drift up
                          (-1 if dist_pct >  1.0 else 0)) # price above max pain → drift down

    # FII F&O net futures (from fetch_round2_data.py)
    fii_fo = _get(row, "fii_net_futures")
    if not pd.isna(fii_fo):
        s["s_fii_fo"] = 1 if fii_fo > 0 else (-1 if fii_fo < 0 else 0)

    # FII cash market (from fii_dii.csv, manual download)
    fii_cash = _get(row, "fii_net")
    if not pd.isna(fii_cash):
        s["s_fii_cash"] = 1 if fii_cash > 500 else (-1 if fii_cash < -500 else 0)

    return sum(s.values()), s


def generate_signals(df):
    """Filter to Tuesdays and Fridays, score, apply event filter, return DataFrame."""
    trade_days = df[df["date"].dt.weekday.isin([1, 4])].copy()
    trade_days["weekday"] = trade_days["date"].dt.day_name()

    rows = []
    for _, row in trade_days.iterrows():
        score, s = score_row(row)
        trade_date = row["date"].date()

        # ── Event calendar hard filter ─────────────────────────────────────────
        if trade_date in EVENT_DATES:
            signal = "NONE"
            event_flag = True
        else:
            signal     = ("CALL" if score >= SIGNAL_THRESHOLD else
                          ("PUT"  if score <= -SIGNAL_THRESHOLD else "NONE"))
            event_flag = False

        row_data = {
            "date":       trade_date,
            "weekday":    row["weekday"],
            "event_day":  event_flag,
            "bn_close":   round(row["bn_close"], 2),
            "ema20":      round(row["ema20"],    2),
            "rsi14":      round(row["rsi14"],    2),
            "trend5":     round(row["trend5"],   2),
            "vix_dir":    round(row["vix_dir"],  2),
            "sp500_chg":  round(row["sp500_chg"],  2),
            "nikkei_chg": round(row["nikkei_chg"], 2),
            "spf_gap":    round(row["spf_gap"],    2),
            "bn_nf_div":  round(row["bn_nf_div"],  2),
            "hv20":       round(row["hv20"],    2),
            "bn_gap":     round(row["bn_gap"],  2),
            **{k: v for k, v in s.items()},
            "score":  score,
            "signal": signal,
        }

        # Optional columns — only add if data exists
        for col, fmt in [("iv_rank", 1), ("pcr", 2), ("max_pain", 0),
                         ("put_oi_chg", 0), ("call_oi_chg", 0),
                         ("fii_net_futures", 0), ("fii_net", 0)]:
            v = _get(row, col)
            if not pd.isna(v):
                row_data[col] = round(v, fmt)

        rows.append(row_data)

    return pd.DataFrame(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Loading data...  [Signal threshold: ±{SIGNAL_THRESHOLD}]")
    df, loaded = load_data()
    print(f"  Merged dataset : {len(df)} trading days  "
          f"({df['date'].min().date()} to {df['date'].max().date()})")

    # Count active indicators
    always_on  = 11          # 8 core + HV20 + BN gap + IV Rank (computed)
    optional   = {
        "PCR":            loaded.get("pcr",      False),
        "OI direction":   loaded.get("oi_buildup", False),
        "Max Pain":       loaded.get("oi_buildup", False),  # same file
        "FII F&O":        loaded.get("fii_fo",   False),
        "FII cash":       loaded.get("fii_dii",  False),
    }
    active_opt = sum(optional.values())
    total_ind  = always_on + active_opt

    print(f"  Active indicators: {total_ind}/16")
    for name, status in optional.items():
        mark = "✓" if status else "✗"
        print(f"    {mark} {name}"
              + ("" if status else "  ← run python3 fetch_round2_data.py"))

    event_count = sum(1 for d in pd.date_range(df["date"].min(),
                                               df["date"].max(), freq="B")
                     if d.date() in EVENT_DATES)
    print(f"  Event filter    : {event_count} event days in period "
          f"(RBI MPC + Budget) → forced NONE")

    print("\nComputing indicators...")
    df = compute_indicators(df)

    print("Generating signals for Tuesdays and Fridays...")
    signals = generate_signals(df)

    # Embed threshold for backtest_engine to read back
    signals["threshold"] = SIGNAL_THRESHOLD
    signals.to_csv(f"{DATA_DIR}/signals.csv", index=False)
    signals.drop(columns=["threshold"], inplace=True)

    # Summary
    total      = len(signals)
    calls      = (signals["signal"] == "CALL").sum()
    puts       = (signals["signal"] == "PUT").sum()
    nones      = (signals["signal"] == "NONE").sum()
    event_days = signals["event_day"].sum() if "event_day" in signals else 0

    print(f"\n{'='*52}")
    print(f"  Trade days scanned : {total}")
    print(f"  CALL signals       : {calls}  ({calls/total*100:.1f}%)")
    print(f"  PUT  signals       : {puts}   ({puts/total*100:.1f}%)")
    print(f"  NO TRADE (score)   : {nones - event_days}  ({(nones-event_days)/total*100:.1f}%)")
    print(f"  NO TRADE (event)   : {event_days}  ({event_days/total*100:.1f}%)")
    print(f"{'='*52}")
    print(f"\nSaved → {DATA_DIR}/signals.csv")

    print("\nFirst 5 signals:")
    print(signals[["date", "weekday", "event_day", "score", "signal"]].head().to_string(index=False))
    print("\nLast 5 signals:")
    print(signals[["date", "weekday", "event_day", "score", "signal"]].tail().to_string(index=False))


if __name__ == "__main__":
    main()
