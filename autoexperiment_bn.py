#!/usr/bin/env python3
"""
autoexperiment_bn.py — Fast single-experiment metric runner for BN autoresearch.

Imports ml_engine.py so any code changes there take effect immediately.
Trains a fixed-param RF on all data except the last 252 days, evaluates on
the holdout, and prints composite score as a single JSON line.

No Optuna, no HPO — deterministic. Runtime: ~30–60 seconds.

Usage:
    python3 autoexperiment_bn.py
    → {"composite": 0.734, "pnl_proxy": 0.68, "n_val": 252, "n_train": 1423}

Used by: autoloop_bn.py (reads stdout, parses JSON)
"""

import argparse
import importlib
import json
import sys
import os
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning")

HOLDOUT_DAYS = 252  # ~1 year temporal holdout — must match model_evolver.py


def _composite(y_true, y_pred) -> float:
    """
    Composite score — matches model_evolver.py _score() exactly:
    0.50 × accuracy + 0.25 × recall_CALL + 0.25 × recall_PUT
    """
    from sklearn.metrics import accuracy_score, recall_score
    acc      = accuracy_score(y_true, y_pred)
    rec_call = recall_score(y_true, y_pred, pos_label=1, zero_division=0)
    rec_put  = recall_score(y_true, y_pred, pos_label=0, zero_division=0)
    return round(0.50 * acc + 0.25 * rec_call + 0.25 * rec_put, 4)


def run():
    # Parse --module arg to allow testing different versions (ml_engine_paper, etc.)
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="ml_engine", help="ML engine module to use")
    args = parser.parse_args()

    # Import ml_engine or ml_engine_paper fresh — code changes take effect since each run is a new process
    mle = importlib.import_module(args.module)

    # ── Load data + compute features ─────────────────────────────────────────
    try:
        df = mle.compute_features(mle.load_all_data())
    except Exception as e:
        print(json.dumps({"error": f"compute_features failed: {e}", "composite": 0.0}))
        sys.exit(1)

    # ── Compute direction labels ──────────────────────────────────────────────
    try:
        labels_df = mle.compute_labels(df)
    except Exception as e:
        print(json.dumps({"error": f"compute_labels failed: {e}", "composite": 0.0}))
        sys.exit(1)

    df = df.merge(labels_df[["date", "label"]], on="date", how="inner")

    # Drop rows missing any required feature or label
    feat_cols = mle.FEATURE_COLS
    missing   = [c for c in feat_cols if c not in df.columns]
    if missing:
        print(json.dumps({"error": f"missing columns: {missing}", "composite": 0.0}))
        sys.exit(1)

    df = df.dropna(subset=feat_cols + ["label"])

    min_rows = HOLDOUT_DAYS + 100
    if len(df) < min_rows:
        print(json.dumps({
            "error": f"only {len(df)} rows after dropna — need >{min_rows}",
            "composite": 0.0,
        }))
        sys.exit(1)

    # ── Temporal split ────────────────────────────────────────────────────────
    split   = len(df) - HOLDOUT_DAYS
    X_train = df[feat_cols].iloc[:split].values
    X_val   = df[feat_cols].iloc[split:].values
    y_train = (df["label"].iloc[:split] == "CALL").astype(int).values
    y_val   = (df["label"].iloc[split:] == "CALL").astype(int).values

    # ── Fixed RF — same params as walk-forward training in ml_engine.py ──────
    from sklearn.ensemble import RandomForestClassifier
    rf = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=3,
        max_features="sqrt",
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    y_pred = rf.predict(X_val)

    composite = _composite(y_val, y_pred)
    pnl_proxy = round(float((y_pred == y_val).mean()), 4)

    print(json.dumps({
        "composite": composite,
        "pnl_proxy": pnl_proxy,
        "n_val":     int(len(y_val)),
        "n_train":   int(len(y_train)),
    }))


if __name__ == "__main__":
    run()
