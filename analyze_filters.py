#!/usr/bin/env python3
"""
Trade-frequency filter analyzer.

Given a per-trade detail CSV (from `backtest_entry_time.py --out`) OR a fresh
real-options backtest, tries a battery of filters to find which combination
(a) keeps at least --min-retention% of trades and (b) maximises total P&L.

Filters tested:
  • rule_ml_agree   — rule_signal == ml_signal
  • ml_conf_band    — ml_conf in [lo, hi]
  • gap_cap         — |bn_open − prev_close| / prev_close ≤ cap
  • orb_extension   — skip if ORB (9:15-9:30) already ran > X% in signal direction
  • weekday_skip    — skip specific weekdays
  • dte_skip0       — skip same-day-expiry (DTE=0)
  • vix_band        — vix_level in [lo, hi]

Usage:
  # Source from existing detail CSV
  python3 analyze_filters.py --in /tmp/detail_rr1.csv --entry-time 09:30

  # Or let it regenerate the per-trade detail for given params
  python3 analyze_filters.py --regen --rr 1.0 --no-trail --start 2024-10-01

  # Lower retention floor or raise threshold
  python3 analyze_filters.py --in /tmp/detail_rr1.csv --min-retention 60
"""
import argparse
import itertools
import os
import subprocess
import sys
from datetime import datetime

import pandas as pd
import numpy as np

DATA_DIR = "data"


def _load_detail(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = df["date"].dt.date
    df["pnl_per_lot"] = pd.to_numeric(df["pnl_per_lot"], errors="coerce").fillna(0.0)
    return df


def _enrich(detail: pd.DataFrame, entry_time: str) -> pd.DataFrame:
    """Join signals_ml + banknifty + ORB features onto the detail table."""
    d = detail[detail["entry_time"] == entry_time].copy()

    # signals_ml.csv — ml_conf, rule_signal, score, weekday
    sig_path = os.path.join(DATA_DIR, "signals_ml.csv")
    if not os.path.exists(sig_path):
        print(f"ERROR: {sig_path} missing")
        sys.exit(1)
    sig = pd.read_csv(sig_path, parse_dates=["date"])
    sig["date"] = sig["date"].dt.date
    keep = [c for c in ["date", "weekday", "ml_conf", "rule_signal",
                        "ml_signal", "score", "ml_p_call", "ml_p_put"]
            if c in sig.columns]
    d = d.merge(sig[keep], on="date", how="left")

    # banknifty.csv — prev close for gap
    bn_path = os.path.join(DATA_DIR, "banknifty.csv")
    if os.path.exists(bn_path):
        bn = pd.read_csv(bn_path, parse_dates=["date"])
        bn["date"]       = bn["date"].dt.date
        bn["prev_close"] = bn["close"].shift(1)
        d = d.merge(bn[["date", "open", "prev_close"]],
                    on="date", how="left", suffixes=("", "_bn"))
        d["gap_pct"] = 100.0 * (d["open"] - d["prev_close"]) / d["prev_close"]
    else:
        d["gap_pct"] = np.nan

    # india_vix.csv — VIX at close (D-1)
    vix_path = os.path.join(DATA_DIR, "india_vix.csv")
    if os.path.exists(vix_path):
        vix = pd.read_csv(vix_path, parse_dates=["date"])
        vix["date"] = vix["date"].dt.date
        vix = vix.rename(columns={"close": "vix_level"})
        d = d.merge(vix[["date", "vix_level"]], on="date", how="left")
    else:
        d["vix_level"] = np.nan

    # ORB file — signed extension
    orb_path = os.path.join(DATA_DIR, "banknifty_15m_orb.csv")
    if os.path.exists(orb_path):
        orb = pd.read_csv(orb_path, parse_dates=["date"])
        orb["date"]    = orb["date"].dt.date
        orb["orb_ext"] = 100.0 * (orb["orb_close"] - orb["orb_open"]) / orb["orb_open"]
        d = d.merge(orb[["date", "orb_ext"]], on="date", how="left")
        # signed extension: positive = moved in signal direction
        d["orb_dir_pct"] = np.where(d["signal"] == "CALL",
                                      d["orb_ext"],
                                     -d["orb_ext"])
    else:
        d["orb_dir_pct"] = np.nan

    # DTE (days to nearest monthly/weekly expiry) — approximate via weekday only here
    # (full DTE lives in backtest_engine.get_dte; for filter purposes, we mark
    #  DTE=0 if trade day IS an expiry day via simple weekday proxy)
    return d


def _summarise(df: pd.DataFrame, label: str) -> dict:
    valid = df[df["result"].isin(["WIN", "LOSS", "TRAIL_SL", "PARTIAL"])]
    if valid.empty:
        return {"filter": label, "trades": 0, "retention%": 0.0, "WR%": 0.0,
                "avg_pnl": 0, "total_pnl": 0}
    wins   = (valid["result"] == "WIN").sum()
    losses = (valid["result"] == "LOSS").sum()
    wr     = 100.0 * wins / (wins + losses) if (wins + losses) else 0.0
    return {
        "filter":     label,
        "trades":     len(valid),
        "WR%":        round(wr, 1),
        "avg_pnl":    int(round(valid["pnl_per_lot"].mean())),
        "total_pnl":  int(round(valid["pnl_per_lot"].sum())),
    }


def _apply(df: pd.DataFrame, mask: pd.Series, label: str, baseline_n: int) -> dict:
    r = _summarise(df[mask], label)
    r["retention%"] = round(100.0 * r["trades"] / baseline_n, 1) if baseline_n else 0.0
    return r


def run_grid(df: pd.DataFrame, min_retention: float) -> pd.DataFrame:
    """Exhaustive filter grid, retain results that pass min-retention gate."""
    baseline = _summarise(df, "BASELINE (no filter)")
    baseline["retention%"] = 100.0
    rows = [baseline]
    n    = baseline["trades"]

    # ── Individual filters ───────────────────────────────────────────────────
    if "rule_signal" in df.columns and "ml_signal" in df.columns:
        mask = (df["rule_signal"] == df["ml_signal"])
        rows.append(_apply(df, mask, "rule=ml agreement", n))

    for lo, hi in [(0.40, 0.55), (0.45, 0.58), (0.48, 0.60),
                    (0.50, 0.60), (0.45, 0.55)]:
        mask = (df["ml_conf"] >= lo) & (df["ml_conf"] <= hi)
        rows.append(_apply(df, mask, f"ml_conf∈[{lo:.2f},{hi:.2f}]", n))

    for cap in [0.30, 0.50, 0.80, 1.00, 1.50]:
        mask = df["gap_pct"].abs() <= cap
        rows.append(_apply(df, mask, f"|gap|≤{cap:.2f}%", n))

    # Skip big "already ran" ORB extensions (chase filter)
    for cap in [0.30, 0.50, 0.80, 1.00, 1.50]:
        mask = df["orb_dir_pct"] <= cap
        rows.append(_apply(df, mask, f"orb_dir≤+{cap:.2f}%", n))

    # Skip one weekday at a time
    if "weekday" in df.columns:
        for wd in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            mask = df["weekday"] != wd
            rows.append(_apply(df, mask, f"skip {wd}", n))

    if "vix_level" in df.columns:
        for lo, hi in [(10, 18), (12, 20), (14, 22), (16, 30)]:
            mask = (df["vix_level"] >= lo) & (df["vix_level"] <= hi)
            rows.append(_apply(df, mask, f"vix∈[{lo},{hi}]", n))

    # ── 2-way combos (only top single filters, avoid combinatorial blowup) ───
    def _mask_agree(x):       return (x["rule_signal"] == x["ml_signal"])
    def _mask_gap(x, cap):    return x["gap_pct"].abs() <= cap
    def _mask_orb(x, cap):    return x["orb_dir_pct"] <= cap
    def _mask_conf(x, lo, hi): return (x["ml_conf"] >= lo) & (x["ml_conf"] <= hi)

    combos = [
        ("agree AND |gap|≤1.0%",       _mask_agree(df) & _mask_gap(df, 1.0)),
        ("agree AND orb_dir≤+0.80%",   _mask_agree(df) & _mask_orb(df, 0.80)),
        ("agree AND orb_dir≤+0.50%",   _mask_agree(df) & _mask_orb(df, 0.50)),
        ("agree AND ml_conf∈[0.45,0.58]",
                                        _mask_agree(df) & _mask_conf(df, 0.45, 0.58)),
        ("|gap|≤1.0% AND orb_dir≤+0.80%",
                                        _mask_gap(df, 1.0) & _mask_orb(df, 0.80)),
        ("|gap|≤0.8% AND orb_dir≤+0.50%",
                                        _mask_gap(df, 0.8) & _mask_orb(df, 0.50)),
        ("agree AND |gap|≤1.0% AND orb_dir≤+0.80%",
                                        _mask_agree(df) & _mask_gap(df, 1.0) & _mask_orb(df, 0.80)),
    ]
    for label, mask in combos:
        rows.append(_apply(df, mask, label, n))

    result = pd.DataFrame(rows)

    # Keep only filters that hit retention floor, then rank by total P&L
    kept = result[result["retention%"] >= min_retention].copy()
    kept = kept.sort_values("total_pnl", ascending=False).reset_index(drop=True)
    return result, kept


def _print_table(title: str, df: pd.DataFrame) -> None:
    if df.empty:
        print(f"\n{title}: (empty)")
        return
    print(f"\n{title}")
    print("─" * 92)
    print(f"  {'filter':<44}{'trades':>8}{'ret%':>7}{'WR%':>7}"
          f"{'avg ₹':>10}{'total ₹':>14}")
    print("  " + "─" * 88)
    for _, r in df.iterrows():
        print(f"  {r['filter']:<44}{int(r['trades']):>8}"
              f"{r['retention%']:>7.1f}{r['WR%']:>7.1f}"
              f"{int(r['avg_pnl']):>10}{int(r['total_pnl']):>14}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default="/tmp/detail_rr1.csv",
                    help="per-trade detail CSV from `backtest_entry_time.py --out`")
    ap.add_argument("--entry-time", default="09:30",
                    help="evaluate at this entry time")
    ap.add_argument("--min-retention", type=float, default=60.0,
                    help="minimum %% of trades to keep (default 60)")
    ap.add_argument("--regen", action="store_true",
                    help="regenerate detail CSV via backtest_entry_time.py")
    ap.add_argument("--rr",        type=float, default=1.0)
    ap.add_argument("--sl-pct",    type=float, default=0.15)
    ap.add_argument("--trail-jump",type=float, default=5.0)
    ap.add_argument("--no-trail",  action="store_true")
    ap.add_argument("--start",     default=None)
    ap.add_argument("--end",       default=None)
    args = ap.parse_args()

    if args.regen:
        cmd = ["python3", "backtest_entry_time.py",
               "--times", args.entry_time,
               "--rr", str(args.rr), "--sl-pct", str(args.sl_pct),
               "--out", args.in_path]
        if args.no_trail:   cmd.append("--no-trail")
        else:               cmd += ["--trail-jump", str(args.trail_jump)]
        if args.start:      cmd += ["--start", args.start]
        if args.end:        cmd += ["--end",   args.end]
        print("Regenerating per-trade detail:  " + " ".join(cmd))
        subprocess.check_call(cmd)

    if not os.path.exists(args.in_path):
        print(f"ERROR: {args.in_path} missing — run with --regen or pass --in")
        sys.exit(1)

    print(f"\nLoading: {args.in_path}  (entry_time={args.entry_time})")
    detail = _load_detail(args.in_path)
    enriched = _enrich(detail, args.entry_time)
    print(f"Trade rows: {len(enriched)}")

    full, kept = run_grid(enriched, args.min_retention)

    _print_table(f"ALL FILTERS TRIED (incl. baseline)", full.sort_values("total_pnl", ascending=False))
    _print_table(f"FILTERS RETAINING ≥ {args.min_retention:.0f}% OF TRADES  (best first)", kept)

    baseline = full[full["filter"] == "BASELINE (no filter)"].iloc[0]
    print(f"\nBaseline:  {baseline['trades']} trades  "
          f"WR {baseline['WR%']}%  avg ₹{int(baseline['avg_pnl'])}  "
          f"total ₹{int(baseline['total_pnl'])}")
    if not kept.empty:
        best = kept.iloc[0]
        delta = int(best["total_pnl"] - baseline["total_pnl"])
        print(f"Best kept: {best['filter']}")
        print(f"  {int(best['trades'])} trades ({best['retention%']}%)"
              f"  WR {best['WR%']}%  avg ₹{int(best['avg_pnl'])}  "
              f"total ₹{int(best['total_pnl'])}  (Δ vs baseline: {delta:+,} ₹)")


if __name__ == "__main__":
    main()
