#!/usr/bin/env python3
"""
scan_ic_rr.py — Scan sl_frac × tp_frac combinations for NF Iron Condor.

Finds optimal SL/TP (reward:risk) ratio by running full backtest for each combo.
Current default: sl=0.50, tp=0.65 → RR=1.3x.

Usage:
    python3 scan_ic_rr.py
    python3 scan_ic_rr.py --ml      # use ML signals (default)
    python3 scan_ic_rr.py --rule    # use rule-based signals
"""
import os, sys, argparse
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from backtest_spreads import run_spread_backtest, _DATA_CACHE, NIFTY_STRATEGIES

SL_VALS = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
TP_VALS = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 0.95, 1.00]
YEARS   = 4.75   # Aug 2021 – Apr 2026

ap = argparse.ArgumentParser()
ap.add_argument("--rule", action="store_true", help="Use rule signals instead of ML")
args = ap.parse_args()
ml = not args.rule

# Pre-load data once — avoids reloading signals/OHLCV each iteration
print("Loading data (one-time)...")
_DATA_CACHE.clear()
# Prime the cache with one run
_ref = run_spread_backtest("nf_iron_condor", ml=ml, instrument="NF")
print(f"Data loaded. Reference: {len(_ref[_ref.result.isin(['SL','TP','EOD'])])} trades, "
      f"{len(_ref)} rows total\n")

print(f"Scanning {len(SL_VALS)} × {len(TP_VALS)} = {len(SL_VALS)*len(TP_VALS)} combos...")
print(f"{'sl':>5} {'tp':>5} {'rr':>5} {'trades':>7} {'wr':>7} {'pnl_L':>8} {'yr_L':>8} {'dd_L':>8} {'tp_hits':>8} {'eod_wins':>9}")
print("-" * 80)

rows = []
orig_sl = NIFTY_STRATEGIES["nf_iron_condor"]["sl_frac"]
orig_tp = NIFTY_STRATEGIES["nf_iron_condor"]["tp_frac"]

for sl in SL_VALS:
    for tp in TP_VALS:
        NIFTY_STRATEGIES["nf_iron_condor"]["sl_frac"] = sl
        NIFTY_STRATEGIES["nf_iron_condor"]["tp_frac"] = tp
        # Don't clear data cache — only strategy params changed
        df = run_spread_backtest("nf_iron_condor", ml=ml, instrument="NF")
        active = df[df["result"].isin(["SL", "TP", "EOD"])]
        if len(active) < 50:
            continue
        wins = (active["pnl"] > 0)
        cum  = active["pnl"].cumsum()
        dd   = float((cum.cummax() - cum).max())
        wr   = wins.mean()
        pnl  = active["pnl"].sum()
        rr   = round(tp / sl, 2)
        n_tp   = int((active["result"] == "TP").sum())
        n_eod_win = int(((active["result"] == "EOD") & (active["pnl"] > 0)).sum())

        rows.append({
            "sl_frac":    sl,
            "tp_frac":    tp,
            "rr":         rr,
            "trades":     len(active),
            "wr":         round(wr, 4),
            "pnl_L":      round(pnl / 1e5, 2),
            "pnl_yr_L":   round(pnl / YEARS / 1e5, 2),
            "max_dd_L":   round(dd / 1e5, 2),
            "tp_hits":    n_tp,
            "eod_wins":   n_eod_win,
        })
        print(f"{sl:>5.2f} {tp:>5.2f} {rr:>5.2f} {len(active):>7} {wr:>7.1%} "
              f"{pnl/1e5:>8.1f} {pnl/YEARS/1e5:>8.1f} {dd/1e5:>8.2f} "
              f"{n_tp:>8} {n_eod_win:>9}")

# Restore defaults
NIFTY_STRATEGIES["nf_iron_condor"]["sl_frac"] = orig_sl
NIFTY_STRATEGIES["nf_iron_condor"]["tp_frac"] = orig_tp

out = pd.DataFrame(rows)
print(f"\n{'='*70}")
print("TOP 15 — by Annual P&L")
print(f"{'='*70}")
top_pnl = out.sort_values("pnl_yr_L", ascending=False).head(15).copy()
top_pnl["wr"] = top_pnl["wr"].map(lambda x: f"{x:.1%}")
print(top_pnl.to_string(index=False))

print(f"\n{'='*70}")
print("TOP 10 — by WIN RATE (≥200 trades)")
print(f"{'='*70}")
wr_top = out[out["trades"] >= 200].sort_values("wr", ascending=False).head(10).copy()
wr_top["wr"] = wr_top["wr"].map(lambda x: f"{x:.1%}")
print(wr_top.to_string(index=False))

print(f"\n{'='*70}")
print("CURRENT defaults: sl=0.50  tp=0.65  rr=1.30")
cur = out[(out["sl_frac"] == 0.50) & (out["tp_frac"] == 0.65)]
if not cur.empty:
    r = cur.iloc[0]
    print(f"  → trades={r['trades']}  wr={r['wr']:.1%}  "
          f"pnl=₹{r['pnl_L']:.1f}L  yr=₹{r['pnl_yr_L']:.1f}L  dd=-₹{r['max_dd_L']:.2f}L")
best = out.sort_values("pnl_yr_L", ascending=False).iloc[0]
print(f"\nBEST: sl={best['sl_frac']:.2f}  tp={best['tp_frac']:.2f}  rr={best['rr']:.2f}"
      f"  → trades={best['trades']}  wr={best['wr']:.1%}"
      f"  ₹{best['pnl_yr_L']:.1f}L/yr  DD=-₹{best['max_dd_L']:.2f}L")
