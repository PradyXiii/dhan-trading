import pandas as pd
import numpy as np
import os
import sys

DATA_DIR = "data"

# ── Change this to test different thresholds (2, 3, 4) ───────────────────────
SIGNAL_THRESHOLD = int(sys.argv[1]) if len(sys.argv) > 1 else 2


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data():
    """Load all data CSVs and merge on date using BankNifty calendar as master."""
    bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",     parse_dates=["date"])
    nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",       parse_dates=["date"])
    vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",     parse_dates=["date"])
    sp  = pd.read_csv(f"{DATA_DIR}/sp500.csv",         parse_dates=["date"])
    nk  = pd.read_csv(f"{DATA_DIR}/nikkei.csv",        parse_dates=["date"])
    spf = pd.read_csv(f"{DATA_DIR}/sp500_futures.csv", parse_dates=["date"])

    # Rename close columns to avoid clashes after merge
    bn  = bn [["date", "open", "high", "low", "close"]].rename(columns={"open": "bn_open",  "high": "bn_high",  "low": "bn_low",  "close": "bn_close"})
    nf  = nf [["date", "close"]].rename(columns={"close": "nf_close"})
    vix = vix[["date", "close"]].rename(columns={"close": "vix_close"})
    sp  = sp [["date", "close"]].rename(columns={"close": "sp_close"})
    nk  = nk [["date", "close"]].rename(columns={"close": "nk_close"})
    spf = spf[["date", "open", "close"]].rename(columns={"open": "spf_open", "close": "spf_close"})

    # BankNifty calendar is the master — merge others, forward-fill gaps (≤3 days)
    df = bn.copy()
    for other in [nf, vix, sp, nk, spf]:
        df = df.merge(other, on="date", how="left")

    # ── Optional Round-1 data sources (skip gracefully if not downloaded yet) ──
    pcr_path = f"{DATA_DIR}/pcr.csv"
    fii_path = f"{DATA_DIR}/fii_dii.csv"

    if os.path.exists(pcr_path):
        pcr = pd.read_csv(pcr_path, parse_dates=["date"])
        pcr = pcr[["date", "pcr"]].rename(columns={"pcr": "pcr"})
        df = df.merge(pcr, on="date", how="left")
    else:
        df["pcr"] = np.nan

    if os.path.exists(fii_path):
        fii = pd.read_csv(fii_path, parse_dates=["date"])
        fii = fii[["date", "fii_net"]].rename(columns={"fii_net": "fii_net"})
        df = df.merge(fii, on="date", how="left")
    else:
        df["fii_net"] = np.nan

    df = df.sort_values("date").reset_index(drop=True)
    df[["nf_close", "vix_close", "sp_close", "nk_close", "spf_open", "spf_close"]] = (
        df[["nf_close", "vix_close", "sp_close", "nk_close", "spf_open", "spf_close"]]
          .ffill(limit=3)
    )
    # Forward-fill PCR and FII only if files were loaded
    if not df["pcr"].isna().all():
        df["pcr"] = df["pcr"].ffill(limit=1)
    if not df["fii_net"].isna().all():
        df["fii_net"] = df["fii_net"].ffill(limit=1)

    return df.dropna(subset=["bn_close", "nf_close", "vix_close", "sp_close",
                              "nk_close", "spf_open", "spf_close"])


# ── Indicator computation ──────────────────────────────────────────────────────

def compute_rsi(series, period=14):
    """RSI using Wilder's smoothing (same as TradingView default)."""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_indicators(df):
    """Add all signal indicator columns to the dataframe."""
    d = df.copy()

    # ── Original 8 indicators ──────────────────────────────────────────────────

    # 1. EMA20 of BankNifty close
    d["ema20"] = d["bn_close"].ewm(span=20, adjust=False).mean()

    # 2. RSI14 of BankNifty close
    d["rsi14"] = compute_rsi(d["bn_close"], period=14)

    # 3. 5-day BankNifty trend (% change over 5 days)
    d["trend5"] = (d["bn_close"] - d["bn_close"].shift(5)) / d["bn_close"].shift(5) * 100

    # 4. VIX direction (today vs yesterday)
    d["vix_dir"] = d["vix_close"] - d["vix_close"].shift(1)

    # 5. Previous-day S&P500 % change
    d["sp500_chg"] = (d["sp_close"] - d["sp_close"].shift(1)) / d["sp_close"].shift(1) * 100

    # 6. Previous-day Nikkei % change
    d["nikkei_chg"] = (d["nk_close"] - d["nk_close"].shift(1)) / d["nk_close"].shift(1) * 100

    # 7. S&P futures overnight gap (today open vs yesterday close)
    d["spf_gap"] = (d["spf_open"] - d["spf_close"].shift(1)) / d["spf_close"].shift(1) * 100

    # 8. BankNifty vs Nifty50 divergence (same-day % change difference)
    bn_chg = (d["bn_close"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100
    nf_chg = (d["nf_close"] - d["nf_close"].shift(1)) / d["nf_close"].shift(1) * 100
    d["bn_nf_div"] = bn_chg - nf_chg

    # ── Round 1 additions ─────────────────────────────────────────────────────

    # 9. BankNifty 20-day historical volatility (annualised, in %)
    #    High vol (>20%) = uncertain/dangerous; Low vol (<12%) = calm/trending
    log_ret = np.log(d["bn_close"] / d["bn_close"].shift(1))
    d["hv20"] = log_ret.rolling(20).std() * np.sqrt(252) * 100

    # 10. BankNifty overnight gap (today's open vs yesterday's close)
    #     Proxy for GIFT Nifty pre-market signal
    d["bn_gap"] = (d["bn_open"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100

    # 11. PCR (Put-Call Ratio) — only if pcr.csv was loaded
    #     PCR > 1.2 = too much put buying = possible reversal up (contrarian bullish)
    #     PCR < 0.8 = too much call buying = possible reversal down (contrarian bearish)
    #     Trend signal: PCR rising = more puts = bearish pressure building

    # 12. FII net buy/sell (₹ crore) — only if fii_dii.csv was loaded
    #     FII net positive = institutional buying = bullish
    #     FII net negative = institutional selling = bearish

    return d.dropna(subset=["ema20", "rsi14", "trend5", "vix_dir",
                             "sp500_chg", "nikkei_chg", "spf_gap", "bn_nf_div",
                             "hv20", "bn_gap"])


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_row(row):
    """Score a single day's indicators. Returns (score, individual scores dict)."""
    s = {}

    # ── Original 8 ────────────────────────────────────────────────────────────

    # EMA20: close above/below EMA
    s["s_ema20"]     = 1 if row["bn_close"] > row["ema20"] else -1

    # RSI14: overbought/oversold/neutral
    s["s_rsi14"]     = 1 if row["rsi14"] > 55 else (-1 if row["rsi14"] < 45 else 0)

    # 5-day trend: strong move up/down
    s["s_trend5"]    = 1 if row["trend5"] > 1.0 else (-1 if row["trend5"] < -1.0 else 0)

    # VIX direction: falling = calm = bullish
    s["s_vix"]       = 1 if row["vix_dir"] < 0 else (-1 if row["vix_dir"] > 0 else 0)

    # S&P500 prev-day: positive = bullish
    s["s_sp500"]     = 1 if row["sp500_chg"] > 0 else -1

    # Nikkei prev-day: positive = bullish
    s["s_nikkei"]    = 1 if row["nikkei_chg"] > 0 else -1

    # S&P futures gap: positive gap = bullish
    s["s_spf_gap"]   = 1 if row["spf_gap"] > 0.2 else (-1 if row["spf_gap"] < -0.2 else 0)

    # BN-NF divergence: BN outperforming = bullish
    s["s_bn_nf_div"] = 1 if row["bn_nf_div"] > 0.5 else (-1 if row["bn_nf_div"] < -0.5 else 0)

    # ── Round 1 additions ─────────────────────────────────────────────────────

    # HV20: low vol = trend likely to continue; high vol = avoid or fade
    # Neutral at mid-vol (12–20%) — these thresholds calibrated for BankNifty
    s["s_hv20"]      = 1 if row["hv20"] < 12.0 else (-1 if row["hv20"] > 20.0 else 0)

    # BN overnight gap: positive gap = bullish open; negative = bearish
    # Threshold ±0.3% — small gaps are noise
    s["s_bn_gap"]    = 1 if row["bn_gap"] > 0.3 else (-1 if row["bn_gap"] < -0.3 else 0)

    # PCR (if available): contrarian — high PCR = fear = bullish; low PCR = greed = bearish
    if not pd.isna(row.get("pcr", float("nan"))):
        s["s_pcr"]   = 1 if row["pcr"] > 1.2 else (-1 if row["pcr"] < 0.8 else 0)

    # FII net (if available): FII buying = bullish; selling = bearish
    # Threshold ±500 crore to filter out small/noise flows
    if not pd.isna(row.get("fii_net", float("nan"))):
        s["s_fii"]   = 1 if row["fii_net"] > 500 else (-1 if row["fii_net"] < -500 else 0)

    total = sum(s.values())
    return total, s


def generate_signals(df):
    """Filter to Tuesdays and Fridays, score each day, return signals DataFrame."""
    # weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    trade_days = df[df["date"].dt.weekday.isin([1, 4])].copy()
    trade_days["weekday"] = trade_days["date"].dt.day_name()

    rows = []
    for _, row in trade_days.iterrows():
        score, s = score_row(row)

        signal = "CALL" if score >= SIGNAL_THRESHOLD else ("PUT" if score <= -SIGNAL_THRESHOLD else "NONE")

        row_data = {
            "date":       row["date"].date(),
            "weekday":    row["weekday"],
            "bn_close":   round(row["bn_close"], 2),
            "ema20":      round(row["ema20"], 2),
            "rsi14":      round(row["rsi14"], 2),
            "trend5":     round(row["trend5"], 2),
            "vix_dir":    round(row["vix_dir"], 2),
            "sp500_chg":  round(row["sp500_chg"], 2),
            "nikkei_chg": round(row["nikkei_chg"], 2),
            "spf_gap":    round(row["spf_gap"], 2),
            "bn_nf_div":  round(row["bn_nf_div"], 2),
            "hv20":       round(row["hv20"], 2),
            "bn_gap":     round(row["bn_gap"], 2),
            **{k: v for k, v in s.items()},
            "score":      score,
            "signal":     signal,
        }

        # Add optional columns if data was available
        if "pcr" in row.index and not pd.isna(row.get("pcr", float("nan"))):
            row_data["pcr"] = round(row["pcr"], 2)
        if "fii_net" in row.index and not pd.isna(row.get("fii_net", float("nan"))):
            row_data["fii_net"] = round(row["fii_net"], 0)

        rows.append(row_data)

    return pd.DataFrame(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Loading data...  [Signal threshold: ±{SIGNAL_THRESHOLD}]")
    df = load_data()
    print(f"  Merged dataset: {len(df)} trading days  "
          f"({df['date'].min().date()} to {df['date'].max().date()})")

    # Report which Round-1 data sources are active
    has_pcr = not df["pcr"].isna().all()
    has_fii = not df["fii_net"].isna().all()
    active_indicators = 10 + (1 if has_pcr else 0) + (1 if has_fii else 0)
    print(f"  Active indicators: {active_indicators}/12"
          f"  [PCR: {'✓' if has_pcr else '✗ (pcr.csv missing)'}]"
          f"  [FII/DII: {'✓' if has_fii else '✗ (fii_dii.csv missing)'}]")

    print("Computing indicators...")
    df = compute_indicators(df)

    print("Generating signals for Tuesdays and Fridays...")
    signals = generate_signals(df)

    # Save
    out_path = f"{DATA_DIR}/signals.csv"
    signals.to_csv(out_path, index=False)

    # Summary
    total  = len(signals)
    calls  = (signals["signal"] == "CALL").sum()
    puts   = (signals["signal"] == "PUT").sum()
    nones  = (signals["signal"] == "NONE").sum()

    print(f"\n{'='*50}")
    print(f"  Trade days scanned : {total}")
    print(f"  CALL signals       : {calls}  ({calls/total*100:.1f}%)")
    print(f"  PUT  signals       : {puts}   ({puts/total*100:.1f}%)")
    print(f"  NO TRADE           : {nones}  ({nones/total*100:.1f}%)")
    print(f"{'='*50}")
    print(f"\nSaved → {out_path}")

    # Show first and last 5 signals
    print("\nFirst 5 signals:")
    print(signals[["date", "weekday", "score", "signal"]].head().to_string(index=False))
    print("\nLast 5 signals:")
    print(signals[["date", "weekday", "score", "signal"]].tail().to_string(index=False))


if __name__ == "__main__":
    main()
