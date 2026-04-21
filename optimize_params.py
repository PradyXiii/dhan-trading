#!/usr/bin/env python3
"""
optimize_params.py — Systematic parameter optimizer for BankNifty spread strategies.

Finds optimal combination of:
  • VIX band (trade only when VIX in [vix_min, vix_max])
  • ML confidence threshold (skip low-confidence signal days)
  • Entry time (when to enter the spread each morning)

Usage:
    python3 optimize_params.py                    # VIX + confidence grid (~10s)
    python3 optimize_params.py --entry-scan       # also scan 6 entry times (~3 min)
    python3 optimize_params.py --save-trades      # save enriched CSVs to /tmp/
    python3 optimize_params.py --year 2024        # grid on single year only
"""
import os, sys, itertools, argparse
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from backtest_spreads import run_spread_backtest, _DATA_CACHE

BNF_STRATEGIES = ["bear_call_credit", "bull_put_credit"]
NF_STRATEGIES  = ["nf_bear_call_credit", "nf_bull_put_credit"]
BASE_ENTRY  = "09:30"
BASE_EXIT   = "15:15"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(strat, entry=BASE_ENTRY, exit_=BASE_EXIT, year=None, max_dte=None,
         instrument="BNF"):
    """Run backtest; return only active (SL/TP/EOD) trades."""
    _DATA_CACHE.clear()
    df = run_spread_backtest(strat, ml=True, adaptive=False,
                             entry_time=entry, exit_time=exit_,
                             max_dte=max_dte, instrument=instrument)
    df = df[df["result"].isin(["SL", "TP", "EOD"])].copy()
    if year is not None:
        df = df[pd.to_datetime(df["date"]).dt.year == year]
    return df


def _filter(df, vmin, vmax, conf):
    f = df.copy()
    if vmin is not None and "vix_at_entry" in f.columns:
        f = f[f["vix_at_entry"] >= vmin]
    if vmax is not None and "vix_at_entry" in f.columns:
        f = f[f["vix_at_entry"] <= vmax]
    if conf is not None and "ml_conf" in f.columns:
        f = f[f["ml_conf"] >= conf]
    return f


def _stats(df):
    if len(df) < 20:
        return None
    win = df["pnl"] > 0
    cum = df["pnl"].cumsum()
    dd  = float((cum.cummax() - cum).max())
    # Annualise: assume ~250 trading days/year
    years = max((pd.to_datetime(df["date"]).max()
                 - pd.to_datetime(df["date"]).min()).days / 365.25, 0.1)
    return {
        "n":       len(df),
        "wr":      float(win.mean()),
        "pnl":     float(df["pnl"].sum()),
        "pnl_yr":  float(df["pnl"].sum() / years),
        "dpt":     float(df["pnl"].mean()),
        "dd":      dd,
    }


# ── Grid search ───────────────────────────────────────────────────────────────

def grid_search(trade_dfs):
    """
    Exhaustive grid: VIX band × ML confidence threshold.
    Returns DataFrame sorted by combined P&L.
    """
    vix_mins  = [None, 11, 12, 13, 14, 15]
    vix_maxs  = [None, 18, 19, 20, 21, 22, 25]
    min_confs = [None, 0.55, 0.57, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]

    rows = []
    total = len(vix_mins) * len(vix_maxs) * len(min_confs)
    done  = 0

    for vmin, vmax, conf in itertools.product(vix_mins, vix_maxs, min_confs):
        done += 1
        if vmin is not None and vmax is not None and vmin >= vmax:
            continue

        combined = {"n": 0, "wins": 0, "pnl": 0.0, "pnl_yr": 0.0, "dd": 0.0}
        valid = True

        for strat, df in trade_dfs.items():
            sub = _filter(df, vmin, vmax, conf)
            st  = _stats(sub)
            if st is None:
                valid = False
                break
            combined["n"]     += st["n"]
            combined["wins"]  += int(st["wr"] * st["n"])
            combined["pnl"]   += st["pnl"]
            combined["pnl_yr"] += st["pnl_yr"]
            combined["dd"]    = max(combined["dd"], st["dd"])

        if not valid or combined["n"] < 50:
            continue

        rows.append({
            "vix_min":  vmin if vmin  is not None else "-",
            "vix_max":  vmax if vmax  is not None else "-",
            "min_conf": f"{conf:.2f}" if conf is not None else "-",
            "trades":   combined["n"],
            "wr":       combined["wins"] / combined["n"],
            "pnl_L":    round(combined["pnl"]    / 1e5, 2),
            "pnl_yr_L": round(combined["pnl_yr"] / 1e5, 2),
            "max_dd_L": round(combined["dd"]     / 1e5, 2),
        })

    df_out = pd.DataFrame(rows)
    return df_out


# ── Entry time scan ───────────────────────────────────────────────────────────

def entry_time_scan(entry_times, year=None, instrument="BNF", strategies=None):
    """Re-run full backtests for each entry time."""
    if strategies is None:
        strategies = NF_STRATEGIES if instrument == "NF" else BNF_STRATEGIES
    rows = []
    for et in entry_times:
        print(f"  Entry {et} ...", end=" ", flush=True)
        combined = {"n": 0, "wins": 0, "pnl": 0.0, "pnl_yr": 0.0}
        for strat in strategies:
            df = _run(strat, entry=et, year=year, instrument=instrument)
            st = _stats(df)
            if st:
                combined["n"]     += st["n"]
                combined["wins"]  += int(st["wr"] * st["n"])
                combined["pnl"]   += st["pnl"]
                combined["pnl_yr"] += st["pnl_yr"]
        wr = combined["wins"] / combined["n"] if combined["n"] else 0
        print(f"WR={wr:.1%}  P&L=₹{combined['pnl']/1e5:.2f}L/total  "
              f"₹{combined['pnl_yr']/1e5:.2f}L/yr  ({combined['n']} trades)")
        rows.append({
            "entry":    et,
            "trades":   combined["n"],
            "wr":       wr,
            "pnl_L":    round(combined["pnl"]    / 1e5, 2),
            "pnl_yr_L": round(combined["pnl_yr"] / 1e5, 2),
        })
    return pd.DataFrame(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def _fmt(df, sort_col, ascending=False, head=25):
    d = df.sort_values(sort_col, ascending=ascending).head(head).copy()
    if "wr" in d.columns:
        d["wr"] = d["wr"].map(lambda x: f"{x:.1%}")
    return d.to_string(index=False)


def main():
    ap = argparse.ArgumentParser(description="Spread parameter optimizer")
    ap.add_argument("--instrument", choices=["BNF", "NF"], default="BNF",
                    help="BNF=BankNifty (default), NF=Nifty50 weekly")
    ap.add_argument("--entry-scan",  action="store_true",
                    help="Test 6 entry times 09:15–10:30 (requires ~3 min)")
    ap.add_argument("--save-trades", action="store_true",
                    help="Save enriched trade CSVs to /tmp/bc_opt.csv, /tmp/bp_opt.csv")
    ap.add_argument("--year", type=int, default=None,
                    help="Restrict analysis to a single year (e.g. --year 2024)")
    ap.add_argument("--max-dte", type=int, default=None,
                    help="Only trade days where DTE ≤ N (e.g. --max-dte 7 = "
                         "last week before expiry, mimics weekly conditions)")
    args = ap.parse_args()

    inst = args.instrument
    strategies = NF_STRATEGIES if inst == "NF" else BNF_STRATEGIES
    inst_label = "Nifty50" if inst == "NF" else "BankNifty"

    yr_note  = f" (year={args.year})"     if args.year    else ""
    dte_note = f" (DTE≤{args.max_dte})"   if args.max_dte else ""
    print(f"\n{'='*70}")
    print(f"{inst_label} Spread Parameter Optimizer{yr_note}{dte_note}")
    print(f"{'='*70}")

    # ── Load base backtests ────────────────────────────────────────────────────
    print("\nRunning base backtests (no extra filters)...")
    trade_dfs = {}
    for strat in strategies:
        df = _run(strat, year=args.year, max_dte=args.max_dte, instrument=inst)
        st = _stats(df)
        if st:
            label = "Bear Call" if "bear" in strat else "Bull Put"
            print(f"  {label}: {st['n']} trades  WR={st['wr']:.1%}  "
                  f"P&L=₹{st['pnl']/1e5:.2f}L  (₹{st['pnl_yr']/1e5:.2f}L/yr)")
        trade_dfs[strat] = df
        if args.save_trades:
            key  = "bc" if "bear" in strat else "bp"
            path = f"/tmp/{inst.lower()}_{key}_opt.csv"
            df.to_csv(path, index=False)
            print(f"    → saved {len(df)} rows to {path}")

    # ── Grid search ───────────────────────────────────────────────────────────
    print("\nGrid search: VIX band × ML confidence threshold...")
    results = grid_search(trade_dfs)

    if results.empty:
        print("  No valid combinations found — check data files.")
        return

    print(f"\n{'='*80}")
    print(f"TOP 25 — sorted by ANNUAL P&L (combined Bear Call + Bull Put) [{inst}]")
    print(f"{'='*80}")
    print(_fmt(results, "pnl_yr_L"))

    print(f"\n{'='*80}")
    print("TOP 20 — sorted by WIN RATE (≥80 trades minimum)")
    print(f"{'='*80}")
    wr_top = results[results["trades"] >= 80].sort_values(
        ["wr", "pnl_yr_L"], ascending=False).head(20).copy()
    if "wr" in wr_top.columns:
        wr_top["wr"] = wr_top["wr"].map(lambda x: f"{x:.1%}")
    print(wr_top.to_string(index=False))

    # Best combo summary
    best = results.sort_values("pnl_yr_L", ascending=False).iloc[0]
    print(f"\n{'='*80}")
    print("BEST COMBO — by P&L:")
    print(f"  VIX [{best['vix_min']} – {best['vix_max']}]  conf ≥ {best['min_conf']}"
          f"  →  WR={best['wr']:.1%}  {best['trades']} trades  "
          f"₹{best['pnl_yr_L']:.2f}L/yr  DD=-₹{best['max_dd_L']:.2f}L")
    wr_candidates = results[results["trades"] >= 80]
    print("BEST COMBO — by WR (≥80 trades):")
    if wr_candidates.empty:
        print("  (fewer than 80 trades in all combos — try wider filter or longer date range)")
    else:
        best_wr = wr_candidates.sort_values("wr", ascending=False).iloc[0]
        print(f"  VIX [{best_wr['vix_min']} – {best_wr['vix_max']}]  conf ≥ {best_wr['min_conf']}"
              f"  →  WR={best_wr['wr']:.1%}  {best_wr['trades']} trades  "
              f"₹{best_wr['pnl_yr_L']:.2f}L/yr  DD=-₹{best_wr['max_dd_L']:.2f}L")

    # ── Entry time scan ────────────────────────────────────────────────────────
    if args.entry_scan:
        print(f"\n{'='*80}")
        print(f"ENTRY TIME SCAN (full backtest re-run per time) [{inst}]")
        print(f"{'='*80}")
        et_df = entry_time_scan(
            ["09:15", "09:30", "09:45", "10:00", "10:15", "10:30"],
            year=args.year, instrument=inst,
        )
        print()
        print(et_df.to_string(index=False))
        best_et = et_df.sort_values("pnl_yr_L", ascending=False).iloc[0]
        print(f"\n  Best entry time: {best_et['entry']}  "
              f"WR={best_et['wr']:.1%}  ₹{best_et['pnl_yr_L']:.2f}L/yr")


if __name__ == "__main__":
    main()
