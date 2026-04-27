#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
model_evolver.py — Nightly autonomous ML training engine.

Runs at 11 PM IST (cron: 30 17 * * 1-5).
Fetches fresh data, engineers features, runs Optuna HPO across
RandomForest + XGBoost + LightGBM, picks the champion model,
saves it to models/champion.pkl, and sends a plain-English Telegram report.

Morning path (ml_engine.py --predict-today) loads champion.pkl in <5 sec.

Usage:
  python3 model_evolver.py            # full nightly run
  python3 model_evolver.py --no-data  # skip data refresh (use existing CSVs)
  python3 model_evolver.py --trials N # Optuna trials per model (default 40)
"""

import os
import sys
import json
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning")

from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR   = "data"
MODELS_DIR = "models"
HOLDOUT_DAYS = 252   # ~1 year temporal holdout for champion selection

# Optional advanced libs — imported lazily
try:
    import shap as _shap
    _SHAP_OK = True
except ImportError:
    _SHAP_OK = False

try:
    from river.drift import ADWIN as _ADWIN
    _RIVER_OK = True
except ImportError:
    _RIVER_OK = False

# ── Weighting constants ────────────────────────────────────────────────────────
VIX_HIGH_THRESHOLD = 17.0   # VIX above this = high-fear day → 3× weight penalty
VIX_HIGH_WEIGHT    = 3.0    # multiplier for high-VIX rows in training
MAG_CLIP           = 3.0    # clip nf_ret1 magnitude before weighting

# ── CLI flags ──────────────────────────────────────────────────────────────────
SKIP_DATA_REFRESH = "--no-data" in sys.argv
N_TRIALS = 30   # 30 trials: good balance of HPO quality vs run time
for _i, _a in enumerate(sys.argv):
    if _a == "--trials" and _i + 1 < len(sys.argv):
        try:
            N_TRIALS = int(sys.argv[_i + 1])
        except ValueError:
            pass

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — DATA REFRESH
# ─────────────────────────────────────────────────────────────────────────────

def refresh_data():
    """Refresh all data CSVs using data_fetcher.py functions."""
    print("\n[1/6] Refreshing data...")

    from data_fetcher import (
        fetch_dhan_index, fetch_yfinance,
        fetch_gold, fetch_crude, fetch_usdinr, fetch_dxy, fetch_us10y,
        fetch_fii_today, fetch_pcr_dhan_today, fetch_rollingoption,
        FROM_DATE, TO_DATE, DATA_DIR as DF_DIR,
    )
    os.makedirs(DF_DIR, exist_ok=True)

    fetches = [
        ("nifty50.csv",      lambda: fetch_dhan_index("13", "Nifty50",   FROM_DATE, TO_DATE)),
        ("india_vix.csv",    lambda: fetch_yfinance("^INDIAVIX",  "India VIX",  FROM_DATE, TO_DATE)),
        ("sp500.csv",        lambda: fetch_yfinance("^GSPC",      "S&P500",     FROM_DATE, TO_DATE)),
        ("nikkei.csv",       lambda: fetch_yfinance("^N225",      "Nikkei225",  FROM_DATE, TO_DATE)),
        ("sp500_futures.csv",lambda: fetch_yfinance("ES=F",       "S&P Futures",FROM_DATE, TO_DATE)),
        ("gold.csv",         lambda: fetch_gold(FROM_DATE, TO_DATE)),
        ("crude.csv",        lambda: fetch_crude(FROM_DATE, TO_DATE)),
        ("usdinr.csv",       lambda: fetch_usdinr(FROM_DATE, TO_DATE)),
        ("dxy.csv",          lambda: fetch_dxy(FROM_DATE, TO_DATE)),
        ("us10y.csv",        lambda: fetch_us10y(FROM_DATE, TO_DATE)),
    ]

    for fname, fn in fetches:
        try:
            df = fn()
            if not df.empty:
                df.to_csv(f"{DF_DIR}/{fname}", index=False)
                print(f"  {fname}: {len(df)} rows saved")
        except Exception as e:
            print(f"  {fname}: ERROR — {e}")

    # Live snapshots (today only)
    try:
        fetch_fii_today()
    except Exception as e:
        print(f"  fii_today: {e}")
    try:
        fetch_pcr_dhan_today()
    except Exception as e:
        print(f"  pcr_dhan_today: {e}")

    # Historical ATM option premiums — incremental update (only new rows fetched)
    try:
        fetch_rollingoption(FROM_DATE, TO_DATE)
    except Exception as e:
        print(f"  rollingoption: {e}")

    print("  Data refresh complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — FEATURE ENGINEERING (extended set)
# ─────────────────────────────────────────────────────────────────────────────

def load_extended_data():
    """Load all CSVs and return merged DataFrame with base + extended columns."""
    from ml_engine import load_all_data, compute_features

    df = compute_features(load_all_data())

    # ── New optional data sources ─────────────────────────────────────────────
    extras = {
        "gold":   (f"{DATA_DIR}/gold.csv",   "gold_close"),
        "crude":  (f"{DATA_DIR}/crude.csv",  "crude_close"),
        "usdinr": (f"{DATA_DIR}/usdinr.csv", "usdinr_close"),
        "dxy":    (f"{DATA_DIR}/dxy.csv",    "dxy_close"),
        "us10y":  (f"{DATA_DIR}/us10y.csv",  "us10y_close"),
    }
    for key, (path, col) in extras.items():
        if col in df.columns:
            # Already loaded by ml_engine.load_all_data() — skip to avoid duplicate columns
            pass
        elif os.path.exists(path):
            tmp = pd.read_csv(path, parse_dates=["date"])
            tmp = tmp[["date", "close"]].rename(columns={"close": col})
            df = df.merge(tmp, on="date", how="left")
            df[col] = df[col].ffill(limit=5)
        else:
            df[col] = np.nan

    # PCR: already loaded by ml_engine.compute_features() → guaranteed in df
    # Just ensure column exists (defensive)
    if "pcr" not in df.columns:
        df["pcr"] = np.nan

    # FII futures net position
    fii_loaded = False
    for fii_path in [f"{DATA_DIR}/fii_dii.csv"]:
        if os.path.exists(fii_path):
            fii = pd.read_csv(fii_path, parse_dates=["date"])
            if "fii_net_fut" in fii.columns:
                fii = fii[["date", "fii_net_fut"]]
                df  = df.merge(fii, on="date", how="left")
                df["fii_net_fut"] = df["fii_net_fut"].ffill(limit=3)
                fii_loaded = True
            break
    if not fii_loaded:
        df["fii_net_fut"] = np.nan

    return df


def compute_extended_features(df):
    """Add the 10 new features on top of the 21 base features from ml_engine."""
    d = df.copy()

    # Gold / crude / macro returns
    for col, ret_col in [
        ("gold_close",   "gold_ret"),
        ("crude_close",  "crude_ret"),
        ("usdinr_close", "usdinr_ret"),
        ("dxy_close",    "dxy_ret"),
    ]:
        if col in d.columns:
            d[ret_col] = (d[col] - d[col].shift(1)) / d[col].shift(1) * 100
        else:
            d[ret_col] = 0.0

    # US 10Y yield daily change (basis points)
    if "us10y_close" in d.columns:
        d["us10y_chg"] = (d["us10y_close"] - d["us10y_close"].shift(1)) * 100
    else:
        d["us10y_chg"] = 0.0

    # PCR — fill NaN with neutral value 1.0
    d["pcr"] = d["pcr"].fillna(1.0) if "pcr" in d.columns else 1.0

    # FII net futures (₹ Cr) — fill NaN with 0, then z-score across rolling 60d
    if "fii_net_fut" in d.columns:
        d["fii_net_fut"] = d["fii_net_fut"].fillna(0.0)
        roll_std = d["fii_net_fut"].rolling(60, min_periods=10).std().replace(0, np.nan)
        roll_mu  = d["fii_net_fut"].rolling(60, min_periods=10).mean()
        d["fii_net_fut"] = ((d["fii_net_fut"] - roll_mu) / roll_std).fillna(0.0)
    else:
        d["fii_net_fut"] = 0.0

    # NF 5-day momentum (different from trend5 which is pct chg over 5d)
    d["nf_ret5"] = (d["nf_close"] / d["nf_close"].shift(5) - 1) * 100

    # NF volume ratio vs 20-day average
    if "volume" in d.columns:
        d["nf_vol_ratio"] = d["volume"] / d["volume"].rolling(20, min_periods=5).mean()
        d["nf_vol_ratio"] = d["nf_vol_ratio"].fillna(1.0)
    else:
        d["nf_vol_ratio"] = 1.0

    # ATR(14) as % of close — intraday range regime proxy
    if all(c in d.columns for c in ["nf_high", "nf_low", "nf_close"]):
        high_low  = d["nf_high"] - d["nf_low"]
        high_prev = (d["nf_high"] - d["nf_close"].shift(1)).abs()
        low_prev  = (d["nf_low"]  - d["nf_close"].shift(1)).abs()
        tr        = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
        d["atr14_pct"] = tr.rolling(14, min_periods=5).mean() / d["nf_close"] * 100
    else:
        d["atr14_pct"] = 0.0

    return d


# Base features from ml_engine
BASE_FEATURE_COLS = [
    "s_ema20", "s_trend5", "s_vix", "s_nf_gap",
    "ema20_pct", "trend5", "vix_dir",
    "rsi14", "hv20", "nf_gap",
    "sp500_chg", "nikkei_chg", "spf_gap",
    "vix_level", "vix_pct_chg", "vix_hv_ratio",
    "nf_ret1", "nf_ret20",
    "dow", "dte",
]

EXTENDED_FEATURE_COLS = BASE_FEATURE_COLS + [
    "gold_ret", "crude_ret", "usdinr_ret", "dxy_ret", "us10y_chg",
    "pcr", "pcr_ma5", "pcr_chg",
    "vix_open_chg",
    "fii_net_cash_z", "fii_net_fut",
    "nf_ret5", "nf_vol_ratio", "atr14_pct",
]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — FEATURE SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def select_features(X, y, all_cols):
    """
    Feature selection using SHAP values when available (more accurate than impurity).
    Falls back to RF impurity importance if shap not installed.
    Always keeps all BASE_FEATURE_COLS (validated signal components).
    """
    from sklearn.ensemble import RandomForestClassifier

    rf = RandomForestClassifier(
        n_estimators=200, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    rf.fit(X, y)

    if _SHAP_OK:
        try:
            explainer  = _shap.TreeExplainer(rf)
            shap_vals  = explainer.shap_values(X)
            # Handle all shap output shapes:
            #   list of [class_0, class_1] arrays (legacy shap)
            #   3D array (n_samples, n_features, n_classes) (shap 0.50+)
            #   2D array (n_samples, n_features) (single output)
            if isinstance(shap_vals, list):
                shap_vals = shap_vals[1]   # class 1 = CALL
            shap_vals = np.asarray(shap_vals)
            if shap_vals.ndim == 3:
                # Take class 1 (CALL) along the last axis
                shap_vals = shap_vals[:, :, 1] if shap_vals.shape[2] >= 2 else shap_vals[:, :, 0]
            importances = np.abs(shap_vals).mean(axis=0)
            # Ensure 1D — collapse any leftover dims
            importances = np.asarray(importances).flatten()
            if importances.shape[0] != X.shape[1]:
                raise ValueError(f"shap importance shape mismatch: {importances.shape} vs {X.shape[1]} features")
            print("  Feature selection: using SHAP mean |value| (more accurate than impurity)")
        except Exception as e:
            print(f"  SHAP failed ({e}) — falling back to RF impurity")
            importances = rf.feature_importances_
    else:
        importances = rf.feature_importances_

    max_imp  = importances.max() if importances.max() > 0 else 1.0
    norm_imp = importances / max_imp * 100

    selected = []
    for col, imp in zip(all_cols, norm_imp):
        if col in BASE_FEATURE_COLS or imp >= 1.0:
            selected.append((col, round(float(imp), 2)))

    selected.sort(key=lambda x: -x[1])
    print(f"\n  Feature selection: {len(selected)} / {len(all_cols)} kept")
    for col, imp in selected[:8]:
        print(f"    {col:<20} {imp:5.1f}%")
    if len(selected) > 8:
        print(f"    ... (+{len(selected)-8} more)")

    return [c for c, _ in selected], selected  # (col_list, [(col, imp), ...])


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — MODEL COMPETITION (Optuna HPO × 3 model types)
# ─────────────────────────────────────────────────────────────────────────────

def _temporal_split(X, y, holdout=HOLDOUT_DAYS):
    """Split keeping time order: train = everything before holdout window."""
    n = len(X)
    if n <= holdout + 100:
        split = n // 2
    else:
        split = n - holdout
    return X[:split], y[:split], X[split:], y[split:]


def _score(y_true, y_pred, y_prob):
    """Balanced composite score: 50% acc + 25% recall_call + 25% recall_put."""
    from sklearn.metrics import accuracy_score, recall_score
    acc = accuracy_score(y_true, y_pred)
    # label 1=CALL, 0=PUT
    rec_call = recall_score(y_true, y_pred, pos_label=1, zero_division=0)
    rec_put  = recall_score(y_true, y_pred, pos_label=0, zero_division=0)
    return 0.50 * acc + 0.25 * rec_call + 0.25 * rec_put


def _build_model(model_type, params):
    if model_type == "rf":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(
            **params, class_weight="balanced", random_state=42, n_jobs=-1)
    elif model_type == "xgb":
        from xgboost import XGBClassifier
        return XGBClassifier(
            **params, use_label_encoder=False, eval_metric="logloss",
            random_state=42, n_jobs=-1, verbosity=0)
    elif model_type == "lgb":
        import lightgbm as lgb
        return lgb.LGBMClassifier(
            **params, class_weight="balanced", random_state=42, n_jobs=-1,
            verbose=-1)
    elif model_type == "cat":
        from catboost import CatBoostClassifier
        return CatBoostClassifier(
            **params, auto_class_weights="Balanced", random_seed=42,
            thread_count=-1, verbose=0)
    raise ValueError(f"Unknown model_type: {model_type}")


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE TRADE FEEDBACK — inject real outcomes + boost miss-day patterns
# ─────────────────────────────────────────────────────────────────────────────

LIVE_TRADES_PATHS   = [
    f"{DATA_DIR}/live_ic_trades.csv",       # IC trades (CALL signal days)
    f"{DATA_DIR}/live_spread_trades.csv",   # Bull Put / Bear Call (PUT signal days)
    f"{DATA_DIR}/live_straddle_trades.csv", # Straddle (capital ≥ ₹2.3L)
]
LIVE_TRADES_PATH    = LIVE_TRADES_PATHS[0]  # backward-compat alias
MIDDAY_CHECKPOINTS  = f"{DATA_DIR}/midday_checkpoints.csv"
LIVE_INJECT_WEIGHT  = 10.0  # live trade rows are 10× more valuable than synthetic labels
MIDDAY_MISS_WEIGHT  = 5.0   # midday reversal rows: confirmed wrong direction mid-session
MISS_BOOST          = 3.0   # historical rows matching miss patterns get 3× weight
MIN_LIVE_TRADES     = 3     # minimum labeled trades before feedback kicks in
MIN_MISSES          = 2     # minimum misses before boosting historical rows


def _load_live_outcomes(df_full, selected_cols):
    """
    Load live_trades.csv, join each trade with its feature vector from df_full.
    df_full must have a 'date' column and all selected_cols.

    Returns dict with keys:
      inject_rows  — list of (feature_vector, y_label) for ALL labeled trades
      X_hits       — feature matrix for oracle-correct trades (or None)
      X_misses     — feature matrix for oracle-wrong trades (or None)
      n_labeled    — count of labeled trades (True/False, not blank)
      n_misses     — count of oracle-wrong trades
    """
    import csv as _csv
    from pathlib import Path

    empty = dict(inject_rows=[], X_hits=None, X_misses=None, n_labeled=0, n_misses=0)

    rows = []
    for _p in LIVE_TRADES_PATHS:
        if not Path(_p).exists():
            continue
        try:
            with open(_p) as f:
                rows.extend(list(_csv.DictReader(f)))
        except Exception as e:
            print(f"  [feedback] Cannot read {_p}: {e}")
    if not rows:
        return empty

    labeled = [r for r in rows
               if str(r.get("oracle_correct", "")).lower() in ("true", "false")]
    if len(labeled) < MIN_LIVE_TRADES:
        print(f"  [feedback] {len(labeled)} labeled live trades — need {MIN_LIVE_TRADES} to activate feedback")
        return {**empty, "n_labeled": len(labeled)}

    df_full = df_full.copy()
    df_full["date"] = pd.to_datetime(df_full["date"])

    inject_rows = []
    X_hits      = []
    X_misses    = []

    for row in labeled:
        try:
            dt      = pd.Timestamp(row["date"])
            correct = str(row["oracle_correct"]).lower() == "true"
            signal  = str(row.get("signal", "")).upper()
        except Exception:
            continue

        match = df_full[df_full["date"] == dt]
        if match.empty:
            continue

        try:
            feats = match[selected_cols].fillna(0).values[0].astype(float)
        except KeyError:
            # Some selected cols may not exist in df_full (extended features added later)
            available = [c for c in selected_cols if c in match.columns]
            row_series = match.reindex(columns=selected_cols).fillna(0).iloc[0]
            feats = row_series.values.astype(float)

        # Convert signal + oracle_correct → binary direction label (1=CALL, 0=PUT)
        if signal in ("CALL", "PUT"):
            if correct:
                y_label = 1 if signal == "CALL" else 0
            else:
                y_label = 0 if signal == "CALL" else 1  # oracle wrong → opposite was right
            inject_rows.append((feats, int(y_label)))

        if correct:
            X_hits.append(feats)
        else:
            X_misses.append(feats)

    n_misses = len(X_misses)
    return dict(
        inject_rows = inject_rows,
        X_hits      = np.array(X_hits)   if X_hits   else None,
        X_misses    = np.array(X_misses) if X_misses else None,
        n_labeled   = len(labeled),
        n_misses    = n_misses,
    )


def _identify_miss_drivers(X_misses, X_hits, selected_cols, top_n=3):
    """
    Find features most different between miss days and hit days using effect size.
    Returns list of (col, direction, effect_size, miss_mean, hit_mean).
    """
    if X_misses is None or X_hits is None:
        return []
    if len(X_misses) < 2 or len(X_hits) < 1:
        return []

    drivers = []
    for i, col in enumerate(selected_cols):
        miss_vals = X_misses[:, i]
        hit_vals  = X_hits[:, i]
        miss_mean = float(miss_vals.mean())
        hit_mean  = float(hit_vals.mean())
        pooled_std = float(np.std(np.concatenate([miss_vals, hit_vals]))) + 1e-8
        effect     = abs(miss_mean - hit_mean) / pooled_std
        direction  = "HIGH" if miss_mean > hit_mean else "LOW"
        drivers.append((col, direction, round(effect, 2), round(miss_mean, 3), round(hit_mean, 3)))

    drivers.sort(key=lambda x: -x[2])
    return drivers[:top_n]


def _load_midday_reversals(df_full, selected_cols):
    """
    Load midday_checkpoints.csv and return reversal rows as (feature_vector, y_label) pairs.
    Reversal = the model predicted the wrong direction mid-session.
    y_label is the OPPOSITE of the signal (what we should have predicted instead).
    Returns empty list if file missing or no reversals found.
    """
    from pathlib import Path
    import csv as _csv

    if not Path(MIDDAY_CHECKPOINTS).exists():
        return []

    try:
        with open(MIDDAY_CHECKPOINTS) as f:
            rows = list(_csv.DictReader(f))
    except Exception as e:
        print(f"  [feedback] Cannot read midday_checkpoints.csv: {e}")
        return []

    reversal_rows = [r for r in rows
                     if str(r.get("reversal_detected", "")).lower() == "true"
                     and str(r.get("signal", "")).upper() in ("CALL", "PUT")]
    if not reversal_rows:
        return []

    df_full = df_full.copy()
    df_full["date"] = pd.to_datetime(df_full["date"])
    inject = []

    for row in reversal_rows:
        try:
            dt    = pd.to_datetime(row["date"])
            match = df_full[df_full["date"] == dt]
            if match.empty:
                continue
            feat = match[selected_cols].fillna(0).values[0]
            # Reversal = we were wrong → label is opposite of signal
            # Convention: CALL=1, PUT=0 (matches _load_live_outcomes)
            # CALL reversal: should have been PUT → y_label = 0
            # PUT  reversal: should have been CALL → y_label = 1
            signal  = row["signal"].upper()
            y_label = 0 if signal == "CALL" else 1
            inject.append((feat, y_label))
        except Exception:
            continue

    if inject:
        print(f"  [feedback] Loaded {len(inject)} midday reversals from checkpoints")
    return inject


def _compute_base_weights(df, y_all):
    """
    Per-row sample weights combining:
    1. Magnitude weighting: bigger NF moves = more informative labels
    2. VIX asymmetric loss: wrong on high-VIX days = IC SL hits faster → 3× penalty
    Applied before live-feedback injection so all training rows benefit.
    """
    weights = np.ones(len(y_all))

    if "nf_ret1" in df.columns:
        mag = pd.to_numeric(df["nf_ret1"], errors="coerce").fillna(0).abs().clip(upper=MAG_CLIP).values
        weights *= (1.0 + mag)

    if "vix_level" in df.columns:
        vix = pd.to_numeric(df["vix_level"], errors="coerce").fillna(15).values
        weights[vix > VIX_HIGH_THRESHOLD] *= VIX_HIGH_WEIGHT

    weights = weights / weights.mean()
    return weights


def _compute_live_feedback(X_all, y_all, df_full, selected_cols):
    """
    Build augmented (X, y, sample_weight) incorporating live trade feedback.

    Steps:
    1. Load live_trades.csv + join feature vectors
    2. Inject live rows into training set (weight=LIVE_INJECT_WEIGHT)
    3. Inject midday reversal rows (weight=MIDDAY_MISS_WEIGHT)
    4. Boost historical rows matching miss-day patterns (weight=MISS_BOOST)

    Returns (X_aug, y_aug, sample_weights, live_info) where live_info
    is a dict for Telegram reporting.
    """
    result = _load_live_outcomes(df_full, selected_cols)
    n_labeled   = result["n_labeled"]
    n_misses    = result["n_misses"]
    inject_rows = result["inject_rows"]
    X_hits      = result["X_hits"]
    X_misses    = result["X_misses"]

    miss_drivers = []
    sample_weights = np.ones(len(X_all))

    # ── Step 1: inject live rows ──────────────────────────────────────────────
    if inject_rows:
        X_inject = np.array([r[0] for r in inject_rows])
        y_inject = np.array([r[1] for r in inject_rows])
        X_aug = np.vstack([X_all, X_inject])
        y_aug = np.concatenate([y_all, y_inject])
        # Live rows get high weight; historical rows keep 1.0 for now
        sample_weights = np.concatenate([
            sample_weights,
            np.full(len(inject_rows), LIVE_INJECT_WEIGHT)
        ])
        print(f"  [feedback] Injected {len(inject_rows)} of {n_labeled} labeled trades (weight={LIVE_INJECT_WEIGHT}×)")
    else:
        X_aug = X_all.copy()
        y_aug = y_all.copy()
        if n_labeled >= MIN_LIVE_TRADES:
            print(f"  [feedback] {n_labeled} labeled trades found but 0 joined df_full (trade dates not yet in feature data — will activate tomorrow)")

    # ── Step 2: inject midday reversal rows ───────────────────────────────────
    midday_rows = _load_midday_reversals(df_full, selected_cols)
    if midday_rows:
        X_mid = np.array([r[0] for r in midday_rows])
        y_mid = np.array([r[1] for r in midday_rows])
        X_aug = np.vstack([X_aug, X_mid])
        y_aug = np.concatenate([y_aug, y_mid])
        sample_weights = np.concatenate([
            sample_weights,
            np.full(len(midday_rows), MIDDAY_MISS_WEIGHT),
        ])
        # Also fold into X_misses so miss-driver analysis sees them
        X_misses = (np.vstack([X_misses, X_mid])
                    if X_misses is not None and X_misses.size
                    else X_mid)
        n_misses += len(midday_rows)
        print(f"  [feedback] Injected {len(midday_rows)} midday reversals (weight={MIDDAY_MISS_WEIGHT}×)")

    # ── Step 3: boost historical rows matching miss patterns ──────────────────
    if n_misses >= MIN_MISSES and X_misses is not None:
        miss_drivers = _identify_miss_drivers(X_misses, X_hits, selected_cols)

        if miss_drivers:
            n_hist = len(X_all)
            for col, direction, effect, miss_mean, hit_mean in miss_drivers:
                if col not in selected_cols:
                    continue
                idx      = selected_cols.index(col)
                col_vals = X_aug[:n_hist, idx]
                col_mean = col_vals.mean()
                col_std  = col_vals.std() + 1e-8

                if direction == "HIGH":
                    threshold = col_mean + 0.5 * col_std
                    mask = col_vals >= threshold
                else:
                    threshold = col_mean - 0.5 * col_std
                    mask = col_vals <= threshold

                sample_weights[:n_hist][mask] *= MISS_BOOST

            # Re-normalise so total weight stays consistent with dataset size
            n_total = len(X_aug)
            sample_weights = sample_weights / sample_weights.mean()

            print(f"  [feedback] Miss pattern boost ({MISS_BOOST}×) on {len(miss_drivers)} drivers:")
            for col, direction, effect, mm, hm in miss_drivers:
                label = _FEATURE_LABELS.get(col, col)
                print(f"    {label}: {direction} on miss days (effect={effect:.2f})")

    live_info = dict(
        n_labeled    = n_labeled,
        n_misses     = n_misses,
        n_hits       = n_labeled - n_misses,
        n_injected   = len(inject_rows),
        miss_drivers = miss_drivers,
    )

    return X_aug, y_aug, sample_weights, live_info


def _optuna_objective(trial, model_type, X_tr, y_tr, X_val, y_val, sw_tr=None):
    # HPO uses SMALLER n_estimators for speed — final champion refit uses the
    # full model. Hyperparameter *ranking* doesn't need large forests/trees.
    if model_type == "rf":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 50, 200),
            "max_depth":       trial.suggest_int("max_depth", 4, 8),
            "min_samples_leaf":trial.suggest_int("min_samples_leaf", 5, 20),
            "max_features":    trial.suggest_categorical("max_features", ["sqrt", 0.5, 0.7]),
        }
    elif model_type == "xgb":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 50, 200),
            "max_depth":       trial.suggest_int("max_depth", 3, 7),
            "learning_rate":   trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
            "subsample":       trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree":trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "scale_pos_weight":sum(y_tr == 0) / max(sum(y_tr == 1), 1),
        }
    elif model_type == "lgb":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 50, 200),
            "max_depth":       trial.suggest_int("max_depth", 3, 7),
            "learning_rate":   trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
            "num_leaves":      trial.suggest_int("num_leaves", 15, 63),
            "min_child_samples":trial.suggest_int("min_child_samples", 10, 30),
        }
    elif model_type == "cat":
        params = {
            "iterations":      trial.suggest_int("iterations", 100, 300),  # HPO speed; champion refit uses 500
            "depth":           trial.suggest_int("depth", 4, 8),
            "learning_rate":   trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
            "l2_leaf_reg":     trial.suggest_float("l2_leaf_reg", 1.0, 10.0),
            "bagging_temperature": trial.suggest_float("bagging_temperature", 0.0, 1.0),
        }

    model = _build_model(model_type, params)
    model.fit(X_tr, y_tr, sample_weight=sw_tr)
    y_pred = model.predict(X_val)
    y_prob = model.predict_proba(X_val)[:, list(model.classes_).index(1)] \
             if 1 in model.classes_ else np.zeros(len(y_val))
    return _score(y_val, y_pred, y_prob)


# Final champion refit uses these full n_estimators (only ONE fit, not N_TRIALS)
_CHAMPION_N_ESTIMATORS = {"rf": 400, "xgb": 300, "lgb": 300, "cat": 500}


def run_competition(X, y, feature_cols, n_trials=N_TRIALS, sample_weight=None):
    """
    Run Optuna HPO for RF, XGBoost, LightGBM on the same temporal split.
    Returns list of result dicts sorted by composite score descending.
    sample_weight: optional array of per-row weights (same length as X/y).
    """
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    X_tr, y_tr, X_val, y_val = _temporal_split(X, y)

    # Split sample_weight the same way as _temporal_split
    if sample_weight is not None:
        n = len(X)
        split = n - len(y_val)
        sw_tr = sample_weight[:split]
    else:
        sw_tr = None

    print(f"\n[4/6] Model competition  (train={len(X_tr)}, holdout={len(X_val)}, "
          f"trials={n_trials} each — fast HPO, full estimators on champion refit)"
          + ("  +live-feedback" if sample_weight is not None else ""))

    results = []
    for mtype in ["rf", "xgb", "lgb", "cat"]:

        n_trials_for = n_trials
        print(f"\n  [{mtype.upper()}] Running {n_trials_for} Optuna trials...")

        try:
            # Quick import check before spinning up Optuna trials
            if mtype == "rf":
                _build_model(mtype, {"n_estimators": 10, "max_depth": 2})
            elif mtype == "cat":
                _build_model(mtype, {"iterations": 10, "depth": 2, "learning_rate": 0.1,
                                     "l2_leaf_reg": 1.0, "bagging_temperature": 0.5})
            else:
                _build_model(mtype, {"n_estimators": 10, "max_depth": 2, "learning_rate": 0.1,
                                     "subsample": 0.8, "colsample_bytree": 0.8,
                                     "scale_pos_weight": 1.0})
        except Exception as e:
            print(f"  [{mtype.upper()}] Not installed — skipping ({e})")
            continue

        # Standard Optuna HPO for RF / XGB / LGB / CAT
        pruner = optuna.pruners.MedianPruner(n_startup_trials=3, n_warmup_steps=0)
        study  = optuna.create_study(direction="maximize",
                                     sampler=optuna.samplers.TPESampler(seed=42),
                                     pruner=pruner)
        try:
            study.optimize(
                lambda trial: _optuna_objective(trial, mtype, X_tr, y_tr, X_val, y_val, sw_tr),
                n_trials=n_trials_for,
                show_progress_bar=False,
            )
        except Exception as e:
            print(f"  [{mtype.upper()}] Optimization error — skipping ({e})")
            continue

        best_params = study.best_params
        best_score  = study.best_value

        # Full eval on holdout with best params
        model = _build_model(mtype, best_params)
        model.fit(X_tr, y_tr)
        y_pred = model.predict(X_val)
        classes = list(model.classes_)
        y_prob  = model.predict_proba(X_val)[:, classes.index(1)] \
                  if 1 in classes else np.zeros(len(y_val))

        from sklearn.metrics import accuracy_score, recall_score
        acc      = accuracy_score(y_val, y_pred)
        rec_call = recall_score(y_val, y_pred, pos_label=1, zero_division=0)
        rec_put  = recall_score(y_val, y_pred, pos_label=0, zero_division=0)

        # Brier score (lower = better calibrated)
        brier = float(np.mean((y_prob - y_val) ** 2))

        print(f"  [{mtype.upper()}] Acc={acc:.1%}  "
              f"RecCALL={rec_call:.1%}  RecPUT={rec_put:.1%}  "
              f"Composite={best_score:.4f}")

        results.append({
            "model_type":  mtype,
            "params":      best_params,
            "score":       best_score,
            "accuracy":    acc,
            "recall_call": rec_call,
            "recall_put":  rec_put,
            "brier":       brier,
            "val_len":     len(X_val),
        })

    results.sort(key=lambda r: (-r["score"], r["brier"]))
    return results, X_tr, y_tr, X_val, y_val


# ─────────────────────────────────────────────────────────────────────────────
#  STACKING META-LEARNER — LogReg on top of 4-model validation probabilities
# ─────────────────────────────────────────────────────────────────────────────

STACK_META_PKL = f"{MODELS_DIR}/stack_meta.pkl"


def train_stacking_meta(results, X_tr, y_tr, X_val, y_val, sw_tr=None):
    """
    Train LogisticRegression meta-learner on OOF (out-of-fold) probabilities.
    Uses TimeSeriesSplit with 3 folds on X_tr to generate OOF predictions,
    then fits meta-learner. Saves as stack_meta.pkl alongside ensemble.

    Returns (meta_model, meta_val_score) where meta_val_score is composite on X_val.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import TimeSeriesSplit

    print("\n  [stack] Training stacking meta-learner (OOF LogReg)...")

    n_models = len(results)
    n_tr     = len(X_tr)
    tscv     = TimeSeriesSplit(n_splits=3)

    oof_probs = np.zeros((n_tr, n_models))

    for i, r in enumerate(results):
        for train_idx, val_idx in tscv.split(X_tr):
            m = _build_model(r["model_type"], r["params"])
            m.fit(X_tr[train_idx], y_tr[train_idx],
                  sample_weight=sw_tr[train_idx] if sw_tr is not None else None)
            classes  = list(m.classes_)
            call_idx = classes.index(1) if 1 in classes else 0
            oof_probs[val_idx, i] = m.predict_proba(X_tr[val_idx])[:, call_idx]

    meta = LogisticRegression(C=0.5, max_iter=1000, random_state=42)
    meta.fit(oof_probs, y_tr)
    print(f"  [stack] Meta weights: { {r['model_type']: round(float(w), 3) for r, w in zip(results, meta.coef_[0])} }")

    # Evaluate meta on val set
    val_probs = np.zeros((len(X_val), n_models))
    for i, r in enumerate(results):
        m = _build_model(r["model_type"], r["params"])
        m.fit(X_tr, y_tr, sample_weight=sw_tr)
        classes  = list(m.classes_)
        call_idx = classes.index(1) if 1 in classes else 0
        val_probs[:, i] = m.predict_proba(X_val)[:, call_idx]

    meta_preds = meta.predict(val_probs)
    meta_probs = meta.predict_proba(val_probs)[:, 1]
    meta_score = _score(y_val, meta_preds, meta_probs)
    print(f"  [stack] Meta composite on holdout: {meta_score:.4f}")

    import joblib
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(meta, STACK_META_PKL)
    print(f"  [stack] Saved: {STACK_META_PKL}")

    return meta, meta_score


# ─────────────────────────────────────────────────────────────────────────────
#  PROBABILITY CALIBRATION — isotonic regression on champion probabilities
# ─────────────────────────────────────────────────────────────────────────────

CALIB_PKL = f"{MODELS_DIR}/champion_calibrated.pkl"


def calibrate_champion(champion_model, X_val, y_val):
    """
    Wrap champion in isotonic calibration using the holdout set.
    Calibrated probabilities → ML_CONF_THRESHOLD filter becomes meaningful.
    Saved as champion_calibrated.pkl.

    sklearn 1.6+ deprecated cv='prefit'. Falls back gracefully via FrozenEstimator
    or cv=5 (re-fit on val).
    """
    from sklearn.calibration import CalibratedClassifierCV
    import joblib

    # Use sigmoid (Platt) — more stable than isotonic on small holdout sets
    # (252 rows × 5-fold = ~50 rows/fold; isotonic creates cliffs in sparse regions
    # that can amplify probabilities to 1.0 at certain raw values, defeating the
    # purpose of calibration.)
    method = "sigmoid"

    # Try modern API: FrozenEstimator (sklearn 1.6+)
    try:
        from sklearn.frozen import FrozenEstimator
        frozen = FrozenEstimator(champion_model)
        calib  = CalibratedClassifierCV(frozen, cv=5, method=method)
        calib.fit(X_val, y_val)
        joblib.dump(calib, CALIB_PKL)
        print(f"  [calib] Calibrated champion saved (FrozenEstimator/{method}): {CALIB_PKL}")
        return calib
    except ImportError:
        pass

    # Try legacy prefit API (sklearn < 1.6)
    try:
        calib = CalibratedClassifierCV(champion_model, cv="prefit", method=method)
        calib.fit(X_val, y_val)
        joblib.dump(calib, CALIB_PKL)
        print(f"  [calib] Calibrated champion saved (prefit/{method}): {CALIB_PKL}")
        return calib
    except Exception as e:
        print(f"  [calib] Calibration failed ({e}) — skipping")
        return champion_model


# ─────────────────────────────────────────────────────────────────────────────
#  BAYESIAN ENSEMBLE WEIGHT OPTIMIZATION (scipy L-BFGS-B, < 5 sec)
# ─────────────────────────────────────────────────────────────────────────────

def optimize_ensemble_weights(results, X_tr, y_tr, X_val, y_val, sw_tr=None):
    """
    Find optimal per-model weights via L-BFGS-B (much faster than Optuna for weights).
    Complements equal-vote ensemble: down-weights models that hurt holdout composite.
    Returns (weights_dict, optimized_score).
    """
    from scipy.optimize import minimize

    model_probs = []
    for r in results:
        m = _build_model(r["model_type"], r["params"])
        m.fit(X_tr, y_tr, sample_weight=sw_tr)
        classes  = list(m.classes_)
        call_idx = classes.index(1) if 1 in classes else 0
        model_probs.append(m.predict_proba(X_val)[:, call_idx])
    model_probs = np.array(model_probs)  # (n_models, n_val)

    def neg_score(w):
        w = np.abs(w) / (np.abs(w).sum() + 1e-8)
        ensemble_p = (model_probs * w[:, None]).sum(axis=0)
        y_pred = (ensemble_p >= 0.5).astype(int)
        return -_score(y_val, y_pred, ensemble_p)

    n = len(results)
    w0  = np.ones(n) / n
    res = minimize(neg_score, w0, method="L-BFGS-B",
                   bounds=[(0.0, 1.0)] * n, options={"maxiter": 200})
    w_opt  = np.abs(res.x) / (np.abs(res.x).sum() + 1e-8)
    opt_score = -res.fun
    w_dict = {r["model_type"]: round(float(w), 3) for r, w in zip(results, w_opt)}
    print(f"  [weights] Optimized ensemble weights: {w_dict}  composite={opt_score:.4f}")

    # Save weights alongside ensemble meta
    weights_path = f"{MODELS_DIR}/ensemble_weights.json"
    with open(weights_path, "w") as f:
        json.dump(w_dict, f, indent=2)
    print(f"  [weights] Saved: {weights_path}")

    return w_dict, opt_score


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME-CONDITIONAL MODELS — train one champion per VIX regime
# ─────────────────────────────────────────────────────────────────────────────

REGIME_DIR = f"{MODELS_DIR}/regime"
VIX_REGIME_BINS = [0, 14, 18, 999]   # low / med / high
VIX_REGIME_NAMES = ["low", "med", "high"]


def train_regime_models(X_all, y_all, df_full, selected_cols, sample_weight=None):
    """
    Train separate champion per VIX regime (low/med/high).
    Each model specializes in its volatility regime.
    Routed at predict time based on today's VIX.

    Uses simple shallow tree to avoid memorization on small per-regime datasets
    (~500 rows each). Reports HOLDOUT accuracy via temporal 80/20 split, not
    training accuracy (training acc would be near 100% on overfit models).

    Saves models/regime/{low,med,high}.pkl + meta.json.
    Skips a regime if it has < 200 rows (too few to train).

    Returns dict {regime_name: meta} for the Telegram report.
    """
    import joblib
    import json as _json
    from sklearn.ensemble import RandomForestClassifier

    os.makedirs(REGIME_DIR, exist_ok=True)
    df = df_full.copy()
    if "vix_level" not in df.columns:
        print("  [regime] vix_level missing — skipping regime-conditional training")
        return {}

    vix = pd.to_numeric(df["vix_level"], errors="coerce").fillna(15).values[:len(y_all)]
    regime_idx = np.digitize(vix, VIX_REGIME_BINS) - 1   # 0/1/2

    out_meta = {}
    for i, name in enumerate(VIX_REGIME_NAMES):
        mask = (regime_idx == i)
        n_rows = int(mask.sum())
        if n_rows < 200:
            print(f"  [regime] {name}: only {n_rows} rows — skipping (need 200)")
            continue
        if len(np.unique(y_all[mask])) < 2:
            print(f"  [regime] {name}: single-class labels — skipping")
            continue

        # Shallow regularized model — small per-regime data needs strong priors
        # to avoid memorization. 50 trees + depth 3 + leaf 20 prevents overfit.
        mdl = RandomForestClassifier(
            n_estimators=50, max_depth=3, min_samples_leaf=20,
            max_features="sqrt", class_weight="balanced",
            random_state=42, n_jobs=-1,
        )

        # Temporal 80/20 split per regime (LAST 20% is holdout)
        idxs   = np.where(mask)[0]
        split  = int(len(idxs) * 0.8)
        train_idx = idxs[:split]
        val_idx   = idxs[split:]

        sw_tr = sample_weight[train_idx] if sample_weight is not None else None
        try:
            mdl.fit(X_all[train_idx], y_all[train_idx], sample_weight=sw_tr)
        except TypeError:
            mdl.fit(X_all[train_idx], y_all[train_idx])

        # Holdout accuracy (honest)
        if len(val_idx) >= 10 and len(np.unique(y_all[val_idx])) >= 2:
            holdout_acc = float((mdl.predict(X_all[val_idx]) == y_all[val_idx]).mean())
        else:
            holdout_acc = float("nan")

        # Re-fit on FULL regime data for production model (now that we know it
        # generalizes — holdout was just for honest reporting)
        sw_full = sample_weight[mask] if sample_weight is not None else None
        try:
            mdl.fit(X_all[mask], y_all[mask], sample_weight=sw_full)
        except TypeError:
            mdl.fit(X_all[mask], y_all[mask])

        joblib.dump(mdl, f"{REGIME_DIR}/{name}.pkl")
        out_meta[name] = {
            "n_rows":      n_rows,
            "holdout_acc": round(holdout_acc, 4) if holdout_acc == holdout_acc else None,  # NaN guard
            "vix_range":   f"{VIX_REGIME_BINS[i]}-{VIX_REGIME_BINS[i+1]}",
        }
        print(f"  [regime] {name}: {n_rows} rows, holdout_acc={holdout_acc:.1%} → {REGIME_DIR}/{name}.pkl")

    if out_meta:
        with open(f"{REGIME_DIR}/meta.json", "w") as f:
            _json.dump({
                "regimes":      out_meta,
                "feature_cols": selected_cols,
                "trained_at":   datetime.now(_IST).isoformat(),
                "bins":         VIX_REGIME_BINS,
            }, f, indent=2)
    return out_meta


# ─────────────────────────────────────────────────────────────────────────────
#  CONCEPT DRIFT DETECTION — River ADWIN on holdout error stream
# ─────────────────────────────────────────────────────────────────────────────

def check_concept_drift(val_preds, y_val):
    """
    Scan holdout predictions for concept drift using ADWIN.
    Drift detected = model accuracy decaying mid-holdout (regime change).
    Logs warning to Telegram if drift found.
    """
    if not _RIVER_OK:
        return None

    detector     = _ADWIN()
    drift_points = []
    for i, (pred, true) in enumerate(zip(val_preds, y_val)):
        error = int(pred != true)
        in_drift = detector.update(error)
        if in_drift:
            drift_points.append(i)

    if drift_points:
        print(f"  [drift] ADWIN detected {len(drift_points)} drift point(s) "
              f"in holdout (indices: {drift_points[:5]}{'...' if len(drift_points)>5 else ''})")
    else:
        print("  [drift] No concept drift detected in holdout window")

    return drift_points


# ─────────────────────────────────────────────────────────────────────────────
#  IC P&L PREDICTOR — shadow model, separate from primary direction model
#  Used as future SKIP FILTER (after 30 trades validate skip accuracy).
# ─────────────────────────────────────────────────────────────────────────────

IC_PNL_PREDICTOR_PKL  = f"{MODELS_DIR}/ic_pnl_predictor.pkl"
IC_PNL_PREDICTOR_META = f"{MODELS_DIR}/ic_pnl_predictor_meta.json"


def train_ic_pnl_predictor(df_full, selected_cols, sample_weight=None):
    """
    Train SECONDARY model with IC P&L labels. Heavy regularization to prevent
    memorization (depth 3, leaf 50, n_est 100).

    Output: P(strategy_will_profit) per day. Used as a future SKIP FILTER.

    Saves models/ic_pnl_predictor.pkl + meta.json with train + holdout accuracy.
    Returns dict {n_train, holdout_acc, p_profit_today} for Telegram report.
    """
    import joblib
    import json as _json
    from sklearn.ensemble import RandomForestClassifier
    from ml_engine import compute_labels_ic_pnl

    print("\n  [skip-filter] Training IC P&L predictor (shadow, regularized)...")

    df = df_full.copy()
    labels_df = compute_labels_ic_pnl(df)
    df = df.merge(labels_df[["date", "label"]], on="date", how="inner")
    df = df.dropna(subset=selected_cols + ["label"]).reset_index(drop=True)

    if len(df) < HOLDOUT_DAYS + 100:
        print(f"  [skip-filter] Only {len(df)} rows after merge — skipping training")
        return {}

    X = df[selected_cols].values.astype(float)
    y = (df["label"] == "CALL").astype(int).values   # 1 = strategy wins, 0 = loses

    split    = len(X) - HOLDOUT_DAYS
    X_tr, y_tr = X[:split], y[:split]
    X_val, y_val = X[split:], y[split:]
    sw_tr = sample_weight[:split] if sample_weight is not None and len(sample_weight) >= split else None

    # Strong regularization to prevent memorization
    mdl = RandomForestClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=50,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    try:
        mdl.fit(X_tr, y_tr, sample_weight=sw_tr)
    except Exception:
        mdl.fit(X_tr, y_tr)

    train_acc   = float((mdl.predict(X_tr) == y_tr).mean())
    holdout_acc = float((mdl.predict(X_val) == y_val).mean())

    # Refit on full data for production
    sw_full = sample_weight[:len(X)] if sample_weight is not None else None
    try:
        mdl.fit(X, y, sample_weight=sw_full)
    except Exception:
        mdl.fit(X, y)

    # Predict probability for today (last row)
    today_row    = df[selected_cols].iloc[[-1]].values.astype(float)
    classes      = list(mdl.classes_)
    profit_idx   = classes.index(1) if 1 in classes else 0
    p_profit_today = float(mdl.predict_proba(today_row)[0, profit_idx])

    joblib.dump(mdl, IC_PNL_PREDICTOR_PKL)
    meta = {
        "model_type":     "rf",
        "params":         {"n_estimators": 100, "max_depth": 3, "min_samples_leaf": 50},
        "train_acc":      round(train_acc, 4),
        "holdout_acc":    round(holdout_acc, 4),
        "n_train":        int(split),
        "n_val":          int(len(X) - split),
        "p_profit_today": round(p_profit_today, 4),
        "feature_cols":   selected_cols,
        "trained_at":     datetime.now(_IST).isoformat(),
    }
    with open(IC_PNL_PREDICTOR_META, "w") as f:
        _json.dump(meta, f, indent=2)

    print(f"  [skip-filter] Train acc: {train_acc:.1%}  Holdout: {holdout_acc:.1%}")
    print(f"  [skip-filter] P(strategy wins today): {p_profit_today:.3f}")
    print(f"  [skip-filter] Saved: {IC_PNL_PREDICTOR_PKL}")
    return {
        "train_acc":      round(train_acc * 100, 1),
        "holdout_acc":    round(holdout_acc * 100, 1),
        "p_profit_today": round(p_profit_today * 100, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5+6 — CHAMPION SELECTION + FINAL TRAINING
# ─────────────────────────────────────────────────────────────────────────────

def train_champion(champion_meta, X_all, y_all, sample_weight=None):
    """Retrain champion model with best params on ALL data.
    Overrides n_estimators (or iterations for CatBoost) to the full-strength
    value — HPO used smaller forests for speed; deployed champion uses full model.
    """
    params = dict(champion_meta["params"])
    mtype  = champion_meta["model_type"]
    if mtype == "cat":
        full_n = _CHAMPION_N_ESTIMATORS.get(mtype, 500)
        params.pop("n_estimators", None)
        params["iterations"] = full_n
    else:
        full_n = _CHAMPION_N_ESTIMATORS.get(mtype, params.get("n_estimators", 300))
        params["n_estimators"] = full_n
    model = _build_model(mtype, params)
    model.fit(X_all, y_all, sample_weight=sample_weight)
    return model


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 7 — SAVE CHAMPION
# ─────────────────────────────────────────────────────────────────────────────

CHAMPION_PKL  = f"{MODELS_DIR}/champion.pkl"
CHAMPION_META = f"{MODELS_DIR}/champion_meta.json"


def save_champion(model, meta):
    import joblib
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, CHAMPION_PKL)
    with open(CHAMPION_META, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"\n  Saved: {CHAMPION_PKL}")
    print(f"  Saved: {CHAMPION_META}")


ENSEMBLE_DIR  = f"{MODELS_DIR}/ensemble"
ENSEMBLE_META = f"{MODELS_DIR}/ensemble_meta.json"


def save_ensemble(models_dict, metas_dict):
    """Save one trained model per type (rf/xgb/lgb) for ensemble prediction."""
    import joblib
    os.makedirs(ENSEMBLE_DIR, exist_ok=True)
    for mtype, model in models_dict.items():
        path = f"{ENSEMBLE_DIR}/{mtype}.pkl"
        joblib.dump(model, path)
        print(f"  Saved ensemble[{mtype}]: {path}")
    with open(ENSEMBLE_META, "w") as f:
        json.dump(metas_dict, f, indent=2)
    print(f"  Saved: {ENSEMBLE_META}")


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 8 — TELEGRAM REPORT (plain English)
# ─────────────────────────────────────────────────────────────────────────────

# Human-friendly names for the top features
_FEATURE_LABELS = {
    "trend5":       "5-day trend direction",
    "vix_hv_ratio": "Fear index vs historical swings",
    "ema20_pct":    "How far Nifty is from its average",
    "dte":          "Days to expiry",
    "nf_ret5":      "5-day Nifty momentum",
    "rsi14":        "Momentum strength (RSI)",
    "hv20":         "Recent volatility (20-day)",
    "vix_level":    "Absolute fear index level",
    "vix_pct_chg":  "Fear index daily change",
    "s_ema20":      "Above/below 20-day average signal",
    "s_trend5":     "5-day trend signal",
    "s_vix":        "VIX direction signal",
    "s_nf_gap":     "Nifty overnight gap direction signal",
    "sp500_chg":    "US market daily move",
    "nikkei_chg":   "Japan market daily move",
    "spf_gap":      "US futures overnight gap",
    "nf_ret1":      "Yesterday Nifty return",
    "nf_ret20":     "1-month Nifty return",
    "dow":          "Day of week",
    "gold_ret":     "Gold daily move",
    "crude_ret":    "Crude oil daily move",
    "usdinr_ret":   "Rupee daily move vs USD",
    "dxy_ret":      "US Dollar index move",
    "us10y_chg":    "US 10-year interest rate change",
    "pcr":          "Put-Call Ratio (options sentiment)",
    "fii_net_fut":  "Foreign investor futures activity",
    "nf_vol_ratio": "Nifty trading volume vs normal",
    "atr14_pct":    "Expected intraday swing range",
    "nf_gap":       "Nifty open gap vs yesterday",
    "vix_dir":      "VIX direction",
    "ema20_pct":    "Distance from 20-day moving average",
}

_MODEL_NAMES = {"rf": "Random Forest", "xgb": "XGBoost", "lgb": "LightGBM", "cat": "CatBoost"}


def send_telegram_report(results, champion_meta, today_signal, today_conf,
                         feature_importances, n_features_total, live_info=None,
                         lever_info=None):
    """Send a plain-English Telegram report after nightly training."""
    import notify

    now_ist   = datetime.now(_IST)
    date_str  = now_ist.strftime("%a %d %b, %I:%M %p")

    champ_type  = champion_meta["model_type"]
    champ_name  = _MODEL_NAMES.get(champ_type, champ_type.upper())
    acc_pct     = round(champion_meta["accuracy"] * 100)
    holdout_days= champion_meta.get("val_len", HOLDOUT_DAYS)

    # Runner-up info
    others = [r for r in results if r["model_type"] != champ_type]
    runner_lines = []
    for r in others:
        runner_lines.append(
            f"  {_MODEL_NAMES.get(r['model_type'], r['model_type'])}: "
            f"{round(r['accuracy']*100)}% right"
        )

    # Top 3 features in human language
    top_feats = feature_importances[:3]
    feat_lines = "\n".join(
        f"  • {_FEATURE_LABELS.get(c, c)}"
        for c, _ in top_feats
    )

    n_used = champion_meta.get("n_features", n_features_total)

    direction  = today_signal if today_signal in ("CALL", "PUT") else "unclear"
    conf_pct   = round(today_conf * 100)
    others_str = ", ".join(
        "{} {}%".format(_MODEL_NAMES.get(r["model_type"], r["model_type"]),
                        round(r["accuracy"] * 100))
        for r in others
    )

    # ── Live trade scorecard ──────────────────────────────────────────────────
    live_section = ""
    try:
        import csv as _csv
        journal_path = f"{DATA_DIR}/live_ic_trades.csv"
        if os.path.exists(journal_path):
            with open(journal_path) as f:
                rows = list(_csv.DictReader(f))
            total  = len(rows)
            wins   = sum(1 for r in rows if str(r.get("oracle_correct", "")).lower() == "true")
            losses = sum(1 for r in rows if str(r.get("oracle_correct", "")).lower() == "false")
            if total >= 3:
                live_wr  = round(wins / total * 100) if total > 0 else 0
                slips    = [float(r["entry_slippage_pct"]) for r in rows
                            if r.get("entry_slippage_pct") not in ("", None)]
                avg_slip = f"{sum(slips)/len(slips):+.1f}%" if slips else "n/a"
                pnls     = [float(r["actual_pnl"]) for r in rows
                            if r.get("actual_pnl") not in ("", None)]
                avg_pnl  = f"Rs.{sum(pnls)/len(pnls):,.0f}" if pnls else "n/a"
                live_section = (
                    f"\n\nLive oracle ({total} trades): {live_wr}%  "
                    f"({wins}W / {losses}L)\n"
                    f"  Avg slippage: {avg_slip}   Avg P&L: {avg_pnl}"
                )
    except Exception:
        pass

    # ── Miss driver analysis ──────────────────────────────────────────────────
    miss_section = ""
    if live_info and live_info.get("n_misses", 0) >= MIN_MISSES:
        drivers = live_info.get("miss_drivers", [])
        n_miss  = live_info["n_misses"]
        n_hit   = live_info["n_hits"]
        if drivers:
            driver_lines = "\n".join(
                f"  • {_FEATURE_LABELS.get(d[0], d[0])}: was {d[1]} (effect {d[2]:.1f}x)"
                for d in drivers
            )
            miss_section = (
                f"\n\nWhat went wrong on {n_miss} missed calls:\n"
                f"{driver_lines}\n"
                f"  Brain now {MISS_BOOST:.0f}x more cautious about these patterns."
            )
        elif n_miss > 0:
            miss_section = f"\n\nMisses: {n_miss} — not enough data yet to identify pattern."

    # ── Lever pipeline status (stacking / calibration / weights / drift) ─────
    # Note: ASCII-only — Telegram had encoding issues with unicode × (multiplication sign)
    # truncating the message at first occurrence.
    lever_section = ""
    if lever_info:
        lines = []
        if lever_info.get("high_vix_n") is not None:
            lines.append(f"  - High-fear days penalised: {lever_info['high_vix_n']} rows at {VIX_HIGH_WEIGHT}x weight")
        if lever_info.get("stack_score") is not None:
            lines.append(f"  - Meta-learner score: {lever_info['stack_score']:.3f}")
        if lever_info.get("calib_ok"):
            lines.append(f"  - Confidence calibrated (honest probabilities)")
        if lever_info.get("ensemble_w"):
            w = lever_info["ensemble_w"]
            top = sorted(w.items(), key=lambda kv: -kv[1])[:3]
            top_str = ", ".join(f"{k.upper()} {v:.2f}" for k, v in top)
            lines.append(f"  - Optimal vote weights: {top_str}")
            if lever_info.get("weight_score") is not None:
                lines.append(f"  - Weighted ensemble score: {lever_info['weight_score']:.3f}")
        if lever_info.get("drift_count") is not None:
            dc = lever_info["drift_count"]
            if dc == 0:
                lines.append(f"  - Drift check: stable (no regime change in holdout)")
            else:
                lines.append(f"  - Drift check: {dc} regime-shift point(s) flagged")
        if lever_info.get("regime_models"):
            rm = lever_info["regime_models"]
            rm_str = ", ".join(f"{k} ({v['n_rows']}r)" for k, v in rm.items())
            lines.append(f"  - Regime-conditional models: {rm_str}")
        if lever_info.get("ic_pnl_predictor"):
            ic = lever_info["ic_pnl_predictor"]
            lines.append(f"  - IC P&L skip-filter (shadow): holdout {ic.get('holdout_acc')}%, today P(win)={ic.get('p_profit_today')}%")
        if lines:
            lever_section = "\n\nUpgrade pipeline:\n" + "\n".join(lines)

    msg = (
        f"Brain trained  |  {date_str}\n\n"
        f"Today's winner: {champ_name}\n"
        f"Right calls in last {holdout_days} days: {acc_pct} out of 100\n"
        f"\n"
        f"What it watches most:\n"
        f"{feat_lines}\n"
        f"\n"
        f"Tomorrow's lean: {direction}  (confidence: {conf_pct}%)\n"
        f"\n"
        f"Other engines: {others_str}\n"
        f"Signals used: {n_used} indicators"
        f"{lever_section}"
        f"{live_section}"
        f"{miss_section}"
    )

    notify.send(msg)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    start = datetime.now()
    print("=" * 60)
    print("  Model Evolver — Nightly Autonomous Training")
    print(f"  {datetime.now(_IST).strftime('%a %d %b %Y  %I:%M %p IST')}")
    print("=" * 60)

    # ── 1. Data refresh ───────────────────────────────────────────────────────
    if not SKIP_DATA_REFRESH:
        refresh_data()
    else:
        print("\n[1/6] Skipping data refresh (--no-data)")

    # ── 2. Feature engineering ────────────────────────────────────────────────
    print("\n[2/6] Engineering features...")
    from ml_engine import compute_labels, get_label_fn

    try:
        df = load_extended_data()
        df = compute_extended_features(df)
    except Exception as e:
        print(f"  ERROR in feature engineering: {e}")
        import notify
        notify.send(f"Model Evolver failed — feature engineering error: {e}")
        sys.exit(1)

    # Keep only Mon–Fri; preserve full weekday df for today's prediction row
    trading_full = df[df["date"].dt.weekday.isin([0, 1, 2, 3, 4])].copy().reset_index(drop=True)

    # Labels (binary: 1=CALL, 0=PUT)
    labels_df = get_label_fn()(trading_full)   # respects LABEL_MODE env var
    y_all = np.array([1 if l == "CALL" else 0 for l in labels_df["label"].values])

    # Filter to rows that have labels (today's row has no next-day label yet)
    trading = trading_full.iloc[:len(y_all)].copy()
    print(f"  Total trading days with labels: {len(trading)}")

    # ── 3. Feature selection ──────────────────────────────────────────────────
    print("\n[3/6] Feature selection...")
    # Build X from extended features, fill NaN with 0
    all_cols_present = [c for c in EXTENDED_FEATURE_COLS if c in trading.columns]
    X_full = trading[all_cols_present].fillna(0).values.astype(float)

    selected_cols, feature_importances = select_features(X_full, y_all, all_cols_present)
    X_all = trading[selected_cols].fillna(0).values.astype(float)

    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(f"{MODELS_DIR}/feature_set.json", "w") as f:
        json.dump({"feature_cols": selected_cols,
                   "importances":  feature_importances,
                   "updated_at":   datetime.now(_IST).isoformat()}, f, indent=2)

    # ── 3b. Base sample weights (magnitude + VIX asymmetric) ─────────────────
    print("\n[3b/6] Base sample weights (magnitude + VIX asymmetric)...")
    base_weights = _compute_base_weights(trading, y_all)
    high_vix_n = int((pd.to_numeric(trading.get("vix_level", pd.Series([])), errors="coerce").fillna(15) > VIX_HIGH_THRESHOLD).sum())
    print(f"  Magnitude + VIX weighting active - {high_vix_n} high-VIX rows at {VIX_HIGH_WEIGHT}x penalty")

    # ── 3c. Live trade feedback — inject real outcomes + boost miss patterns ──
    print("\n[3c/6] Live trade feedback...")
    X_aug, y_aug, live_weights, live_info = _compute_live_feedback(
        X_all, y_all, df, selected_cols
    )
    # Merge: base weights for historical rows × live weights (already includes inject/boost)
    sample_weights = live_weights.copy()
    n_hist = len(y_all)
    sample_weights[:n_hist] *= base_weights
    sample_weights = sample_weights / sample_weights.mean()  # re-normalize

    has_feedback = live_info["n_injected"] > 0 or live_info["n_misses"] >= MIN_MISSES

    # ── 4. Model competition ──────────────────────────────────────────────────
    results, X_tr, y_tr, X_val, y_val = run_competition(
        X_aug, y_aug, selected_cols,
        sample_weight=sample_weights,  # always apply base weights (magnitude + VIX)
    )

    print("\n[4/6] Competition results:")
    for i, r in enumerate(results):
        medal = ["Champion", "2nd", "3rd"][i] if i < 3 else f"{i+1}th"
        print(f"  {medal}: {_MODEL_NAMES.get(r['model_type'], r['model_type'])}"
              f"  Acc={r['accuracy']:.1%}  Score={r['score']:.4f}")

    champion = results[0]

    # ── 5+6. Final training: champion + full ensemble (one per model type) ────
    # Champion: overall best — used as single-model fallback (backwards compatible).
    # Ensemble: best RF + best XGB + best LGB trained individually on full data.
    # At prediction time, ensemble majority vote is used instead of single champion.
    print(f"\n[5/6] Final training: champion + ensemble (RF + XGB + LGB)...")
    sw = sample_weights if has_feedback else None

    final_model = train_champion(champion, X_aug, y_aug, sample_weight=sw)

    # Train the best model of each type for ensemble
    # results may have fewer than 3 types if a type failed — build a dict keyed by type
    best_by_type = {}
    for r in results:
        t = r["model_type"]
        if t not in best_by_type:
            best_by_type[t] = r   # results are sorted best-first per overall score

    ensemble_models = {}
    ensemble_metas  = {}
    trained_at_str  = datetime.now(_IST).isoformat()
    for mtype, r in best_by_type.items():
        params = dict(r["params"])
        if mtype == "cat":
            full_n = _CHAMPION_N_ESTIMATORS.get(mtype, 500)
            params.pop("n_estimators", None)
            params["iterations"] = full_n
        else:
            full_n = _CHAMPION_N_ESTIMATORS.get(mtype, params.get("n_estimators", 300))
            params["n_estimators"] = full_n
        m = _build_model(mtype, params)
        m.fit(X_aug, y_aug, sample_weight=sw)
        ensemble_models[mtype] = m
        ensemble_metas[mtype]  = {
            "model_type":   mtype,
            "params":       params,
            "accuracy":     r["accuracy"],
            "recall_call":  r["recall_call"],
            "recall_put":   r["recall_put"],
            "score":        r["score"],
            "feature_cols": selected_cols,
            "train_rows":   len(X_all),
            "trained_at":   trained_at_str,
        }
        print(f"  Trained ensemble [{mtype.upper()}]: acc={r['accuracy']:.1%}  score={r['score']:.4f}")

    # ── 7. Save champion + ensemble ───────────────────────────────────────────
    meta = {
        "model_type":  champion["model_type"],
        "params":      champion["params"],
        "accuracy":    champion["accuracy"],
        "recall_call": champion["recall_call"],
        "recall_put":  champion["recall_put"],
        "brier":       champion["brier"],
        "score":       champion["score"],
        "val_len":     champion["val_len"],
        "feature_cols":selected_cols,
        "n_features":  len(selected_cols),
        "train_rows":  len(X_all),
        "trained_at":  trained_at_str,
    }
    save_champion(final_model, meta)
    save_ensemble(ensemble_models, ensemble_metas)

    # ── 7b-e. Lever pipeline (stacking, calibration, weight optim, drift) ─────
    lever_info = {
        "high_vix_n":        high_vix_n,
        "stack_score":       None,
        "calib_ok":          False,
        "ensemble_w":        None,
        "weight_score":      None,
        "drift_count":       None,
        "regime_models":     None,
        "ic_pnl_predictor":  None,
    }
    sw_tr = sample_weights[:len(y_tr)]

    try:
        _, stack_score = train_stacking_meta(results, X_tr, y_tr, X_val, y_val, sw_tr=sw_tr)
        lever_info["stack_score"] = stack_score
    except Exception as e:
        print(f"  [stack] Skipped: {e}")

    try:
        calib = calibrate_champion(final_model, X_val, y_val)
        lever_info["calib_ok"] = calib is not final_model  # True if a real CalibratedCV returned
    except Exception as e:
        print(f"  [calib] Skipped: {e}")

    try:
        w_dict, w_score = optimize_ensemble_weights(results, X_tr, y_tr, X_val, y_val, sw_tr=sw_tr)
        lever_info["ensemble_w"]   = w_dict
        lever_info["weight_score"] = w_score
    except Exception as e:
        print(f"  [weights] Skipped: {e}")

    try:
        val_preds_all = np.array([
            m.predict(X_val) for m in ensemble_models.values()
        ])
        ensemble_val_pred = (val_preds_all.sum(axis=0) >= len(ensemble_models) / 2).astype(int)
        drift_pts = check_concept_drift(ensemble_val_pred, y_val)
        lever_info["drift_count"] = len(drift_pts) if drift_pts is not None else None
    except Exception as e:
        print(f"  [drift] Skipped: {e}")

    try:
        regime_meta = train_regime_models(X_aug, y_aug, trading, selected_cols, sample_weight=sw)
        lever_info["regime_models"] = regime_meta
    except Exception as e:
        print(f"  [regime] Skipped: {e}")

    try:
        ic_pnl_meta = train_ic_pnl_predictor(trading, selected_cols, sample_weight=base_weights)
        lever_info["ic_pnl_predictor"] = ic_pnl_meta
    except Exception as e:
        print(f"  [skip-filter] Skipped: {e}")

    # ── 8. Predict tomorrow using ensemble vote ───────────────────────────────
    # Always predict from the most recent row in trading_full. If today's data
    # isn't available yet (late fetch, holiday, weekend), falls back to the
    # most recent trading day automatically.
    today_ts = pd.Timestamp(datetime.now(_IST).date())
    exact_match = trading_full[trading_full["date"] == today_ts]
    if not exact_match.empty:
        today_rows = exact_match
        print(f"  Prediction basis: today ({today_ts.date()})")
    elif not trading_full.empty:
        today_rows = trading_full.iloc[[-1]]
        last_date = trading_full.iloc[-1]["date"]
        print(f"  Prediction basis: most recent available ({pd.Timestamp(last_date).date()}) — today's row not in data")
    else:
        today_rows = trading_full.iloc[0:0]  # empty

    if not today_rows.empty:
        votes  = []
        confs  = []
        for mtype, m in ensemble_models.items():
            X_t    = today_rows[selected_cols].fillna(0).values.astype(float)
            proba  = m.predict_proba(X_t)[0]
            clses  = list(m.classes_)
            pc     = proba[clses.index(1)] if 1 in clses else 0.5
            pp     = proba[clses.index(0)] if 0 in clses else 0.5
            votes.append("CALL" if pc >= pp else "PUT")
            confs.append(max(pc, pp))
        call_v = votes.count("CALL")
        put_v  = votes.count("PUT")
        today_signal = "CALL" if call_v >= put_v else "PUT"
        agreed_confs = [c for v, c in zip(votes, confs) if v == today_signal]
        today_conf   = sum(agreed_confs) / len(agreed_confs) if agreed_confs else 0.5
        n_models = len(votes)
        print(f"  Ensemble tomorrow: {call_v}/{n_models} CALL  {put_v}/{n_models} PUT → {today_signal}  "
              f"(avg conf {today_conf:.1%})")
    else:
        # Fallback to single champion for tomorrow preview
        X_today = None
        for mtype, m in ensemble_models.items():
            X_today = m  # just get any model
            break
        today_signal = "NONE"
        today_conf   = 0.5

    elapsed = (datetime.now() - start).seconds
    print(f"\n  Elapsed: {elapsed // 60}m {elapsed % 60}s")

    # ── 8. Telegram report ────────────────────────────────────────────────────
    print("\n[6/6] Sending Telegram report...")
    try:
        send_telegram_report(
            results             = results,
            champion_meta       = meta,
            today_signal        = today_signal,
            today_conf          = today_conf,
            feature_importances = feature_importances,
            n_features_total    = len(all_cols_present),
            live_info           = live_info,
            lever_info          = lever_info,
        )
    except Exception as e:
        print(f"  Telegram report failed: {e}")

    # ── Wiki raw dump ─────────────────────────────────────────────────────────
    try:
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _ist = _tz((_td(hours=5, minutes=30)))
        _wiki_raw = _Path(__file__).parent / "docs" / "wiki" / "raw"
        _wiki_raw.mkdir(parents=True, exist_ok=True)
        _month = _dt.now(_ist).strftime("%Y-%m")
        _today = _dt.now(_ist).strftime("%Y-%m-%d")
        _top3 = [f[0] for f in (feature_importances[:3] if feature_importances else [])]
        _line = (
            f"{_today} | {meta['model_type'].upper()} | acc={meta['accuracy']:.1%} "
            f"| score={meta['score']:.4f} | signal={today_signal} conf={today_conf:.0%} "
            f"| top3_features={','.join(_top3)}\n"
        )
        with open(_wiki_raw / f"{_month}_evolver.txt", "a", encoding="utf-8") as _f:
            _f.write(_line)
    except Exception:
        pass

    print("\n  Model Evolver complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
