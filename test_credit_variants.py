#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
Credit-spread variant sweep.
Tests 6 independent tweaks vs the +₹36.2L baseline (sl=0.5, tp=0.65).

Variants:
  1. VIX filter         — skip VIX <13 or >22 (avoid tiny credit + breach days)
  2. DTE filter         — only DTE 7-14 (sweet spot theta zone)
  3. Delayed entry      — 10:30 AM (skip opening whipsaw)
  4. Early exit         — 1:30 PM  (capture theta, dodge EOD gamma)
  5. Lot cap 10         — half MAX_LOTS (reduce worst-day bleed)
  6. Combined best      — stack all wins

ATM+2 strike width requires new OTM fetch — not tested here.
"""
import copy
import pandas as pd

import backtest_spreads as bs


def run(label, overrides_bc, overrides_bp,
        entry_time="09:30", exit_time="15:15"):
    """Run both credit strategies with temp overrides; return (bc_df, bp_df)."""
    saved_bc = copy.deepcopy(bs.STRATEGIES["bear_call_credit"])
    saved_bp = copy.deepcopy(bs.STRATEGIES["bull_put_credit"])
    bs.STRATEGIES["bear_call_credit"].update(overrides_bc)
    bs.STRATEGIES["bull_put_credit"].update(overrides_bp)
    try:
        bc = bs.run_spread_backtest("bear_call_credit", ml=True,
                                    entry_time=entry_time, exit_time=exit_time)
        bp = bs.run_spread_backtest("bull_put_credit",  ml=True,
                                    entry_time=entry_time, exit_time=exit_time)
    finally:
        bs.STRATEGIES["bear_call_credit"] = saved_bc
        bs.STRATEGIES["bull_put_credit"]  = saved_bp
    return bc, bp


def summary(label, bc_df, bp_df):
    def _stats(df):
        active = df[df["result"].isin(["SL", "TP", "EOD"])]
        n      = len(active)
        wins   = active[active["pnl"] > 0]
        wr     = (len(wins) / n * 100) if n else 0
        pnl    = active["pnl"].sum()
        return n, wr, pnl

    n_bc, wr_bc, pnl_bc = _stats(bc_df)
    n_bp, wr_bp, pnl_bp = _stats(bp_df)
    combined = pnl_bc + pnl_bp
    print(f"  {label:<32s}  "
          f"BC: {n_bc:>3}t/{wr_bc:4.1f}%/₹{pnl_bc/1e5:+5.1f}L  "
          f"BP: {n_bp:>3}t/{wr_bp:4.1f}%/₹{pnl_bp/1e5:+5.1f}L  "
          f"TOTAL: ₹{combined/1e5:+6.2f}L")
    return combined


def main():
    print("\n" + "="*100)
    print("CREDIT SPREAD VARIANT SWEEP  (baseline = sl=0.5, tp=0.65, entry=9:30, exit=15:15)")
    print("="*100 + "\n")

    baseline = run("baseline", {}, {})
    base_total = summary("0. Baseline (locked config)", *baseline)
    print()

    # 1. VIX filter 13-22
    v1 = run("vix13-22",
            {"vix_min": 13.0, "vix_max": 22.0},
            {"vix_min": 13.0, "vix_max": 22.0})
    summary("1. VIX 13-22", *v1)

    # 2. DTE filter 7-14
    v2 = run("dte7-14",
            {"dte_min": 7, "dte_max": 14},
            {"dte_min": 7, "dte_max": 14})
    summary("2. DTE 7-14", *v2)

    # 3. Delayed entry 10:30
    v3 = run("entry10:30", {}, {}, entry_time="10:30")
    summary("3. Entry 10:30", *v3)

    # 4. Early exit 13:30
    v4 = run("exit13:30", {}, {}, exit_time="13:30")
    summary("4. Exit 13:30", *v4)

    # 5. Lot cap 10
    v5 = run("maxlots10",
            {"max_lots": 10},
            {"max_lots": 10})
    summary("5. Max lots 10", *v5)

    # 6. Combined best — stack every positive variant (we'll know which from above)
    print()
    print("-"*100)
    print("Individual tweaks above. Combined-best run after inspecting results.")
    print("-"*100)


if __name__ == "__main__":
    main()
