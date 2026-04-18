#!/usr/bin/env python3
"""
autoexperiment_bn.py — Fast single-experiment metric runner for BN autoresearch.

Imports ml_engine.py so any code changes there take effect immediately.
Trains a fixed-param classifier on all data except the last 252 days,
evaluates on the holdout, and prints composite score as a single JSON line.

Model selection: reads models/champion_meta.json and trains a fresh model of
the SAME family (CatBoost / XGBoost / LightGBM / RandomForest) with fixed
deterministic params. This keeps research aligned with production — a feature
that helps the live champion is kept; one that only helps RF is discarded.
Falls back to RandomForest if champion_meta is missing.

No Optuna, no HPO — deterministic. Runtime: ~15–60 seconds depending on model.

Usage:
    python3 autoexperiment_bn.py
    → {"composite": 0.734, "pnl_proxy": 0.68, "n_val": 252, "n_train": 1423, "model": "CatBoost"}

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


def _build_eval_model():
    """Return (model, name) — match current champion type for research↔production alignment.

    Reads models/champion_meta.json. Falls back to RF if meta is missing or
    the required package isn't installed. All models use fixed deterministic
    params (random_state=42) so experiment-to-experiment scores are comparable.
    """
    from sklearn.ensemble import RandomForestClassifier

    try:
        with open("models/champion_meta.json") as f:
            champ = json.load(f).get("model_type", "").lower()
    except Exception:
        champ = ""

    if "cat" in champ:
        try:
            from catboost import CatBoostClassifier
            return CatBoostClassifier(
                iterations=300, depth=6, learning_rate=0.05,
                l2_leaf_reg=3.0, random_seed=42, thread_count=-1,
                auto_class_weights="Balanced", verbose=False,
            ), "CatBoost"
        except ImportError:
            pass

    if "xgb" in champ or "xgboost" in champ:
        try:
            from xgboost import XGBClassifier
            return XGBClassifier(
                n_estimators=300, max_depth=6, learning_rate=0.05,
                reg_lambda=1.0, random_state=42, n_jobs=-1,
                tree_method="hist", eval_metric="logloss",
            ), "XGBoost"
        except ImportError:
            pass

    if "lgb" in champ or "light" in champ:
        try:
            from lightgbm import LGBMClassifier
            return LGBMClassifier(
                n_estimators=300, max_depth=6, learning_rate=0.05,
                num_leaves=31, random_state=42, n_jobs=-1,
                class_weight="balanced", verbose=-1,
            ), "LightGBM"
        except ImportError:
            pass

    return RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=3,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    ), "RandomForest"


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
    import numpy as np
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

    # ── Model: match current champion so research aligns with production ─────
    # Reads models/champion_meta.json → uses same model family (CatBoost/XGB/LGB/RF)
    # with deterministic fixed params. If a feature helps the live champion, keep
    # it; if it only helps RF but not the ensemble, discard. Falls back to RF if
    # champion_meta is missing or the required package isn't installed.
    model, model_name = _build_eval_model()
    model.fit(X_train, y_train)
    y_pred       = model.predict(X_val)
    y_train_pred = model.predict(X_train)

    composite       = _composite(y_val, y_pred)
    train_composite = _composite(y_train, y_train_pred)

    # ── Leakage guard ────────────────────────────────────────────────────────
    # True leakage: model peeks at same-day close/high/low that don't exist
    # at 9:30 AM entry. Signature: BOTH train AND holdout are anomalously high
    # (leaky features inflate BOTH splits since holdout is also historical data).
    # Pure overfitting: train high, holdout near-random (0.50-0.55). That is NOT
    # leakage — it just means features are weak; the autoloop discards it anyway.
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
            f"RF memorising leaked signal. "
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
    }))


if __name__ == "__main__":
    run()
