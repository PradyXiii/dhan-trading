#!/usr/bin/env python3
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
N_TRIALS = 40
for _i, _a in enumerate(sys.argv):
    if _a == "--trials" and _i + 1 < len(sys.argv):
        try:
            N_TRIALS = int(sys.argv[_i + 1])
        except ValueError:
            pass

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — DATA REFRESH
# ─────────────────────────────────────────────────────────────────────────────

def _renew_token():
    """
    Extend the current Dhan token by 24 hours at 11 PM before the evolver runs.
    Ensures the 9:15 AM auto_trader.py run tomorrow has a valid token.
    PUT /v2/RenewToken — only works on active (non-expired) tokens.
    """
    import requests as _req
    from dotenv import load_dotenv as _lde
    import os as _os
    _lde()
    token     = _os.getenv("DHAN_ACCESS_TOKEN", "")
    client_id = _os.getenv("DHAN_CLIENT_ID",    "")
    if not token or not client_id:
        print("  Token renewal: credentials not set — skipping")
        return
    try:
        resp = _req.put(
            "https://api.dhan.co/v2/RenewToken",
            headers={"access-token": token, "dhanClientId": client_id,
                     "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            print("  Token auto-renewed for another 24h ✓")
        else:
            print(f"  Token renewal: {resp.status_code} — token still valid for today")
    except Exception as e:
        print(f"  Token renewal skipped ({e})")


def refresh_data():
    """Refresh all data CSVs using data_fetcher.py functions."""
    print("\n[1/6] Refreshing data...")

    # Renew token first — 11 PM now, 9:15 AM trade tomorrow needs a valid token
    _renew_token()

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
        if os.path.exists(path):
            tmp = pd.read_csv(path, parse_dates=["date"])
            tmp = tmp[["date", "close"]].rename(columns={"close": col})
            df = df.merge(tmp, on="date", how="left")
            df[col] = df[col].ffill(limit=5)
        else:
            df[col] = np.nan

    # PCR: prefer pcr_live.csv (live Dhan) over pcr.csv (manual bhavcopy)
    pcr_loaded = False
    for pcr_path in [f"{DATA_DIR}/pcr_live.csv", f"{DATA_DIR}/pcr.csv"]:
        if os.path.exists(pcr_path):
            pcr = pd.read_csv(pcr_path, parse_dates=["date"])[["date", "pcr"]]
            df  = df.merge(pcr, on="date", how="left")
            df["pcr"] = df["pcr"].ffill(limit=3)
            pcr_loaded = True
            break
    if not pcr_loaded:
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
    "pcr", "fii_net_fut", "bn_ret5", "bn_vol_ratio", "atr14_pct",
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
    raise ValueError(f"Unknown model_type: {model_type}")


def _optuna_objective(trial, model_type, X_tr, y_tr, X_val, y_val):
    if model_type == "rf":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 100, 500),
            "max_depth":       trial.suggest_int("max_depth", 4, 10),
            "min_samples_leaf":trial.suggest_int("min_samples_leaf", 5, 20),
            "max_features":    trial.suggest_categorical("max_features", ["sqrt", 0.5, 0.7]),
        }
    elif model_type == "xgb":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 100, 400),
            "max_depth":       trial.suggest_int("max_depth", 3, 8),
            "learning_rate":   trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":       trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree":trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "scale_pos_weight":sum(y_tr == 0) / max(sum(y_tr == 1), 1),
        }
    elif model_type == "lgb":
        params = {
            "n_estimators":    trial.suggest_int("n_estimators", 100, 400),
            "max_depth":       trial.suggest_int("max_depth", 3, 8),
            "learning_rate":   trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "num_leaves":      trial.suggest_int("num_leaves", 20, 80),
            "min_child_samples":trial.suggest_int("min_child_samples", 10, 30),
        }

    model = _build_model(model_type, params)
    model.fit(X_tr, y_tr)
    y_pred = model.predict(X_val)
    y_prob = model.predict_proba(X_val)[:, list(model.classes_).index(1)] \
             if 1 in model.classes_ else np.zeros(len(y_val))
    return _score(y_val, y_pred, y_prob)


def run_competition(X, y, feature_cols, n_trials=N_TRIALS):
    """
    Run Optuna HPO for RF, XGBoost, LightGBM on the same temporal split.
    Returns list of result dicts sorted by composite score descending.
    """
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    X_tr, y_tr, X_val, y_val = _temporal_split(X, y)
    print(f"\n[4/6] Model competition  (train={len(X_tr)}, holdout={len(X_val)}, "
          f"trials={n_trials} each)")

    results = []
    for mtype in ["rf", "xgb", "lgb"]:
        print(f"\n  [{mtype.upper()}] Running {n_trials} Optuna trials...")

        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(
            lambda trial: _optuna_objective(trial, mtype, X_tr, y_tr, X_val, y_val),
            n_trials=n_trials,
            show_progress_bar=False,
        )

        best_params  = study.best_params
        best_score   = study.best_value

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

def train_champion(champion_meta, X_all, y_all):
    """Retrain champion model with best params on ALL data."""
    model = _build_model(champion_meta["model_type"], champion_meta["params"])
    model.fit(X_all, y_all)
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


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 8 — TELEGRAM REPORT (plain English)
# ─────────────────────────────────────────────────────────────────────────────

# Human-friendly names for the top features
_FEATURE_LABELS = {
    "trend5":       "5-day trend direction",
    "vix_hv_ratio": "Fear index vs historical swings",
    "ema20_pct":    "How far BankNifty is from its average",
    "dte":          "Days to expiry",
    "bn_ret5":      "5-day BankNifty momentum",
    "rsi14":        "Momentum strength (RSI)",
    "hv20":         "Recent volatility (20-day)",
    "vix_level":    "Absolute fear index level",
    "vix_pct_chg":  "Fear index daily change",
    "s_ema20":      "Above/below 20-day average signal",
    "s_trend5":     "5-day trend signal",
    "s_vix":        "VIX direction signal",
    "s_bn_nf_div":  "BankNifty vs Nifty divergence signal",
    "bn_nf_div":    "BankNifty vs Nifty divergence",
    "sp500_chg":    "US market daily move",
    "nikkei_chg":   "Japan market daily move",
    "spf_gap":      "US futures overnight gap",
    "bn_ret1":      "Yesterday BankNifty return",
    "bn_ret20":     "1-month BankNifty return",
    "dow":          "Day of week",
    "gold_ret":     "Gold daily move",
    "crude_ret":    "Crude oil daily move",
    "usdinr_ret":   "Rupee daily move vs USD",
    "dxy_ret":      "US Dollar index move",
    "us10y_chg":    "US 10-year interest rate change",
    "pcr":          "Put-Call Ratio (options sentiment)",
    "fii_net_fut":  "Foreign investor futures activity",
    "bn_vol_ratio": "BankNifty trading volume vs normal",
    "atr14_pct":    "Expected intraday swing range",
    "bn_gap":       "BankNifty open gap vs yesterday",
    "vix_dir":      "VIX direction",
    "ema20_pct":    "Distance from 20-day moving average",
}

_MODEL_NAMES = {"rf": "Random Forest", "xgb": "XGBoost", "lgb": "LightGBM"}


def send_telegram_report(results, champion_meta, today_signal, today_conf,
                         feature_importances, n_features_total):
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

    # Keep only Mon/Tue/Thu/Fri
    trading = df[df["date"].dt.weekday.isin([0, 1, 2, 3, 4])].copy().reset_index(drop=True)

    # Labels (binary: 1=CALL, 0=PUT)
    labels_df = compute_labels(trading)
    y_all = np.array([1 if l == "CALL" else 0 for l in labels_df["label"].values])

    # Filter to rows that have labels
    trading = trading.iloc[:len(y_all)].copy()
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

    # ── 4. Model competition ──────────────────────────────────────────────────
    results, X_tr, y_tr, X_val, y_val = run_competition(X_all, y_all, selected_cols)

    print("\n[4/6] Competition results:")
    for i, r in enumerate(results):
        medal = ["Champion", "2nd", "3rd"][i] if i < 3 else f"{i+1}th"
        print(f"  {medal}: {_MODEL_NAMES.get(r['model_type'], r['model_type'])}"
              f"  Acc={r['accuracy']:.1%}  Score={r['score']:.4f}")

    champion = results[0]

    # ── 5+6. Final training on all data ──────────────────────────────────────
    print(f"\n[5/6] Final training: {_MODEL_NAMES[champion['model_type']]} on all {len(X_all)} rows...")
    final_model = train_champion(champion, X_all, y_all)

    # ── 7. Save champion ──────────────────────────────────────────────────────
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
        "trained_at":  datetime.now(_IST).isoformat(),
    }
    save_champion(final_model, meta)

    # ── 8. Predict tomorrow using the just-trained champion ───────────────────
    today_ts = pd.Timestamp(datetime.now(_IST).date())
    today_rows = trading[trading["date"] == today_ts]

    if not today_rows.empty:
        X_today = today_rows[selected_cols].fillna(0).values.astype(float)
        proba   = final_model.predict_proba(X_today)[0]
        classes = list(final_model.classes_)
        p_call  = proba[classes.index(1)] if 1 in classes else 0.5
        p_put   = proba[classes.index(0)] if 0 in classes else 0.5
        today_signal = "CALL" if p_call >= p_put else "PUT"
        today_conf   = max(p_call, p_put)
    else:
        today_signal = "NONE"
        today_conf   = 0.5

    elapsed = (datetime.now() - start).seconds
    print(f"\n  Elapsed: {elapsed // 60}m {elapsed % 60}s")

    # ── 8. Telegram report ────────────────────────────────────────────────────
    print("\n[6/6] Sending Telegram report...")
    try:
        send_telegram_report(
            results         = results,
            champion_meta   = meta,
            today_signal    = today_signal,
            today_conf      = today_conf,
            feature_importances = feature_importances,
            n_features_total    = len(all_cols_present),
        )
    except Exception as e:
        print(f"  Telegram report failed: {e}")

    print("\n  Model Evolver complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
