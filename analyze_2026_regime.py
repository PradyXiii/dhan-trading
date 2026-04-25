#!/usr/bin/env python3
"""
analyze_2026_regime.py — diagnostic for 2026 IC WR drop

The NF IC backtest shows 2026 (Jan-Apr) WR dropped to 66.7% vs 5yr avg 84.7%.
This script slices the trade log to identify what changed.

Usage:
  python3 analyze_2026_regime.py [--csv data/trade_log_ml_realopt.csv]

Reads trade_log_ml_realopt.csv (written by backtest_engine.py --real-options --ml)
OR data/equity_curve_ml_realopt.csv. Falls back to live_ic_trades.csv for live data.
"""

import os
import sys
import pandas as pd
import numpy as np
from pathlib import Path

DATA = Path(__file__).parent / "data"


def _load_ic_log():
    candidates = [
        DATA / "ic_backtest_trades.csv",
        DATA / "trade_log_ml_realopt.csv",
        DATA / "live_ic_trades.csv",
    ]
    for p in candidates:
        if p.exists():
            try:
                df = pd.read_csv(p)
                date_col = next((c for c in ["date", "Date", "trade_date"] if c in df.columns), None)
                if date_col:
                    df["date"] = pd.to_datetime(df[date_col])
                    return df, p
            except Exception:
                continue
    return None, None


def _summarise(df, label):
    if df.empty:
        print(f"  {label}: no rows")
        return
    pnl_col = next((c for c in ["net_pnl", "pnl", "profit"] if c in df.columns), None)
    if not pnl_col:
        print(f"  {label}: no pnl column found")
        return
    n   = len(df)
    wr  = (df[pnl_col] > 0).mean() * 100
    avg = df[pnl_col].mean()
    tot = df[pnl_col].sum()
    avg_w = df.loc[df[pnl_col] > 0, pnl_col].mean()
    avg_l = df.loc[df[pnl_col] <= 0, pnl_col].mean()
    print(f"  {label:<20} N={n:<5} WR={wr:5.1f}%  avg=₹{avg:>9,.0f}  total=₹{tot:>12,.0f}  avgW=₹{avg_w:>8,.0f}  avgL=₹{avg_l:>8,.0f}")


def _vix_stats(df, label):
    if "vix" not in df.columns and "vix_open" not in df.columns:
        return
    vix_col = "vix_open" if "vix_open" in df.columns else "vix"
    print(f"  {label} VIX: mean={df[vix_col].mean():.2f}  std={df[vix_col].std():.2f}  "
          f"min={df[vix_col].min():.2f}  max={df[vix_col].max():.2f}")


def main():
    df, src = _load_ic_log()
    if df is None:
        print("No IC trade log found in data/. Run `backtest_spreads.py` first.")
        sys.exit(1)
    print(f"Source: {src}")
    print(f"Trades: {len(df)}  date range: {df['date'].min().date()} → {df['date'].max().date()}")
    print()

    df["year"] = df["date"].dt.year
    df["dow"]  = df["date"].dt.day_name()

    print("=" * 80)
    print("PER-YEAR SUMMARY")
    print("=" * 80)
    for yr in sorted(df["year"].unique()):
        _summarise(df[df["year"] == yr], f"{yr}")
    print()

    print("=" * 80)
    print("2026 vs 2024-2025 (last full bull regime)")
    print("=" * 80)
    base = df[df["year"].isin([2024, 2025])]
    new  = df[df["year"] == 2026]
    _summarise(base, "2024-2025 baseline")
    _summarise(new,  "2026 (Jan-Apr)")

    if "vix" in df.columns or "vix_open" in df.columns:
        _vix_stats(base, "2024-2025")
        _vix_stats(new,  "2026     ")

    print()
    print("=" * 80)
    print("DOW BREAKDOWN — 2026 vs baseline")
    print("=" * 80)
    print(f"  {'DOW':<10} {'2024-25 WR':<12} {'2026 WR':<12} {'Δ pp':<8}")
    for dow in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
        wr_b = (base[base["dow"] == dow]["net_pnl"] > 0).mean() * 100 if "net_pnl" in base.columns else np.nan
        wr_n = (new[new["dow"] == dow]["net_pnl"] > 0).mean()  * 100 if "net_pnl" in new.columns else np.nan
        if pd.isna(wr_b) or pd.isna(wr_n):
            continue
        print(f"  {dow:<10} {wr_b:5.1f}%       {wr_n:5.1f}%       {wr_n-wr_b:+5.1f}")

    print()
    print("=" * 80)
    print("LARGEST 5 LOSSES IN 2026")
    print("=" * 80)
    if "net_pnl" in new.columns and not new.empty:
        worst = new.nsmallest(5, "net_pnl")[["date", "dow", "net_pnl"] + [c for c in ["vix_open", "nf_open", "signal"] if c in new.columns]]
        print(worst.to_string(index=False))

    print()
    print("Hypothesis to investigate:")
    print("  1. VIX spike events: did 2026 have more sudden vol jumps?")
    print("  2. Friday concentration: are losses concentrated on a single DOW?")
    print("  3. Outlier days: 1-2 events dragging WR down vs systemic shift?")
    print("  4. Fed/RBI event days: are losses clustered around macro events?")


if __name__ == "__main__":
    main()
