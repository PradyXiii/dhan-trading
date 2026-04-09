#!/usr/bin/env python3
"""
indicator_attribution.py — Which signal source drives the edge?
===============================================================
Runs the full 4.5-year backtest using each indicator ALONE as the signal,
then shows how each compares to the combined 10-indicator signal.

Answers:
  - Is yesterday's close (all 10 combined) better than just US market close?
  - How much does GIFT Nifty proxy (bn_gap) contribute on its own?
  - Which single indicator has the most predictive power?
  - Are some indicators actually dragging down performance?

Signal categories tested:
  Individual :  each of the 10 indicator scores alone
  Pre-market  :  bn_gap + spf_gap + s_sp500 (overnight / morning signals)
  Technical   :  ema20 + rsi14 + trend5 + hv20 (pure chart signals)
  Macro       :  sp500 + nikkei + spf_gap (global market signals)
  Combined    :  all 10 (baseline — what we currently use)

Usage:
    python3 indicator_attribution.py
    python3 indicator_attribution.py --verbose   # show per-day breakdown for each
"""

import sys
import os
import pandas as pd
import numpy as np
from math import sqrt, floor

DATA_DIR         = "data"
LOT_SIZE         = 30
SL_PCT           = 0.30
RISK_PCT         = 0.05
MAX_LOTS         = 20
PREMIUM_K        = 0.004
DELTA            = 0.5
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}

VERBOSE = "--verbose" in sys.argv


# ── Transaction costs (mirrors backtest_engine.py) ─────────────────────────────

def calculate_charges(premium, lots):
    pv         = lots * LOT_SIZE * premium
    brokerage  = 40.0
    stt        = 0.000625 * pv
    exchange   = 0.00053  * pv * 2
    clearing   = 0.000005 * pv * 2
    gst        = 0.18 * (brokerage + exchange + clearing)
    stamp_duty = 0.00003  * pv
    sebi       = 0.000001 * pv * 2
    return round(brokerage + stt + exchange + clearing + gst + stamp_duty + sebi, 2)


# ── Trade simulator ────────────────────────────────────────────────────────────

def simulate_trade(row, bn_ohlcv, capital):
    d       = row["date"]
    weekday = row["weekday"]
    signal  = row["signal"]

    if d not in bn_ohlcv.index:
        return 0.0, "SKIPPED", 0, 0.0

    bar      = bn_ohlcv.loc[d]
    bn_open  = bar["open"]
    bn_high  = bar["high"]
    bn_low   = bar["low"]
    bn_close = bar["close"]

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * (dte ** 0.5)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIPPED_LOW_CAPITAL", 0, premium

    lots   = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))
    sl_pts = (SL_PCT * premium) / DELTA
    tp_pts = (rr * SL_PCT * premium) / DELTA

    if signal == "CALL":
        sl_level = bn_open - sl_pts
        tp_level = bn_open + tp_pts
        sl_hit   = bn_low  <= sl_level
        tp_hit   = bn_high >= tp_level
        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross = (bn_close - bn_open) * DELTA * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            return round(gross - charges, 2), "PARTIAL", lots, premium
    else:
        sl_level = bn_open + sl_pts
        tp_level = bn_open - tp_pts
        sl_hit   = bn_high >= sl_level
        tp_hit   = bn_low  <= tp_level
        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross = (bn_open - bn_close) * DELTA * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            return round(gross - charges, 2), "PARTIAL", lots, premium

    charges = calculate_charges(premium, lots)
    if result == "WIN":
        pnl =  lots * LOT_SIZE * premium * rr * SL_PCT - charges
    else:
        pnl = -lots * LOT_SIZE * premium * SL_PCT - charges

    return round(pnl, 2), result, lots, premium


# ── Backtest runner ────────────────────────────────────────────────────────────

def run_backtest(signal_rows, bn_ohlcv):
    """Run full backtest given a list of (date, weekday, signal) dicts."""
    capital       = STARTING_CAPITAL
    current_month = None
    wins = losses = 0
    all_caps      = []
    total_pnl     = 0.0

    for row in signal_rows:
        d         = row["date"]
        month_key = (d.year, d.month)

        if current_month is None:
            current_month = month_key
        elif month_key != current_month:
            capital      += MONTHLY_TOPUP
            current_month = month_key

        pnl, result, lots, _ = simulate_trade(row, bn_ohlcv, capital)
        capital   += pnl
        total_pnl += pnl
        all_caps.append(capital)

        if result == "WIN":    wins   += 1
        elif result == "LOSS": losses += 1

    total  = wins + losses
    wr     = wins / total * 100 if total > 0 else 0.0
    cap_s  = pd.Series(all_caps) if all_caps else pd.Series([STARTING_CAPITAL])
    max_dd = ((cap_s - cap_s.cummax()) / cap_s.cummax() * 100).min()

    # net P&L = ending capital − all injected cash
    months_elapsed = 0
    if signal_rows:
        first = signal_rows[0]["date"]
        last  = signal_rows[-1]["date"]
        months_elapsed = (last.year * 12 + last.month) - (first.year * 12 + first.month)
    injected = STARTING_CAPITAL + months_elapsed * MONTHLY_TOPUP
    net_pnl  = capital - injected

    return {
        "trades": total,
        "wins":   wins,
        "losses": losses,
        "wr":     wr,
        "net_pnl": net_pnl,
        "end_cap": capital,
        "max_dd":  max_dd,
    }


# ── Signal builders ────────────────────────────────────────────────────────────

def build_signals_from_col(full_df, score_col, threshold=1):
    """
    Generate CALL/PUT signals using only one indicator column.
    score_col must be one of: s_ema20, s_rsi14, s_trend5, s_vix,
    s_sp500, s_nikkei, s_spf_gap, s_bn_nf_div, s_hv20, s_bn_gap
    Each of those is already +1 / 0 / -1.
    """
    rows = []
    for _, r in full_df.iterrows():
        v = r.get(score_col, 0)
        if pd.isna(v):
            v = 0
        v = int(v)
        if v >= threshold:
            sig = "CALL"
        elif v <= -threshold:
            sig = "PUT"
        else:
            continue   # skip NONE rows
        rows.append({"date": r["date"], "weekday": r["weekday"], "signal": sig})
    return rows


def build_signals_from_combo(full_df, score_cols, threshold=1):
    """Sum multiple indicator columns and generate signal if |sum| >= threshold."""
    rows = []
    for _, r in full_df.iterrows():
        total = sum(int(r.get(c, 0) or 0) for c in score_cols)
        if total >= threshold:
            sig = "CALL"
        elif total <= -threshold:
            sig = "PUT"
        else:
            continue
        rows.append({"date": r["date"], "weekday": r["weekday"], "signal": sig})
    return rows


def build_signals_combined(full_df, threshold=1):
    """All 10 indicators combined — baseline."""
    all_score_cols = ["s_ema20", "s_rsi14", "s_trend5", "s_vix",
                      "s_sp500", "s_nikkei", "s_spf_gap", "s_bn_nf_div",
                      "s_hv20", "s_bn_gap"]
    return build_signals_from_combo(full_df, all_score_cols, threshold)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load signals.csv (has per-indicator scores) and banknifty OHLCV
    try:
        sig_df = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        sig_df = sig_df.drop(columns=["threshold"], errors="ignore")
    except FileNotFoundError:
        print("ERROR: data/signals.csv not found. Run: python3 signal_engine.py")
        sys.exit(1)

    try:
        bn = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        bn_ohlcv = bn.set_index("date")
    except FileNotFoundError:
        print("ERROR: data/banknifty.csv not found. Run: python3 data_fetcher.py")
        sys.exit(1)

    # signals.csv includes NONE rows too — keep all for indicator reconstruction
    # (each row has s_ema20, s_rsi14, ... columns regardless of signal)
    all_rows = sig_df.copy()
    date_range = f"{all_rows['date'].min().date()}  →  {all_rows['date'].max().date()}"

    # ── Indicator definitions ──────────────────────────────────────────────────
    individual = {
        #  label                    col           description
        "EMA20 (trend)"        : ("s_ema20",    "BN above/below 20-day EMA"),
        "RSI14 (momentum)"     : ("s_rsi14",    "RSI > 55 bullish / < 45 bearish"),
        "5-day trend"          : ("s_trend5",   "BN 5-day return > ±1%"),
        "VIX direction"        : ("s_vix",      "VIX falling=bull / rising=bear"),
        "US close (S&P500)"    : ("s_sp500",    "S&P500 prev-day return direction"),
        "Nikkei close"         : ("s_nikkei",   "Nikkei prev-day return direction"),
        "S&P Futures gap"      : ("s_spf_gap",  "S&P500 futures overnight gap"),
        "BN vs Nifty div"      : ("s_bn_nf_div","BN outperforms/underperforms NF50"),
        "HV20 (volatility)"    : ("s_hv20",     "Low vol=bull / high vol=bear"),
        "BN gap at open ★"    : ("s_bn_gap",   "BankNifty overnight gap — GIFT Nifty proxy"),
    }

    combos = {
        "Pre-market signals"   : ["s_bn_gap", "s_spf_gap", "s_sp500"],
        "Technical signals"    : ["s_ema20", "s_rsi14", "s_trend5", "s_hv20"],
        "Macro signals"        : ["s_sp500", "s_nikkei", "s_spf_gap"],
        "All 10 combined ★"   : ["s_ema20", "s_rsi14", "s_trend5", "s_vix",
                                  "s_sp500", "s_nikkei", "s_spf_gap", "s_bn_nf_div",
                                  "s_hv20", "s_bn_gap"],
        # ── Targeted tests based on attribution results ───────────────────────
        # Remove the 4 clear losers (BN gap, Nikkei, HV20, S&P futures) + S&P close
        "No macro (5 India-only)": ["s_ema20", "s_rsi14", "s_trend5",
                                    "s_vix", "s_bn_nf_div"],
        # Remove just the global macro (keep HV20 as vol filter)
        "No macro (6 India+vol)" : ["s_ema20", "s_rsi14", "s_trend5",
                                    "s_vix", "s_bn_nf_div", "s_hv20"],
        # Top 2 standalone performers only
        "Top 2: trend5 + BN-NF"  : ["s_trend5", "s_bn_nf_div"],
        # Top 3
        "Top 3: +EMA20"          : ["s_trend5", "s_bn_nf_div", "s_ema20"],
        # Top 3 + VIX
        "Top 4: +VIX"            : ["s_trend5", "s_bn_nf_div", "s_ema20", "s_vix"],
        # Top 4 + RSI (all technical, no macro, no HV20, no BN gap)
        "Top 5: +RSI14 (no macro)": ["s_trend5", "s_bn_nf_div", "s_ema20",
                                      "s_vix", "s_rsi14"],
    }

    # ── Run individual indicator backtests ────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  INDICATOR ATTRIBUTION ANALYSIS  ({date_range})")
    print(f"  Which signal source drives the edge?")
    print(f"{'═'*80}")
    print(f"  {'Indicator':<28} {'Trades':>7} {'WR':>7} {'Net P&L':>12} {'End Cap':>12} {'MaxDD':>7}")
    print(f"  {'─'*76}")

    results = {}

    for label, (col, desc) in individual.items():
        if col not in all_rows.columns:
            print(f"  {label:<28}  — column '{col}' not in signals.csv (re-run signal_engine.py)")
            continue
        rows = build_signals_from_col(all_rows, col, threshold=1)
        if not rows:
            continue
        r = run_backtest(rows, bn_ohlcv)
        results[label] = r
        marker = " ◀" if label.endswith("★") else ""
        print(f"  {label:<28} {r['trades']:>7} {r['wr']:>6.1f}%  "
              f"₹{r['net_pnl']:>10,.0f}  ₹{r['end_cap']:>10,.0f}  {r['max_dd']:>6.1f}%{marker}")

    print(f"\n  {'─'*76}")
    print(f"  COMBINATIONS")
    print(f"  {'─'*76}")

    for label, cols in combos.items():
        available = [c for c in cols if c in all_rows.columns]
        if not available:
            continue
        rows = build_signals_from_combo(all_rows, available, threshold=1)
        if not rows:
            continue
        r = run_backtest(rows, bn_ohlcv)
        results[label] = r
        marker = " ◀" if label.endswith("★") else ""
        print(f"  {label:<28} {r['trades']:>7} {r['wr']:>6.1f}%  "
              f"₹{r['net_pnl']:>10,.0f}  ₹{r['end_cap']:>10,.0f}  {r['max_dd']:>6.1f}%{marker}")

    print(f"{'═'*80}")

    # ── Rank by Net P&L ───────────────────────────────────────────────────────
    print(f"\n  RANKING BY NET P&L")
    print(f"  {'─'*60}")
    ranked = sorted(results.items(), key=lambda x: x[1]["net_pnl"], reverse=True)
    for rank, (label, r) in enumerate(ranked, 1):
        print(f"  #{rank:<3} {label:<30}  ₹{r['net_pnl']:>10,.0f}  WR {r['wr']:.1f}%")

    print(f"\n  ★ = key comparison points")
    print(f"  'BN gap at open' is the closest freely-available proxy to GIFT Nifty.")
    print(f"  'All 10 combined' is the current live strategy baseline.")
    print(f"{'═'*80}\n")

    # ── Key insights ──────────────────────────────────────────────────────────
    if "All 10 combined ★" in results and "BN gap at open ★" in results:
        combined_pnl = results["All 10 combined ★"]["net_pnl"]
        gift_pnl     = results["BN gap at open ★"]["net_pnl"]
        us_close_lbl = "US close (S&P500)"
        us_pnl       = results.get(us_close_lbl, {}).get("net_pnl", 0)

        print(f"  KEY FINDINGS")
        print(f"  {'─'*60}")
        print(f"  All 10 combined     : ₹{combined_pnl:>10,.0f}  (baseline)")
        print(f"  GIFT Nifty proxy    : ₹{gift_pnl:>10,.0f}  "
              f"({'better' if gift_pnl > combined_pnl else 'worse'} than combined)")
        print(f"  US close alone      : ₹{us_pnl:>10,.0f}  "
              f"({'better' if us_pnl > combined_pnl else 'worse'} than combined)")

        best_label, best_r = ranked[0]
        print(f"  Best single source  : {best_label}  →  ₹{best_r['net_pnl']:,.0f}")
        print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
