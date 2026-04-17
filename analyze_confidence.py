#!/usr/bin/env python3
"""
analyze_confidence.py — Two diagnostic questions:

1. Is accuracy higher on high-confidence predictions?
   (Precision @ top decile — if yes, filter to high-confidence trades only)

2. Is accuracy regime-dependent (VIX level)?
   (Regime clustering — if yes, stop trading in low-accuracy regimes)

Run: python3 analyze_confidence.py
"""
import warnings; warnings.filterwarnings("ignore")
import os; os.environ.setdefault("PYTHONWARNINGS","ignore::UserWarning")

import numpy as np
import importlib
mle = importlib.import_module("ml_engine")
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score

HOLDOUT_DAYS = 252

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
proba = rf.predict_proba(X_val)[:, 1]   # P(CALL)
confidence = np.abs(proba - 0.5) * 2    # 0=no edge, 1=certain

print("\n══ 1. Precision @ Confidence Buckets ══")
print(f"{'Threshold':>12}  {'N trades':>9}  {'Accuracy':>9}  {'% of days':>9}")
for thresh in [0.0, 0.10, 0.20, 0.30, 0.40]:
    mask = confidence >= thresh
    n = mask.sum()
    if n == 0: continue
    acc = accuracy_score(y_val[mask], (proba[mask] >= 0.5).astype(int))
    print(f"  conf>={thresh:.0%}   {n:>9}  {acc:>9.1%}  {n/len(y_val):>9.1%}")

print("\n══ 2. Accuracy by VIX Regime ══")
vix_vals = df["vix_level"].iloc[split:].values
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

print()
