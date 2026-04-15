#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
backtest_live_context.py — Live market context backtest
========================================================
Tests whether adding signals available at 9:15 AM (before trade entry)
improves directional accuracy vs the current daily-close ML model.

Live context signals (proxied from daily OHLCV — no lookahead):
  bn_gap_pct    BN open vs yesterday's close  (already in features)
  spf_gap_pct   ES futures overnight gap       (already in features)
  sp500_chg     S&P 500 previous day return    (already in features)
  vix_open_chg  VIX open vs yesterday's VIX close  ← NEW

Override logic tested:
  GAP_TRAP      BN gaps UP ≥ X% but ES/SP500 was DOWN ≥ Y% → flip CALL→PUT
  VIX_SURGE     VIX opens UP ≥ Z% while BN gaps up → flip CALL→PUT
  (symmetric rules for PUT direction)

Grid-searches over thresholds, finds the combination that maximises accuracy,
and shows per-rule breakdown + P&L impact.

Usage:
  python3 backtest_live_context.py
  python3 backtest_live_context.py --from 2023-01-01
"""

import os
import sys
import argparse
import numpy as np
import pandas as pd
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ml_engine import (
    load_all_data, compute_features, compute_labels,
    simulate_outcome, DATA_DIR,
)
from backtest_engine import PREMIUM_K, get_dte

SL_PCT   = 0.15
RR       = 2.5
TP_PCT   = SL_PCT * RR
LOT_SIZE = 30


# ─────────────────────────────────────────────────────────────────────────────
#  LOAD VIX OPEN (not in standard load_all_data — only close is loaded)
# ─────────────────────────────────────────────────────────────────────────────

def load_vix_open_chg():
    """
    Returns Series indexed by date: VIX open % change vs previous VIX close.
    This is what VIX looks like at 9:15 AM — VIX opens with the equity market.
    """
    path = f"{DATA_DIR}/india_vix.csv"
    if not os.path.exists(path):
        print("  ⚠  india_vix.csv not found — VIX_SURGE rule disabled")
        return pd.Series(dtype=float, name="vix_open_chg")
    df = pd.read_csv(path, parse_dates=["date"])
    if "open" not in df.columns:
        print("  ⚠  india_vix.csv has no 'open' column — VIX_SURGE rule disabled")
        return pd.Series(dtype=float, name="vix_open_chg")
    df = df.sort_values("date").reset_index(drop=True)
    df["vix_prev_close"] = df["close"].shift(1)
    df["vix_open_chg"]   = (df["open"] - df["vix_prev_close"]) / df["vix_prev_close"] * 100
    return df.set_index("date")["vix_open_chg"]


# ─────────────────────────────────────────────────────────────────────────────
#  OVERRIDE RULES
# ─────────────────────────────────────────────────────────────────────────────

def apply_overrides(signal, bn_gap, spf_gap, sp500_chg, vix_open_chg,
                    gap_thresh, es_down_thresh, vix_thresh):
    """
    Returns (final_signal, rule_name | None).

    GAP_TRAP  : BN gaps ≥ gap_thresh% UP, but ES overnight gap OR SP500 prev day
                is ≤ -es_down_thresh% → morning strength not globally supported → PUT
    VIX_SURGE : VIX opens ≥ vix_thresh% ABOVE prev close while BN gaps up
                → institutions selling into the gap → PUT

    Symmetric versions fire for PUT days where gap is down but global is up.
    """
    rules_fired = []

    # ── CALL overrides ────────────────────────────────────────────────────────
    if signal == "CALL":
        # GAP_TRAP: BN up but ES/SP500 falling
        if bn_gap >= gap_thresh and (spf_gap <= -es_down_thresh or sp500_chg <= -es_down_thresh):
            rules_fired.append("GAP_TRAP")
        # VIX_SURGE: VIX rising while BN gapping up
        if not np.isnan(vix_open_chg) and vix_open_chg >= vix_thresh and bn_gap >= 0.1:
            rules_fired.append("VIX_SURGE")

    # ── PUT overrides ─────────────────────────────────────────────────────────
    elif signal == "PUT":
        # GAP_TRAP_PUT: BN down but ES/SP500 recovering
        if bn_gap <= -gap_thresh and (spf_gap >= es_down_thresh or sp500_chg >= es_down_thresh):
            rules_fired.append("GAP_TRAP_PUT")
        # VIX_CRASH_PUT: VIX collapsing while BN gapping down (risk-on opening)
        if not np.isnan(vix_open_chg) and vix_open_chg <= -vix_thresh and bn_gap <= -0.1:
            rules_fired.append("VIX_CRASH_PUT")

    if rules_fired:
        flipped = "PUT" if signal == "CALL" else "CALL"
        return flipped, "+".join(rules_fired)
    return signal, None


# ─────────────────────────────────────────────────────────────────────────────
#  OUTCOME HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def outcome_wins(outcome):
    """WIN or PARTIAL (near-win EOD) count as wins for accuracy."""
    return outcome in ("WIN", "PARTIAL")


def pnl_for_outcome(outcome, premium, lots=1):
    if outcome == "WIN":
        return round((TP_PCT * premium) * LOT_SIZE * lots, 0)
    elif outcome == "LOSS":
        return round((-SL_PCT * premium) * LOT_SIZE * lots, 0)
    else:  # PARTIAL — assume small gain/loss, use 0 as neutral
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2022-01-01",
                        help="Start date for backtest (default: 2022-01-01)")
    parser.add_argument("--signal-source", choices=["ml", "rule"], default="ml",
                        help="Use ML signals or rule-based signals (default: ml)")
    args = parser.parse_args()

    print(f"\n{'═'*65}")
    print(f"  LIVE CONTEXT BACKTEST")
    print(f"  From: {args.from_date}  |  Signal: {args.signal_source.upper()}")
    print(f"{'═'*65}")

    # ── 1. Load features + ground-truth labels ────────────────────────────────
    print("\n  Loading features...")
    raw  = load_all_data()
    feat = compute_features(raw)
    feat = feat[feat["date"] >= pd.Timestamp(args.from_date)].reset_index(drop=True)

    print("  Computing ground-truth outcomes (CALL/PUT winners per day)...")
    labels = compute_labels(feat)   # cols: date, call_out, put_out, label

    df = feat.merge(labels, on="date", how="inner")

    # ── 2. Load ML signals ────────────────────────────────────────────────────
    sig_csv = f"{DATA_DIR}/signals_ml.csv"
    if args.signal_source == "ml" and os.path.exists(sig_csv):
        sig_df = pd.read_csv(sig_csv, parse_dates=["date"])
        sig_df = sig_df[["date", "signal", "ml_conf"]].rename(
            columns={"signal": "ml_signal_csv"})
        df = df.merge(sig_df, on="date", how="left")
        # Use CSV signal where available, fall back to rule_signal
        df["base_signal"] = df["ml_signal_csv"].where(
            df["ml_signal_csv"].isin(["CALL", "PUT"]),
            other=df["rule_signal"])
    else:
        df["base_signal"] = df["rule_signal"]

    # Keep only days where a trade was taken
    df = df[df["base_signal"].isin(["CALL", "PUT"])].copy()

    # ── 3. Add live context signals ───────────────────────────────────────────
    vix_open_chg = load_vix_open_chg()
    df["vix_open_chg"] = df["date"].map(vix_open_chg).fillna(float("nan"))

    # bn_gap, spf_gap, sp500_chg already in compute_features output
    df["bn_gap"]    = df["bn_gap"].fillna(0.0)
    df["spf_gap"]   = df["spf_gap"].fillna(0.0)
    df["sp500_chg"] = df["sp500_chg"].fillna(0.0)

    # ── 4. Premium estimate per day (for P&L simulation) ─────────────────────
    df["dte"]     = df["date"].apply(
        lambda x: get_dte(x.date() if hasattr(x, "date") else x))
    df["premium"] = df["bn_open"] * PREMIUM_K * (df["dte"].clip(lower=1) ** 0.5)

    # ── 5. Baseline accuracy ──────────────────────────────────────────────────
    def get_trade_outcome(row, signal):
        """Look up precomputed call_out / put_out for a given signal."""
        if signal == "CALL":
            return row["call_out"]
        return row["put_out"]

    df["base_outcome"] = df.apply(
        lambda r: get_trade_outcome(r, r["base_signal"]), axis=1)
    df["base_win"] = df["base_outcome"].apply(outcome_wins)
    df["base_pnl"] = df.apply(
        lambda r: pnl_for_outcome(r["base_outcome"], r["premium"]), axis=1)

    n_total    = len(df)
    base_acc   = df["base_win"].mean()
    base_pnl   = df["base_pnl"].sum()

    print(f"\n  Total trade days in backtest : {n_total}")
    print(f"  Date range                   : "
          f"{df['date'].min().date()} → {df['date'].max().date()}")

    print(f"\n  BASELINE ({args.signal_source.upper()} signal, no live context)")
    print(f"  {'─'*55}")
    print(f"  Accuracy  : {base_acc:.1%}  ({df['base_win'].sum()}/{n_total})")
    print(f"  Total P&L : ₹{base_pnl:+,.0f}  (formula premium, 1 lot)")

    # ── 6. Grid search over override thresholds ───────────────────────────────
    gap_thresholds  = [0.20, 0.30, 0.50, 0.75]   # BN gap % to trigger
    es_thresholds   = [0.10, 0.20, 0.30, 0.50]   # ES/SP500 down % to confirm trap
    vix_thresholds  = [1.0,  1.5,  2.0,  3.0]    # VIX open % surge threshold

    best = {"acc": base_acc, "pnl": base_pnl, "params": None, "df": None}

    n_combos = len(gap_thresholds) * len(es_thresholds) * len(vix_thresholds)
    print(f"\n  Grid-searching {n_combos} threshold combinations...")

    all_results = []

    for gap_t, es_t, vix_t in product(gap_thresholds, es_thresholds, vix_thresholds):
        rows = []
        for _, row in df.iterrows():
            ctx_sig, rule = apply_overrides(
                row["base_signal"],
                row["bn_gap"], row["spf_gap"], row["sp500_chg"],
                row["vix_open_chg"],
                gap_thresh=gap_t, es_down_thresh=es_t, vix_thresh=vix_t,
            )
            outcome = get_trade_outcome(row, ctx_sig)
            win     = outcome_wins(outcome)
            pnl     = pnl_for_outcome(outcome, row["premium"])
            rows.append({
                "date":         row["date"],
                "base_signal":  row["base_signal"],
                "ctx_signal":   ctx_sig,
                "rule":         rule or "",
                "overridden":   rule is not None,
                "base_win":     row["base_win"],
                "ctx_win":      win,
                "ctx_pnl":      pnl,
            })

        rdf     = pd.DataFrame(rows)
        ctx_acc = rdf["ctx_win"].mean()
        ctx_pnl = rdf["ctx_pnl"].sum()

        all_results.append({
            "gap_t": gap_t, "es_t": es_t, "vix_t": vix_t,
            "acc": ctx_acc, "pnl": ctx_pnl,
            "n_overrides": rdf["overridden"].sum(),
        })

        if ctx_acc > best["acc"]:
            best = {"acc": ctx_acc, "pnl": ctx_pnl,
                    "params": (gap_t, es_t, vix_t), "df": rdf}

    # ── 7. Best result ────────────────────────────────────────────────────────
    print(f"\n  BEST LIVE CONTEXT OVERRIDE")
    print(f"  {'─'*55}")

    if best["params"] is None:
        print(f"  No threshold combination beat baseline — live context doesn't help")
        print(f"  (Baseline accuracy {base_acc:.1%} is already the ceiling with this data)")
    else:
        gap_t, es_t, vix_t = best["params"]
        rdf = best["df"]
        n_ov = rdf["overridden"].sum()

        print(f"  Thresholds   : BN gap ≥ {gap_t:.2f}%  |  ES down ≥ {es_t:.2f}%  "
              f"|  VIX surge ≥ {vix_t:.1f}%")
        print(f"  Accuracy     : {best['acc']:.1%}  ({rdf['ctx_win'].sum()}/{n_total})")
        print(f"  Delta vs base: {best['acc'] - base_acc:+.1%}")
        print(f"  Total P&L    : ₹{best['pnl']:+,.0f}  (formula premium, 1 lot)")
        print(f"  P&L delta    : ₹{best['pnl'] - base_pnl:+,.0f}")
        print(f"  Overrides    : {n_ov} of {n_total} days ({n_ov/n_total:.1%})")

        # Quality of overrides
        ov = rdf[rdf["overridden"]]
        if len(ov):
            rescued = ((~ov["base_win"]) & ov["ctx_win"]).sum()   # wrong→right
            damaged = (ov["base_win"]  & ~ov["ctx_win"]).sum()    # right→wrong
            print(f"\n  OVERRIDE QUALITY  (on {len(ov)} flipped days)")
            print(f"  {'─'*55}")
            print(f"  Rescued (wrong→right) : {rescued}")
            print(f"  Damaged (right→wrong) : {damaged}")
            print(f"  Net balance           : {rescued - damaged:+d}")
            print(f"  Override accuracy     : {ov['ctx_win'].mean():.1%}  "
                  f"(base on same days: {ov['base_win'].mean():.1%})")

        # Per-rule breakdown
        rule_grp = rdf[rdf["overridden"]].groupby("rule")
        if len(rule_grp) > 0:
            print(f"\n  PER-RULE BREAKDOWN")
            print(f"  {'─'*55}")
            print(f"  {'Rule':<25}  {'N':>4}  {'Base acc':>8}  {'Ctx acc':>8}  {'Delta':>6}")
            for rule_name, grp in rule_grp:
                b_acc = grp["base_win"].mean()
                c_acc = grp["ctx_win"].mean()
                print(f"  {rule_name:<25}  {len(grp):>4}  {b_acc:>8.1%}  {c_acc:>8.1%}  "
                      f"{c_acc-b_acc:>+6.1%}")

        # Sample of flipped days
        rescued_df = ov[(~ov["base_win"]) & ov["ctx_win"]].head(8)
        if len(rescued_df):
            print(f"\n  SAMPLE RESCUES  (override turned loss into win)")
            print(f"  {'─'*55}")
            print(f"  {'Date':<12}  {'Was':>4} → {'Now':>4}  {'Rule'}")
            for _, r in rescued_df.iterrows():
                print(f"  {str(r['date'].date()):<12}  "
                      f"{r['base_signal']:>4} → {r['ctx_signal']:>4}  {r['rule']}")

        damaged_df = ov[ov["base_win"] & ~ov["ctx_win"]].head(8)
        if len(damaged_df):
            print(f"\n  SAMPLE DAMAGE  (override turned win into loss)")
            print(f"  {'─'*55}")
            print(f"  {'Date':<12}  {'Was':>4} → {'Now':>4}  {'Rule'}")
            for _, r in damaged_df.iterrows():
                print(f"  {str(r['date'].date()):<12}  "
                      f"{r['base_signal']:>4} → {r['ctx_signal']:>4}  {r['rule']}")

    # ── 8. Top-10 threshold combinations ─────────────────────────────────────
    top10 = sorted(all_results, key=lambda x: x["acc"], reverse=True)[:10]
    print(f"\n  TOP 10 THRESHOLD COMBINATIONS")
    print(f"  {'─'*65}")
    print(f"  {'gap%':>5}  {'es%':>5}  {'vix%':>5}  {'acc':>6}  {'Δacc':>6}  "
          f"{'P&L':>10}  {'overrides':>9}")
    for r in top10:
        print(f"  {r['gap_t']:>5.2f}  {r['es_t']:>5.2f}  {r['vix_t']:>5.1f}  "
              f"{r['acc']:>6.1%}  {r['acc']-base_acc:>+6.1%}  "
              f"₹{r['pnl']:>9,.0f}  {r['n_overrides']:>5}/{n_total}")

    # ── 9. Verdict ────────────────────────────────────────────────────────────
    print(f"\n  VERDICT")
    print(f"  {'─'*55}")
    best_delta = best["acc"] - base_acc if best["params"] else 0
    if best_delta >= 0.05:
        print(f"  ✅  Live context gives a real edge: +{best_delta:.1%} accuracy")
        print(f"      Recommended: wire into auto_trader.py as hard override")
    elif best_delta > 0:
        print(f"  ⚠   Marginal improvement: +{best_delta:.1%}")
        print(f"      Worth adding as a confidence modifier, not a hard override")
    else:
        print(f"  ❌  Live context doesn't improve accuracy on historical data")
        print(f"      Daily signals already capture this information")
    print(f"\n{'═'*65}\n")


if __name__ == "__main__":
    main()
