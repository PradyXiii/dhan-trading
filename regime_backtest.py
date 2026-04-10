#!/usr/bin/env python3
"""
regime_backtest.py — TQI (Trend Quality Index) regime filter test
==================================================================
Tests whether skipping trades on choppy / directionless days improves
the BankNifty strategy's win rate and reduces drawdown.

Two components of TQI:
  Efficiency Ratio (ER)  — Kaufman's directional efficiency over N days
                           |net move| / sum(|daily moves|)
                           1.0 = perfect trend, 0.0 = pure chop
  ATR Ratio              — current ATR(14) / 50-day average ATR
                           > 1.0 = expanding range (momentum), < 1.0 = contracting

  TQI = ER × ATR_ratio   (raw, not normalised — higher = better trending day)

Tests thresholds:
  No filter   → baseline (all signals taken)
  TQI > 0.25  → skip mildly choppy days
  TQI > 0.40  → skip moderately choppy days
  TQI > 0.55  → skip all but strong trending days

Per-weekday breakdown included so you can see if the filter helps
more on Thursday (historically weak) than on Friday (historically strong).

Usage:
  python3 regime_backtest.py
  python3 regime_backtest.py --er-period 14   # default 10
"""

import os
import sys
import pandas as pd
import numpy as np
from math import floor, sqrt

DATA_DIR  = "data"
LOT_SIZE  = 30
SL_PCT    = 0.30
RISK_PCT  = 0.05
MAX_LOTS  = 20
DELTA     = 0.5
PREMIUM_K = 0.004

STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}
TRADE_WEEKDAYS = {0, 1, 2, 3, 4}

_EVENT_STRS = {
    "2021-10-08","2021-12-08",
    "2022-02-01","2022-02-10","2022-04-08","2022-06-08","2022-08-05",
    "2022-09-30","2022-12-07",
    "2023-02-01","2023-02-08","2023-04-06","2023-06-08","2023-08-10",
    "2023-10-06","2023-12-08",
    "2024-02-01","2024-02-08","2024-04-05","2024-06-07","2024-07-23",
    "2024-08-08","2024-10-09","2024-12-06",
    "2025-02-01","2025-02-07","2025-04-09","2025-06-06","2025-08-07",
    "2025-10-08","2025-12-05",
    "2026-02-01","2026-02-06",
}
EVENT_DATES = {pd.Timestamp(d).date() for d in _EVENT_STRS}


# ── Data loading + indicators ─────────────────────────────────────────────────

def load_and_compute(er_period=10):
    bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",  parse_dates=["date"])
    nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",    parse_dates=["date"])
    vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",  parse_dates=["date"])

    bn  = bn [["date","open","high","low","close"]].rename(
              columns={"open":"bn_open","high":"bn_high","low":"bn_low","close":"bn_close"})
    nf  = nf [["date","close"]].rename(columns={"close":"nf_close"})
    vix = vix[["date","close"]].rename(columns={"close":"vix_close"})

    df = bn.merge(nf, on="date", how="left").merge(vix, on="date", how="left")
    df = df.sort_values("date").reset_index(drop=True)
    df[["nf_close","vix_close"]] = df[["nf_close","vix_close"]].ffill(limit=3)
    df = df.dropna(subset=["bn_close","nf_close","vix_close"])

    # ── Strategy signals ──────────────────────────────────────────────────────
    df["ema20"]     = df["bn_close"].ewm(span=20, adjust=False).mean()
    df["trend5"]    = (df["bn_close"] - df["bn_close"].shift(5)) / df["bn_close"].shift(5) * 100
    df["vix_dir"]   = df["vix_close"] - df["vix_close"].shift(1)
    bn_chg          = (df["bn_close"] - df["bn_close"].shift(1)) / df["bn_close"].shift(1) * 100
    nf_chg          = (df["nf_close"] - df["nf_close"].shift(1)) / df["nf_close"].shift(1) * 100
    df["bn_nf_div"] = bn_chg - nf_chg

    # ── TQI components ────────────────────────────────────────────────────────
    # 1. Efficiency Ratio (Kaufman)
    #    ER = |close[N] - close[0]| / sum(|close[i] - close[i-1]| for i in 1..N)
    net_move   = (df["bn_close"] - df["bn_close"].shift(er_period)).abs()
    daily_move = df["bn_close"].diff().abs()
    path_len   = daily_move.rolling(er_period).sum()
    df["er"]   = net_move / path_len.replace(0, np.nan)

    # 2. ATR(14) and its 50-day rolling mean
    hl   = df["bn_high"] - df["bn_low"]
    hc   = (df["bn_high"] - df["bn_close"].shift(1)).abs()
    lc   = (df["bn_low"]  - df["bn_close"].shift(1)).abs()
    tr   = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr14           = tr.rolling(14).mean()
    atr50_avg       = atr14.rolling(50).mean()
    df["atr_ratio"] = atr14 / atr50_avg.replace(0, np.nan)

    # 3. TQI = ER × ATR ratio
    df["tqi"] = df["er"] * df["atr_ratio"]

    return df.dropna(subset=["ema20","trend5","vix_dir","bn_nf_div","er","atr_ratio","tqi"]).copy()


# ── Signal generator ──────────────────────────────────────────────────────────

def make_signals(df, threshold=1, tqi_cutoff=None):
    """Return signal DataFrame, optionally filtered by TQI >= tqi_cutoff."""
    trade = df[df["date"].dt.weekday.isin(TRADE_WEEKDAYS)].copy()
    rows = []
    for _, row in trade.iterrows():
        s_ema   = 1 if row["bn_close"] > row["ema20"] else -1
        s_trend = (1 if row["trend5"] > 1.0 else (-1 if row["trend5"] < -1.0 else 0))
        s_vix   = (1 if row["vix_dir"] < 0  else (-1 if row["vix_dir"] > 0   else 0))
        s_div   = (1 if row["bn_nf_div"] > 0.5 else (-1 if row["bn_nf_div"] < -0.5 else 0))
        score   = s_ema + s_trend + s_vix + s_div
        td      = row["date"].date()
        if td in EVENT_DATES:
            signal = "NONE"
        else:
            signal = ("CALL" if score >= threshold else ("PUT" if score <= -threshold else "NONE"))

        # Regime filter: override to NONE if TQI too low
        if tqi_cutoff is not None and row["tqi"] < tqi_cutoff:
            signal = "NONE"

        rows.append({
            "date":    row["date"],
            "weekday": row["date"].day_name(),
            "bn_open": row["bn_open"], "bn_high": row["bn_high"],
            "bn_low":  row["bn_low"],  "bn_close": row["bn_close"],
            "score":   score, "signal": signal, "tqi": row["tqi"],
        })
    return pd.DataFrame(rows)


# ── Trade simulator ───────────────────────────────────────────────────────────

def calculate_charges(premium, lots):
    pv  = lots * LOT_SIZE * premium
    b   = 40.0
    stt = 0.000625 * pv
    exc = 0.00053  * pv * 2
    clr = 0.000005 * pv * 2
    gst = 0.18 * (b + exc + clr)
    stmp= 0.00003  * pv
    sebi= 0.000001 * pv * 2
    return round(b + stt + exc + clr + gst + stmp + sebi, 2)


def simulate_trade(row, capital):
    signal  = row["signal"]
    weekday = row["weekday"]
    if signal not in ("CALL", "PUT"):
        return 0.0, "SKIP"

    bn_open  = float(row["bn_open"])
    bn_high  = float(row["bn_high"])
    bn_low   = float(row["bn_low"])
    bn_close = float(row["bn_close"])

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIP_CAP"

    lots    = min(MAX_LOTS, max(1, floor(capital * RISK_PCT / max_loss_1lot)))
    sl_pts  = (SL_PCT * premium) / DELTA
    tp_pts  = (rr * SL_PCT * premium) / DELTA
    charges = calculate_charges(premium, lots)

    if signal == "CALL":
        sl_hit = bn_low  <= bn_open - sl_pts
        tp_hit = bn_high >= bn_open + tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit: result = "WIN"
        elif sl_hit: result = "LOSS"
        else:
            gross = (bn_close - bn_open) * DELTA * lots * LOT_SIZE
            return round(gross - charges, 2), "PARTIAL"
    else:
        sl_hit = bn_high >= bn_open + sl_pts
        tp_hit = bn_low  <= bn_open - tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit: result = "WIN"
        elif sl_hit: result = "LOSS"
        else:
            gross = (bn_open - bn_close) * DELTA * lots * LOT_SIZE
            return round(gross - charges, 2), "PARTIAL"

    if result == "WIN":
        pnl = lots * LOT_SIZE * premium * rr * SL_PCT - charges
    else:
        pnl = -(lots * LOT_SIZE * premium * SL_PCT) - charges
    return round(pnl, 2), result


def run_backtest(sig_df):
    capital = STARTING_CAPITAL
    current_month = None
    rows = []
    for _, row in sig_df.iterrows():
        d  = row["date"]
        mk = (d.year, d.month)
        if current_month is None:
            current_month = mk
        elif mk != current_month:
            capital += MONTHLY_TOPUP
            current_month = mk

        cap_before = capital
        pnl, result = simulate_trade(row, capital)
        capital += pnl
        rows.append({
            "date":       d.date(),
            "weekday":    row["weekday"],
            "signal":     row["signal"],
            "tqi":        round(row["tqi"], 4),
            "result":     result,
            "pnl":        pnl,
            "cap_before": cap_before,
            "cap_after":  capital,
        })
    return pd.DataFrame(rows), capital


# ── Analysis helpers ──────────────────────────────────────────────────────────

def max_drawdown(trade_df):
    """Peak-to-trough drawdown on equity curve."""
    if trade_df.empty:
        return 0.0
    equity = trade_df["cap_after"].values
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        dd   = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)
    return round(max_dd, 1)


def summarise(trade_df, label):
    """Return a dict of summary stats for one scenario."""
    active = trade_df[trade_df["result"].isin(["WIN","LOSS","PARTIAL"])]
    if active.empty:
        return {"label": label, "trades": 0, "wr": 0, "net": 0,
                "avg": 0, "maxdd": 0, "end_cap": STARTING_CAPITAL}
    w  = (active["result"] == "WIN").sum()
    l  = (active["result"] == "LOSS").sum()
    wr = w / (w + l) * 100 if (w + l) > 0 else 0
    return {
        "label":   label,
        "trades":  len(active),
        "wr":      round(wr, 1),
        "net":     round(active["pnl"].sum(), 0),
        "avg":     round(active["pnl"].mean(), 0),
        "maxdd":   max_drawdown(trade_df),
        "end_cap": round(trade_df["cap_after"].iloc[-1], 0),
    }


def per_weekday(trade_df, label):
    """Win rate + net P&L by weekday for one scenario."""
    active = trade_df[trade_df["result"].isin(["WIN","LOSS","PARTIAL"])]
    if active.empty:
        return {}
    rows = {}
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        sub = active[active["weekday"] == day]
        if sub.empty:
            rows[day] = (0, 0, 0)
            continue
        w   = (sub["result"] == "WIN").sum()
        l   = (sub["result"] == "LOSS").sum()
        wr  = w / (w + l) * 100 if (w + l) > 0 else 0
        rows[day] = (len(sub), round(wr, 0), round(sub["pnl"].sum(), 0))
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    er_period = 10
    for i, a in enumerate(args):
        if a == "--er-period" and i+1 < len(args):
            er_period = int(args[i+1])

    print("=" * 72)
    print("  BankNifty — TQI Regime Filter Backtest")
    print(f"  Efficiency Ratio period: {er_period} days  |  ATR: 14 / 50-day avg")
    print("=" * 72)

    print("\nLoading data and computing TQI...")
    df = load_and_compute(er_period)
    print(f"  {len(df)} trading days  "
          f"({df['date'].min().date()} → {df['date'].max().date()})")

    # TQI distribution
    tqi = df["tqi"]
    print(f"\n  TQI distribution (on trading days with a signal):")
    for p in [10, 25, 50, 75, 90]:
        print(f"    p{p:02d} = {np.percentile(tqi, p):.3f}")

    # ── Build scenarios ───────────────────────────────────────────────────────
    cutoffs = [None, 0.25, 0.40, 0.55]
    labels  = ["No filter (baseline)", "TQI > 0.25", "TQI > 0.40", "TQI > 0.55"]

    results   = []
    trade_dfs = {}

    for cutoff, label in zip(cutoffs, labels):
        sigs = make_signals(df, threshold=1, tqi_cutoff=cutoff)
        td, _ = run_backtest(sigs)
        s = summarise(td, label)
        results.append(s)
        trade_dfs[label] = td

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'═'*72}")
    print(f"  SUMMARY — effect of TQI regime filter on all 5 weekdays")
    print(f"{'═'*72}")
    print(f"  {'Scenario':<26}  {'Trades':>7}  {'WR':>7}  {'Net P&L':>12}  "
          f"{'Avg/trade':>10}  {'Max DD':>8}  {'End Cap':>12}")
    print(f"  {'─'*88}")

    baseline = results[0]
    for r in results:
        trade_delta = r["trades"] - baseline["trades"]
        td_str = f"({trade_delta:+d})" if trade_delta != 0 else "      "
        print(f"  {r['label']:<26}  {r['trades']:>4} {td_str:<4}  "
              f"{r['wr']:>6.1f}%  ₹{r['net']:>10,.0f}  "
              f"₹{r['avg']:>8,.0f}  {r['maxdd']:>6.1f}%  ₹{r['end_cap']:>10,.0f}")

    print(f"{'─'*72}")

    # ── Per-weekday breakdown ─────────────────────────────────────────────────
    print(f"\n  PER-WEEKDAY WIN RATE  (trades | WR% | net P&L)")
    print(f"  {'Day':<12}", end="")
    for r in results:
        print(f"  {r['label'][:18]:<20}", end="")
    print()
    print(f"  {'─'*92}")

    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        print(f"  {day:<12}", end="")
        for label, td in trade_dfs.items():
            wd = per_weekday(td, label)
            cnt, wr, net = wd.get(day, (0, 0, 0))
            print(f"  {cnt:>3} | {wr:>3.0f}% | ₹{net:>7,.0f}  ", end="")
        print()

    print(f"\n{'═'*72}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n  VERDICT:")
    best = max(results[1:], key=lambda x: x["net"])
    base_net = baseline["net"]

    if best["net"] > base_net:
        gain_pct  = (best["net"] - base_net) / abs(base_net) * 100 if base_net != 0 else 0
        dd_change = best["maxdd"] - baseline["maxdd"]
        print(f"  Best filter: {best['label']}")
        print(f"  Net P&L:  ₹{base_net:,.0f} → ₹{best['net']:,.0f}  ({gain_pct:+.0f}%)")
        print(f"  Max DD:   {baseline['maxdd']:.1f}% → {best['maxdd']:.1f}%  "
              f"({'reduced ✓' if dd_change < 0 else 'increased ✗'})")
        print(f"  Win rate: {baseline['wr']:.1f}% → {best['wr']:.1f}%")
        trades_cut = baseline["trades"] - best["trades"]
        print(f"  Trades skipped: {trades_cut} ({trades_cut/baseline['trades']*100:.0f}% of all trades)")
        if gain_pct > 5 and (best["wr"] - baseline["wr"]) > 2:
            print(f"\n  ✓ TQI filter HELPS — better P&L AND higher win rate.")
            print(f"    Consider adding regime filter to auto_trader.py.")
        elif gain_pct > 5:
            print(f"\n  ~ TQI filter improves P&L but win rate is similar.")
            print(f"    Gain is from skipping bad trades, not improving WR.")
        else:
            print(f"\n  ~ Marginal improvement. TQI filter is not decisive here.")
    else:
        print(f"  No TQI cutoff improves on the unfiltered baseline.")
        print(f"  The strategy doesn't benefit from regime filtering at these thresholds.")
        print(f"  Possible reason: the 4-indicator signal already captures market regime.")

    print(f"{'='*72}")

    # Save results
    out_path = f"{DATA_DIR}/regime_backtest_log.csv"
    all_rows = []
    for label, td in trade_dfs.items():
        active = td[td["result"].isin(["WIN","LOSS","PARTIAL"])].copy()
        active["scenario"] = label
        all_rows.append(active)
    if all_rows:
        combined = pd.concat(all_rows, ignore_index=True)
        combined.to_csv(out_path, index=False)
        print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
