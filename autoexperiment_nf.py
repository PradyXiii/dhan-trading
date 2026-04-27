#!/usr/bin/env python3
# ─── REAL-OPTIONS RULE (April 2026) ──────────────────────────────────────────
# Composite score here measures DIRECTIONAL signal quality only. It does not
# reflect real option P&L — ignores theta, IV compression, slippage.
# Before promoting any paper model or config change to live, cross-validate
# with `python3 backtest_engine.py --real-options --ml`.
# See "REAL-OPTIONS RULE" in CLAUDE.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
autoexperiment_nf.py — Fast single-experiment metric runner for NF IC autoresearch.

Imports ml_engine.py so any code changes there take effect immediately.
Trains all 4 fixed-param classifiers (CatBoost, XGBoost, LightGBM, RandomForest),
combines their predictions via majority vote (exactly as production does), and
prints composite score as a single JSON line.

This mirrors the live ensemble: a feature that only helps RF but not the
other 3 models will show no improvement in ensemble vote, and gets discarded.

No Optuna, no HPO — deterministic. Runtime: ~30–90 seconds for all 4 models.

Usage:
    python3 autoexperiment_nf.py
    → {"composite": 0.734, "pnl_proxy": 0.68, "n_val": 252, "n_train": 1423,
       "model": "Ensemble(CAT+XGB+LGB+RF)", "scores": {"CatBoost": 0.71, ...}}

Used by: autoloop_nf.py (reads stdout, parses JSON)
"""

import argparse
import argparse
import importlib
import json
import sys
import os
import warnings

import numpy as np

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning")

HOLDOUT_DAYS = 252  # ~1 year temporal holdout — must match model_evolver.py


def _build_all_models() -> list[tuple]:
    """Return list of (model, name) for all 4 model families with fixed deterministic params.
    Skips any package that isn't installed. Always includes RandomForest as fallback.
    """
    from sklearn.ensemble import RandomForestClassifier
    models = []

    try:
        from catboost import CatBoostClassifier
        models.append((CatBoostClassifier(
            iterations=300, depth=6, learning_rate=0.05,
            l2_leaf_reg=3.0, random_seed=42, thread_count=-1,
            auto_class_weights="Balanced", verbose=False,
        ), "CatBoost"))
    except ImportError:
        pass

    try:
        from xgboost import XGBClassifier
        models.append((XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.05,
            reg_lambda=1.0, random_state=42, n_jobs=-1,
            tree_method="hist", eval_metric="logloss",
        ), "XGBoost"))
    except ImportError:
        pass

    try:
        from lightgbm import LGBMClassifier
        models.append((LGBMClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.05,
            num_leaves=31, random_state=42, n_jobs=-1,
            class_weight="balanced", verbose=-1,
        ), "LightGBM"))
    except ImportError:
        pass

    models.append((RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=3,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    ), "RandomForest"))

    try:
        from model_evolver import TabNetWrapper
        # Confirm pytorch-tabnet is importable before adding to ensemble
        from pytorch_tabnet.tab_model import TabNetClassifier  # noqa: F401
        models.append((TabNetWrapper(
            n_d=16, n_a=16, n_steps=3, lr=0.02,
            max_epochs=30, patience=8, batch_size=128,
        ), "TabNet"))
    except ImportError:
        pass

    return models


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

    # Diagnose NaN-heavy columns before dropna (helps Claude fix bad rolling windows)
    nan_culprits = []
    for col in feat_cols:
        nan_pct = df[col].isna().mean()
        if nan_pct > 0.30:
            nan_culprits.append(f"{col}={nan_pct:.0%}NaN")

    df = df.dropna(subset=feat_cols + ["label"])

    min_rows = HOLDOUT_DAYS + 100
    if len(df) < min_rows:
        culprit_str = f" (high-NaN cols: {nan_culprits})" if nan_culprits else ""
        print(json.dumps({
            "error": f"only {len(df)} rows after dropna — need >{min_rows}{culprit_str}",
            "composite": 0.0,
        }))
        sys.exit(1)

    # ── Temporal split ────────────────────────────────────────────────────────
    split   = len(df) - HOLDOUT_DAYS
    X_train = df[feat_cols].iloc[:split].values
    X_val   = df[feat_cols].iloc[split:].values
    y_train = (df["label"].iloc[:split] == "CALL").astype(int).values
    y_val   = (df["label"].iloc[split:] == "CALL").astype(int).values

    # ── Leakage guard: reject any feature with absurd |corr| with the label ──
    # The label depends on today's close-open sign, so same-day close/high/low
    # features can leak. Compute on TRAIN only so holdout stays untouched.
    leaks = []
    for i, col in enumerate(feat_cols):
        x = X_train[:, i]
        if np.std(x) == 0:
            continue
        corr = np.corrcoef(x, y_train)[0, 1]
        if abs(corr) > 0.85:
            leaks.append(f"{col}={corr:+.2f}")
    if leaks:
        print(json.dumps({
            "error": f"label leakage suspected (|corr|>0.85 on train): {leaks}",
            "composite": 0.0,
        }))
        sys.exit(1)

    # ── Ensemble: train all 4 models, combine via majority vote ──────────────
    # Mirrors production exactly: all 4 models vote, majority wins.
    # A feature that only helps RF but not the other 3 won't move the ensemble
    # composite — so the autoloop correctly discards it.
    all_models = _build_all_models()
    val_preds   = []   # shape: (n_models, n_val)
    train_preds = []
    individual_scores = {}

    for model, name in all_models:
        model.fit(X_train, y_train)
        vp = model.predict(X_val)
        tp = model.predict(X_train)
        val_preds.append(vp)
        train_preds.append(tp)
        individual_scores[name] = _composite(y_val, vp)

    # Majority vote (ties go to 1 = CALL, matching production tie-break)
    val_votes   = np.array(val_preds)    # (n_models, n_val)
    train_votes = np.array(train_preds)
    y_pred       = (val_votes.sum(axis=0) >= len(all_models) / 2).astype(int)
    y_train_pred = (train_votes.sum(axis=0) >= len(all_models) / 2).astype(int)

    composite       = _composite(y_val, y_pred)
    train_composite = _composite(y_train, y_train_pred)
    model_name      = f"Ensemble({'|'.join(n for _, n in all_models)})"

    # ── Leakage guard ────────────────────────────────────────────────────────
    leak_reason = None
    if composite > 0.90:
        leak_reason = (
            f"holdout composite {composite:.4f} exceeds cap 0.90 — "
            f"likely label leakage (train={train_composite:.4f}). "
            "Check for same-day close/high/low in rolling windows "
            "(must call .shift(1) BEFORE .rolling()/.ewm())."
        )
    elif train_composite > 0.98 and composite > 0.70:
        leak_reason = (
            f"train composite {train_composite:.4f} exceeds cap 0.98 "
            f"with holdout {composite:.4f} > 0.70 — "
            "Find the feature missing .shift(1) before rolling/pct_change/ewm."
        )
    if leak_reason:
        print(json.dumps({"error": leak_reason, "composite": 0.0}))
        sys.exit(1)

    pnl_proxy = round(float((y_pred == y_val).mean()), 4)

    print(json.dumps({
        "composite": composite,
        "pnl_proxy": pnl_proxy,
        "n_val":     int(len(y_val)),
        "n_train":   int(len(y_train)),
        "model":     model_name,
        "scores":    {k: round(v, 4) for k, v in individual_scores.items()},
    }))


if __name__ == "__main__":
    run()
