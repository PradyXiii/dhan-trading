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
        ("banknifty.csv",    lambda: fetch_dhan_index("25", "BankNifty", FROM_DATE, TO_DATE)),
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

    # BN 5-day momentum (different from trend5 which is pct chg over 5d)
    d["bn_ret5"] = (d["bn_close"] / d["bn_close"].shift(5) - 1) * 100

    # BN volume ratio vs 20-day average
    if "volume" in d.columns:
        d["bn_vol_ratio"] = d["volume"] / d["volume"].rolling(20, min_periods=5).mean()
        d["bn_vol_ratio"] = d["bn_vol_ratio"].fillna(1.0)
    else:
        d["bn_vol_ratio"] = 1.0

    # ATR(14) as % of close — intraday range regime proxy
    if all(c in d.columns for c in ["bn_high", "bn_low", "bn_close"]):
        high_low  = d["bn_high"] - d["bn_low"]
        high_prev = (d["bn_high"] - d["bn_close"].shift(1)).abs()
        low_prev  = (d["bn_low"]  - d["bn_close"].shift(1)).abs()
        tr        = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
        d["atr14_pct"] = tr.rolling(14, min_periods=5).mean() / d["bn_close"] * 100
    else:
        d["atr14_pct"] = 0.0

    return d


# Base features from ml_engine
BASE_FEATURE_COLS = [
    "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div",
    "ema20_pct", "trend5", "vix_dir", "bn_nf_div",
    "rsi14", "hv20", "bn_gap",
    "sp500_chg", "nikkei_chg", "spf_gap",
    "vix_level", "vix_pct_chg", "vix_hv_ratio",
    "bn_ret1", "bn_ret20",
    "dow", "dte",
]

EXTENDED_FEATURE_COLS = BASE_FEATURE_COLS + [
    "gold_ret", "crude_ret", "usdinr_ret", "dxy_ret", "us10y_chg",
    "pcr", "pcr_ma5", "pcr_chg",
    "vix_open_chg",
    "fii_net_cash_z", "fii_net_fut",
    "bn_ret5", "bn_vol_ratio", "atr14_pct",
]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — FEATURE SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def select_features(X, y, all_cols):
    """
    Train a quick RF on all data, return features with importance >= 1% of max.
    Always keeps all BASE_FEATURE_COLS (they are validated signal components).
    """
    from sklearn.ensemble import RandomForestClassifier

    rf = RandomForestClassifier(
        n_estimators=200, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    rf.fit(X, y)
    importances = rf.feature_importances_
    max_imp     = importances.max() if importances.max() > 0 else 1.0
    norm_imp    = importances / max_imp * 100

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

LIVE_TRADES_PATH    = f"{DATA_DIR}/live_trades.csv"
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

    if not Path(LIVE_TRADES_PATH).exists():
        return empty

    try:
        with open(LIVE_TRADES_PATH) as f:
            rows = list(_csv.DictReader(f))
    except Exception as e:
        print(f"  [feedback] Cannot read live_trades.csv: {e}")
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
        print(f"  [feedback] Injected {len(inject_rows)} live trades (weight={LIVE_INJECT_WEIGHT}×)")
    else:
        X_aug = X_all.copy()
        y_aug = y_all.copy()

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

        print(f"\n  [{mtype.upper()}] Running {n_trials} Optuna trials...")

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
                n_trials=n_trials,
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
    "bn_ret5":      "5-day Nifty momentum",
    "rsi14":        "Momentum strength (RSI)",
    "hv20":         "Recent volatility (20-day)",
    "vix_level":    "Absolute fear index level",
    "vix_pct_chg":  "Fear index daily change",
    "s_ema20":      "Above/below 20-day average signal",
    "s_trend5":     "5-day trend signal",
    "s_vix":        "VIX direction signal",
    "s_bn_nf_div":  "Nifty Bank vs Nifty divergence signal",
    "bn_nf_div":    "Nifty Bank vs Nifty divergence",
    "sp500_chg":    "US market daily move",
    "nikkei_chg":   "Japan market daily move",
    "spf_gap":      "US futures overnight gap",
    "bn_ret1":      "Yesterday Nifty return",
    "bn_ret20":     "1-month Nifty return",
    "dow":          "Day of week",
    "gold_ret":     "Gold daily move",
    "crude_ret":    "Crude oil daily move",
    "usdinr_ret":   "Rupee daily move vs USD",
    "dxy_ret":      "US Dollar index move",
    "us10y_chg":    "US 10-year interest rate change",
    "pcr":          "Put-Call Ratio (options sentiment)",
    "fii_net_fut":  "Foreign investor futures activity",
    "bn_vol_ratio": "Nifty trading volume vs normal",
    "atr14_pct":    "Expected intraday swing range",
    "bn_gap":       "Nifty open gap vs yesterday",
    "vix_dir":      "VIX direction",
    "ema20_pct":    "Distance from 20-day moving average",
}

_MODEL_NAMES = {"rf": "Random Forest", "xgb": "XGBoost", "lgb": "LightGBM", "cat": "CatBoost"}


def send_telegram_report(results, champion_meta, today_signal, today_conf,
                         feature_importances, n_features_total, live_info=None):
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
        journal_path = f"{DATA_DIR}/live_trades.csv"
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
    from ml_engine import compute_labels

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
    labels_df = compute_labels(trading_full)
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

    # ── 3b. Live trade feedback — inject real outcomes + boost miss patterns ──
    print("\n[3b/6] Live trade feedback...")
    X_aug, y_aug, sample_weights, live_info = _compute_live_feedback(
        X_all, y_all, df, selected_cols
    )
    has_feedback = live_info["n_injected"] > 0 or live_info["n_misses"] >= MIN_MISSES

    # ── 4. Model competition ──────────────────────────────────────────────────
    results, X_tr, y_tr, X_val, y_val = run_competition(
        X_aug, y_aug, selected_cols,
        sample_weight=sample_weights if has_feedback else None,
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
        )
    except Exception as e:
        print(f"  Telegram report failed: {e}")

    print("\n  Model Evolver complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
