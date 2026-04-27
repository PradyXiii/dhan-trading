#!/usr/bin/env python3
"""
analyze_confidence.py — Two diagnostic questions:

1. Is accuracy higher on high-confidence predictions?
   (Precision @ top decile — if yes, filter to high-confidence trades only)

2. Is accuracy regime-dependent (VIX level)?
   (Regime clustering — if yes, stop trading in low-accuracy regimes)

Run: python3 analyze_confidence.py
     python3 analyze_confidence.py --write-threshold   # update data/vix_threshold.json
     python3 analyze_confidence.py --module ml_engine_paper --write-threshold
"""
import argparse
import warnings; warnings.filterwarnings("ignore")
import os; os.environ.setdefault("PYTHONWARNINGS","ignore::UserWarning")

import json
import numpy as np
import importlib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score

HOLDOUT_DAYS   = 252
DATA_DIR       = "data"
THRESHOLD_FILE = f"{DATA_DIR}/vix_threshold.json"
MIN_ACC_TO_TRADE = 0.52   # model must clear this bar for a VIX band to be tradeable
VIX_FLOOR      = 10.0     # never lower threshold below this
VIX_CEIL       = 18.0     # never raise threshold above this


def _build_model_and_split(mle):
    df = mle.compute_features(mle.load_all_data())
    labels_df = mle.compute_labels(df)
    df = df.merge(labels_df[["date","label"]], on="date", how="inner")
    df = df.dropna(subset=mle.FEATURE_COLS + ["label"])

    feat_cols = mle.FEATURE_COLS
    split     = len(df) - HOLDOUT_DAYS

    X_train = df[feat_cols].iloc[:split].values
    X_val   = df[feat_cols].iloc[split:].values
    y_train = (df["label"].iloc[:split] == "CALL").astype(int).values
    y_val   = (df["label"].iloc[split:] == "CALL").astype(int).values

    rf = RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=3,
        max_features="sqrt", class_weight="balanced", random_state=42, n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    proba     = rf.predict_proba(X_val)[:, 1]
    vix_vals  = df["vix_level"].iloc[split:].values

    return feat_cols, rf, proba, y_val, vix_vals, split, df


def compute_optimal_threshold(proba, y_val, vix_vals) -> dict:
    """
    Find the lowest VIX threshold where model accuracy on VIX >= T is still >= MIN_ACC_TO_TRADE.
    Returns dict with 'vix_min_trade', 'accuracy_at_threshold', 'n_trades', 'reason'.
    """
    best_threshold = VIX_CEIL   # conservative fallback
    best_acc       = 0.0
    best_n         = 0
    bucket_report  = []

    for t in range(int(VIX_FLOOR), int(VIX_CEIL) + 1):
        mask = vix_vals >= t
        n    = int(mask.sum())
        if n < 20:
            continue
        acc = accuracy_score(y_val[mask], (proba[mask] >= 0.5).astype(int))
        # Statistical-significance gate: a one-sided binomial test against the
        # 50% null. We require P(acc | random) < 0.05 — protects against
        # picking a noisy bucket where 13/20 ≈ 65% looks great but is within
        # one-tail noise. With n>=20, MIN_ACC_TO_TRADE=0.52 alone is too lax.
        from scipy.stats import binomtest
        wins = int(round(acc * n))
        p_value = binomtest(wins, n, p=0.5, alternative="greater").pvalue
        bucket_report.append((t, acc, n, p_value))
        if acc >= MIN_ACC_TO_TRADE and p_value < 0.05:
            if t < best_threshold:
                best_threshold = t
                best_acc       = acc
                best_n         = n

    threshold = float(max(VIX_FLOOR, min(best_threshold, VIX_CEIL)))
    # Ceiling — preserved across nightly rewrites so auto_trader keeps the panic-VIX cap.
    # Set by real-options backtest (Oct-24 → Apr-26: VIX∈[12,20] = best ≥60%-retention filter).
    vix_max = 20.0
    return {
        "vix_min_trade":          threshold,
        "vix_max_trade":          vix_max,
        "accuracy_at_threshold":  round(best_acc, 4),
        "n_trades_in_holdout":    best_n,
        "n_holdout_total":        int(len(y_val)),
        "coverage_pct":           round(best_n / max(len(y_val), 1), 4),
        "min_acc_required":       MIN_ACC_TO_TRADE,
        "bucket_report":          [(t, round(a, 4), n) for t, a, n in bucket_report],
        "reason":                 (
            f"Lowest VIX≥{threshold:.0f} where accuracy ({best_acc:.1%}) "
            f">= {MIN_ACC_TO_TRADE:.0%} over {best_n} holdout days. "
            f"Ceiling fixed at {vix_max:.0f} (panic regime)."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-threshold", action="store_true",
                        help="Compute optimal VIX threshold and write to data/vix_threshold.json")
    parser.add_argument("--module", default="ml_engine",
                        help="ML engine module to use (default: ml_engine)")
    args = parser.parse_args()

    mle = importlib.import_module(args.module)
    feat_cols, rf, proba, y_val, vix_vals, split, df = _build_model_and_split(mle)
    confidence = np.abs(proba - 0.5) * 2

    print("\n══ 1. Precision @ Confidence Buckets ══")
    print(f"{'Threshold':>12}  {'N trades':>9}  {'Accuracy':>9}  {'% of days':>9}")
    for thresh in [0.0, 0.10, 0.20, 0.30, 0.40]:
        mask = confidence >= thresh
        n = mask.sum()
        if n == 0: continue
        acc = accuracy_score(y_val[mask], (proba[mask] >= 0.5).astype(int))
        print(f"  conf>={thresh:.0%}   {n:>9}  {acc:>9.1%}  {n/len(y_val):>9.1%}")

    print("\n══ 2. Accuracy by VIX Regime ══")
    buckets = [(0, 13, "Low  (VIX<13)"), (13, 18, "Mid  (13-18)"),
               (18, 25, "High (18-25)"), (25, 999, "Spike(>25  )")]
    for lo, hi, label in buckets:
        mask = (vix_vals >= lo) & (vix_vals < hi)
        n = mask.sum()
        if n < 10: continue
        acc = accuracy_score(y_val[mask], (proba[mask] >= 0.5).astype(int))
        print(f"  {label}:  n={n:>3}  acc={acc:.1%}")

    print("\n══ 3. Top 10 Feature Importances ══")
    imp = sorted(zip(feat_cols, rf.feature_importances_), key=lambda x: -x[1])
    for name, score in imp[:10]:
        print(f"  {name:<22} {score:.3f}  {'█' * int(score*200)}")

    if args.write_threshold:
        result = compute_optimal_threshold(proba, y_val, vix_vals)
        import datetime
        result["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result["module"]     = args.module

        os.makedirs(DATA_DIR, exist_ok=True)
        from atomic_io import write_atomic_json
        write_atomic_json(THRESHOLD_FILE, result)

        print(f"\n══ 4. Dynamic VIX Threshold ══")
        print(f"  New VIX_MIN_TRADE = {result['vix_min_trade']:.1f}")
        print(f"  Accuracy @ threshold: {result['accuracy_at_threshold']:.1%}")
        print(f"  Trades in holdout: {result['n_trades_in_holdout']} / {result['n_holdout_total']} "
              f"({result['coverage_pct']:.0%} of days)")
        print(f"  Written → {THRESHOLD_FILE}")
        print(f"  {result['reason']}")

    print()


if __name__ == "__main__":
    main()
