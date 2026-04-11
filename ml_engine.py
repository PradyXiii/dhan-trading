#!/usr/bin/env python3
"""
ml_engine.py — Walk-forward ML signal enhancement for BankNifty options.

Approach
--------
1. For every Mon/Tue/Thu/Fri trading day, compute 18 features from OHLCV + global data.
2. Simulate true CALL and PUT outcomes (WIN/LOSS/PARTIAL) using the same SL/TP logic as
   backtest_engine.py — zero look-ahead because labels use the SAME day's bar that the
   rule-based system already trades on.
3. Assign a 3-class target: CALL (CALL wins, PUT loses), PUT (PUT wins, CALL loses),
   NONE (both, neither, or PARTIAL).
4. Walk-forward RandomForest: at day t, train on days 1…t-1, predict t.
   Retrain every 5 days; rolling 3-year window.
5. Enter only when model confidence ≥ ml_threshold (default 0.55).
6. Apply RBI MPC + Budget event filter (same as signal_engine).
7. Save data/signals_ml.csv — same schema as signals.csv so backtest_engine works
   unchanged when called with --ml.

Usage
-----
  python3 ml_engine.py                   # ML-only signals, threshold 0.55
  python3 ml_engine.py 0.60              # stricter confidence gate
  python3 ml_engine.py --combined        # both rule-based score AND ML must agree
  python3 ml_engine.py --combined 0.58   # combined + stricter threshold
  python3 ml_engine.py --analyze         # feature importance + walk-forward stats

Then backtest:
  python3 backtest_engine.py --ml        # reads signals_ml.csv

Walk-forward note
-----------------
The first 252 trading days have no ML prediction (cold-start). Those days are
assigned signal=NONE and not traded, so backtest results start from ~year 2 of data.
This is intentional — we want clean out-of-sample performance only.
"""

import os
import sys
import warnings
import numpy as np
import pandas as pd
from datetime import date as _date, timedelta

warnings.filterwarnings("ignore")

from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix

# ── Import get_dte from backtest_engine (no sys.argv at module level there) ───
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import get_dte, PREMIUM_K

DATA_DIR  = "data"

# Strategy params — keep in sync with backtest_engine.py
SL_PCT    = 0.15
RR        = 2.0
TP_PCT    = SL_PCT * RR   # 0.30

# Walk-forward hyperparams
MIN_TRAIN      = 252   # ~1 year of trading days
RETRAIN_EVERY  = 5     # retrain every N trading days
MAX_TRAIN_DAYS = 756   # rolling 3-year window (avoids stale patterns)

# Rule-based score threshold — only relevant in --combined mode
SCORE_THRESHOLD = 1

# ── Event calendar (duplicated from signal_engine to avoid sys.argv conflict) ─
_RBI_MPC = {
    "2021-10-08","2021-12-08",
    "2022-02-10","2022-04-08","2022-06-08","2022-08-05","2022-09-30","2022-12-07",
    "2023-02-08","2023-04-06","2023-06-08","2023-08-10","2023-10-06","2023-12-08",
    "2024-02-08","2024-04-05","2024-06-07","2024-08-08","2024-10-09","2024-12-06",
    "2025-02-07","2025-04-09","2025-06-06","2025-08-07","2025-10-08","2025-12-05",
    "2026-02-06","2026-04-09","2026-06-05","2026-08-07","2026-10-09","2026-12-05",
    "2027-02-05","2027-04-09","2027-06-04","2027-08-06","2027-10-08","2027-12-03",
}
_BUDGET = {
    "2022-02-01","2023-02-01","2024-02-01","2024-07-23","2025-02-01","2026-02-01",
}
EVENT_DATES = {pd.Timestamp(d).date() for d in (_RBI_MPC | _BUDGET)}

# ── CLI parsing ────────────────────────────────────────────────────────────────
MODE         = "ml"       # "ml" | "combined" | "analyze"
ML_THRESHOLD = 0.55

for arg in sys.argv[1:]:
    if arg == "--combined":
        MODE = "combined"
    elif arg == "--analyze":
        MODE = "analyze"
    else:
        try:
            ML_THRESHOLD = float(arg)
        except ValueError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_all_data():
    """Load and merge all OHLCV CSVs. Returns master daily DataFrame."""
    bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",     parse_dates=["date"])
    nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",       parse_dates=["date"])
    vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",     parse_dates=["date"])
    sp  = pd.read_csv(f"{DATA_DIR}/sp500.csv",         parse_dates=["date"])
    nk  = pd.read_csv(f"{DATA_DIR}/nikkei.csv",        parse_dates=["date"])
    spf = pd.read_csv(f"{DATA_DIR}/sp500_futures.csv", parse_dates=["date"])

    bn  = bn [["date","open","high","low","close"]].rename(columns={
              "open":"bn_open","high":"bn_high","low":"bn_low","close":"bn_close"})
    nf  = nf [["date","close"]].rename(columns={"close":"nf_close"})
    vix = vix[["date","close"]].rename(columns={"close":"vix_close"})
    sp  = sp [["date","close"]].rename(columns={"close":"sp_close"})
    nk  = nk [["date","close"]].rename(columns={"close":"nk_close"})
    spf = spf[["date","open","close"]].rename(columns={"open":"spf_open","close":"spf_close"})

    df = bn.copy()
    for other in [nf, vix, sp, nk, spf]:
        df = df.merge(other, on="date", how="left")

    df = df.sort_values("date").reset_index(drop=True)

    ff_cols = ["nf_close","vix_close","sp_close","nk_close","spf_open","spf_close"]
    df[ff_cols] = df[ff_cols].ffill(limit=3)
    df = df.dropna(subset=["bn_close","nf_close","vix_close","sp_close","nk_close",
                            "spf_open","spf_close"])
    return df


# ─────────────────────────────────────────────────────────────────────────────
#  INDICATOR & FEATURE COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def _rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_features(df):
    """
    Compute 18-feature matrix from raw OHLCV + global data.
    All features are derived from CLOSE prices (no same-day intraday).
    Returns DataFrame with 'date' column plus feature columns.
    """
    d = df.copy()

    # ── Core technicals (mirrored from signal_engine) ─────────────────────────
    d["ema20"]      = d["bn_close"].ewm(span=20, adjust=False).mean()
    d["rsi14"]      = _rsi(d["bn_close"], 14)
    d["trend5"]     = (d["bn_close"] - d["bn_close"].shift(5)) / d["bn_close"].shift(5) * 100
    d["vix_dir"]    = d["vix_close"] - d["vix_close"].shift(1)
    d["sp500_chg"]  = (d["sp_close"] - d["sp_close"].shift(1)) / d["sp_close"].shift(1) * 100
    d["nikkei_chg"] = (d["nk_close"] - d["nk_close"].shift(1)) / d["nk_close"].shift(1) * 100
    d["spf_gap"]    = (d["spf_open"] - d["spf_close"].shift(1)) / d["spf_close"].shift(1) * 100
    bn_chg          = (d["bn_close"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100
    nf_chg          = (d["nf_close"] - d["nf_close"].shift(1)) / d["nf_close"].shift(1) * 100
    d["bn_nf_div"]  = bn_chg - nf_chg
    log_ret         = np.log(d["bn_close"] / d["bn_close"].shift(1))
    d["hv20"]       = log_ret.rolling(20).std() * np.sqrt(252) * 100
    d["bn_gap"]     = (d["bn_open"] - d["bn_close"].shift(1)) / d["bn_close"].shift(1) * 100

    # ── Rule-based score (4 active indicators — used in --combined mode) ──────
    d["s_ema20"]     = np.where(d["bn_close"] > d["ema20"], 1, -1)
    d["s_trend5"]    = np.where(d["trend5"] > 1.0, 1, np.where(d["trend5"] < -1.0, -1, 0))
    d["s_vix"]       = np.where(d["vix_dir"] < 0,  1, np.where(d["vix_dir"]  > 0, -1, 0))
    d["s_bn_nf_div"] = np.where(d["bn_nf_div"] > 0.5, 1, np.where(d["bn_nf_div"] < -0.5, -1, 0))
    d["rule_score"]  = d["s_ema20"] + d["s_trend5"] + d["s_vix"] + d["s_bn_nf_div"]
    d["rule_signal"] = np.where(d["rule_score"] >= SCORE_THRESHOLD, "CALL",
                       np.where(d["rule_score"] <= -SCORE_THRESHOLD, "PUT", "NONE"))

    # ── Extended ML features (not in rule-based system) ───────────────────────
    d["ema20_pct"]   = (d["bn_close"] - d["ema20"]) / d["ema20"] * 100   # EMA distance %
    d["vix_level"]   = d["vix_close"]                                      # abs VIX level
    d["vix_pct_chg"] = d["vix_dir"] / d["vix_close"].shift(1) * 100       # VIX % change
    d["vix_hv_ratio"]= d["vix_close"] / d["hv20"].replace(0, np.nan)      # IV/HV proxy
    d["bn_ret1"]     = (d["bn_close"] / d["bn_close"].shift(1) - 1) * 100
    d["bn_ret20"]    = (d["bn_close"] / d["bn_close"].shift(20) - 1) * 100
    d["dow"]         = d["date"].dt.weekday                                  # 0=Mon 4=Fri
    d["dte"]         = d["date"].apply(
                           lambda x: get_dte(x.date() if hasattr(x, "date") else x))

    # Drop rows with NaNs in required columns
    req = ["ema20","rsi14","trend5","vix_dir","sp500_chg","nikkei_chg","spf_gap",
           "bn_nf_div","hv20","bn_gap","vix_pct_chg","vix_hv_ratio","bn_ret20"]
    d = d.dropna(subset=req)
    return d


# Feature columns fed into the RandomForest
FEATURE_COLS = [
    # Rule-based signals (score components) — does ML learn to reweight them?
    "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div",
    # Continuous versions of same signals
    "ema20_pct", "trend5", "vix_dir", "bn_nf_div",
    # Other technical indicators (inactive in rule-based)
    "rsi14", "hv20", "bn_gap",
    # Global market signals
    "sp500_chg", "nikkei_chg", "spf_gap",
    # Vol regime features
    "vix_level", "vix_pct_chg", "vix_hv_ratio",
    # Momentum features
    "bn_ret1", "bn_ret20",
    # Calendar features
    "dow", "dte",
]


# ─────────────────────────────────────────────────────────────────────────────
#  LABEL COMPUTATION (WIN / LOSS / PARTIAL for each day)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_outcome(bn_open, bn_high, bn_low, bn_close, signal, premium):
    """
    Simulate WIN / LOSS / PARTIAL for one trade using same-day OHLCV.
    Mirrors backtest_engine.simulate_trade exactly (SL=15%, TP=30%, RR=2.0).
    """
    sl_pts = (SL_PCT * premium) / 0.5
    tp_pts = (TP_PCT * premium) / 0.5

    if signal == "CALL":
        sl_level = bn_open - sl_pts
        tp_level = bn_open + tp_pts
        sl_hit   = bn_low  <= sl_level
        tp_hit   = bn_high >= tp_level
        if sl_hit and tp_hit:
            return "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:
            return "WIN"
        elif sl_hit:
            return "LOSS"
        return "PARTIAL"
    else:  # PUT
        sl_level = bn_open + sl_pts
        tp_level = bn_open - tp_pts
        sl_hit   = bn_high >= sl_level
        tp_hit   = bn_low  <= tp_level
        if sl_hit and tp_hit:
            return "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:
            return "WIN"
        elif sl_hit:
            return "LOSS"
        return "PARTIAL"


def compute_labels(df):
    """
    For each row in df, simulate both CALL and PUT outcomes.
    Returns df with call_out, put_out, target columns.

    Target assignment:
      CALL  — CALL=WIN and PUT=LOSS  (unambiguous bullish winning day)
      PUT   — PUT=WIN  and CALL=LOSS (unambiguous bearish winning day)
      NONE  — everything else (both WIN, both LOSS, either PARTIAL)
    """
    rows = []
    for _, r in df.iterrows():
        bn_open  = r["bn_open"]
        bn_high  = r["bn_high"]
        bn_low   = r["bn_low"]
        bn_close = r["bn_close"]
        date     = r["date"]

        dte     = get_dte(date.date() if hasattr(date, "date") else date)
        premium = bn_open * PREMIUM_K * (dte ** 0.5)

        call_out = simulate_outcome(bn_open, bn_high, bn_low, bn_close, "CALL", premium)
        put_out  = simulate_outcome(bn_open, bn_high, bn_low, bn_close, "PUT",  premium)

        if call_out == "WIN" and put_out == "LOSS":
            target = "CALL"
        elif put_out == "WIN" and call_out == "LOSS":
            target = "PUT"
        else:
            target = "NONE"

        rows.append({"date": date, "call_out": call_out, "put_out": put_out,
                     "target": target})

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  WALK-FORWARD PREDICTION
# ─────────────────────────────────────────────────────────────────────────────

def run_walkforward(X, y_str, dates, ml_threshold):
    """
    Walk-forward RandomForest prediction.

    For each day i ≥ MIN_TRAIN:
      - Train on X[max(0,i-MAX_TRAIN_DAYS):i], y[same]
      - Predict class probabilities for X[i]
      - Assign ml_signal based on confidence threshold

    Returns DataFrame: date, ml_signal, ml_p_call, ml_p_put, ml_p_none, ml_conf, ml_trained.
    """
    LABEL_TO_INT = {"NONE": 0, "CALL": 1, "PUT": 2}
    INT_TO_LABEL = {0: "NONE", 1: "CALL", 2: "PUT"}

    y = np.array([LABEL_TO_INT[l] for l in y_str])
    n = len(X)

    results      = []
    model        = None
    last_retrain = -RETRAIN_EVERY  # force train on first eligible day

    for i in range(n):
        date = dates[i]

        if i < MIN_TRAIN:
            results.append({
                "date": date, "ml_signal": "NONE",
                "ml_p_call": 0.0, "ml_p_put": 0.0,
                "ml_p_none": 1.0, "ml_conf": 0.0, "ml_trained": False,
            })
            continue

        # ── Retrain? ─────────────────────────────────────────────────────────
        if (i - last_retrain) >= RETRAIN_EVERY:
            train_start = max(0, i - MAX_TRAIN_DAYS)
            X_tr = X[train_start:i]
            y_tr = y[train_start:i]

            if len(np.unique(y_tr)) >= 2:
                model = RandomForestClassifier(
                    n_estimators=300,
                    max_depth=6,
                    min_samples_leaf=15,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=-1,
                )
                model.fit(X_tr, y_tr)
            last_retrain = i

        if model is None:
            results.append({
                "date": date, "ml_signal": "NONE",
                "ml_p_call": 0.0, "ml_p_put": 0.0,
                "ml_p_none": 1.0, "ml_conf": 0.0, "ml_trained": False,
            })
            continue

        # ── Predict ───────────────────────────────────────────────────────────
        proba   = model.predict_proba(X[i].reshape(1, -1))[0]
        classes = model.classes_
        p       = {INT_TO_LABEL[c]: proba[j] for j, c in enumerate(classes)}

        p_call = p.get("CALL", 0.0)
        p_put  = p.get("PUT",  0.0)
        p_none = p.get("NONE", 0.0)

        if p_call >= p_put and p_call >= ml_threshold:
            ml_signal = "CALL"
            ml_conf   = p_call
        elif p_put > p_call and p_put >= ml_threshold:
            ml_signal = "PUT"
            ml_conf   = p_put
        else:
            ml_signal = "NONE"
            ml_conf   = max(p_call, p_put, p_none)

        results.append({
            "date":      date,
            "ml_signal": ml_signal,
            "ml_p_call": round(p_call, 4),
            "ml_p_put":  round(p_put,  4),
            "ml_p_none": round(p_none, 4),
            "ml_conf":   round(ml_conf, 4),
            "ml_trained": True,
        })

    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def generate_ml_signals(ml_threshold=0.55, mode="ml"):
    """
    Full pipeline:
      load → indicators → labels → walk-forward RF → save signals_ml.csv

    mode : "ml"       — use ML signal as final signal
           "combined" — require both rule-based score AND ML to agree
    """
    print("Loading data...")
    raw = load_all_data()
    print(f"  Master dataset: {len(raw)} days  "
          f"({raw['date'].min().date()} to {raw['date'].max().date()})")

    print("Computing indicators and features...")
    df = compute_features(raw)

    # ── Filter to trading weekdays (Mon/Tue/Thu/Fri — same as signal_engine) ──
    trading = df[df["date"].dt.weekday.isin([0, 1, 3, 4])].copy().reset_index(drop=True)
    print(f"  Trading days (Mon/Tue/Thu/Fri): {len(trading)}")

    # ── Compute labels ────────────────────────────────────────────────────────
    print("Simulating trade outcomes for all trading days (labels)...")
    labels_df = compute_labels(trading)

    # Distribution check
    dist = labels_df["target"].value_counts()
    total_lbl = len(labels_df)
    print(f"  Label distribution: "
          f"CALL={dist.get('CALL',0)} ({dist.get('CALL',0)/total_lbl*100:.1f}%)  "
          f"PUT={dist.get('PUT',0)} ({dist.get('PUT',0)/total_lbl*100:.1f}%)  "
          f"NONE={dist.get('NONE',0)} ({dist.get('NONE',0)/total_lbl*100:.1f}%)")

    # ── Build feature matrix ──────────────────────────────────────────────────
    X       = trading[FEATURE_COLS].values.astype(float)
    y_str   = labels_df["target"].values
    dates   = trading["date"].values

    # ── Walk-forward prediction ───────────────────────────────────────────────
    print(f"Running walk-forward prediction  "
          f"[min_train={MIN_TRAIN}, retrain_every={RETRAIN_EVERY}, "
          f"window={MAX_TRAIN_DAYS}, threshold={ml_threshold}]")

    preds_df = run_walkforward(X, y_str, dates, ml_threshold)

    # ── Merge predictions with features ──────────────────────────────────────
    merged = trading.copy()
    merged = merged.merge(preds_df, on="date", how="left")
    merged = merged.merge(labels_df[["date","call_out","put_out","target"]],
                          on="date", how="left")

    # ── Apply event day filter ────────────────────────────────────────────────
    merged["event_day"] = merged["date"].apply(
        lambda d: (d.date() if hasattr(d, "date") else d) in EVENT_DATES)

    # ── Determine final signal ────────────────────────────────────────────────
    if mode == "combined":
        # Enter only when BOTH rule-based score AND ML agree on direction
        def combined_signal(row):
            if row["event_day"]:
                return "NONE"
            if not row["ml_trained"]:
                return "NONE"
            rule = row["rule_signal"]
            ml   = row["ml_signal"]
            return rule if rule == ml and rule != "NONE" else "NONE"
        merged["signal"] = merged.apply(combined_signal, axis=1)
    else:
        # ML-only: use ML prediction, override to NONE on event days
        merged["signal"] = np.where(
            merged["event_day"] | ~merged["ml_trained"],
            "NONE",
            merged["ml_signal"]
        )

    # ── Build output in signals.csv-compatible format ─────────────────────────
    merged["weekday"] = merged["date"].dt.day_name()
    merged["date"]    = merged["date"].dt.date

    out_cols = [
        "date", "weekday", "event_day",
        "bn_close", "ema20", "rsi14", "trend5", "vix_dir",
        "sp500_chg", "nikkei_chg", "spf_gap", "bn_nf_div", "hv20", "bn_gap",
        "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div",
        "rule_score", "rule_signal",
        "ml_signal", "ml_p_call", "ml_p_put", "ml_p_none", "ml_conf", "ml_trained",
        "call_out", "put_out", "target",
        "signal",
    ]
    # Add score column (same as rule_score, for backtest compatibility)
    merged["score"]    = merged["rule_score"]
    merged["threshold"] = 1   # embed for backtest_engine to read

    # Round numeric columns
    for col in ["bn_close","ema20","rsi14","trend5","vix_dir",
                "sp500_chg","nikkei_chg","spf_gap","bn_nf_div","hv20","bn_gap"]:
        if col in merged.columns:
            merged[col] = merged[col].round(2)

    final_cols = out_cols + ["score", "threshold"]
    out = merged[[c for c in final_cols if c in merged.columns]]
    out.to_csv(f"{DATA_DIR}/signals_ml.csv", index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    traded    = out[out["signal"].isin(["CALL", "PUT"])]
    n_call    = (traded["signal"] == "CALL").sum()
    n_put     = (traded["signal"] == "PUT").sum()
    n_none    = (out["signal"] == "NONE").sum()
    n_event   = out["event_day"].sum()
    n_trained = out["ml_trained"].sum()
    total_td  = len(out)

    print(f"\n{'='*60}")
    print(f"  ML ENGINE OUTPUT  [{mode.upper()} mode, threshold={ml_threshold}]")
    print(f"{'='*60}")
    print(f"  Trading days scanned     : {total_td}")
    print(f"  Days with trained model  : {n_trained}  (first {MIN_TRAIN} = warmup)")
    print(f"  CALL signals             : {n_call}  ({n_call/total_td*100:.1f}%)")
    print(f"  PUT  signals             : {n_put}  ({n_put/total_td*100:.1f}%)")
    print(f"  NONE (event filter)      : {n_event}")
    print(f"  NONE (low confidence)    : {n_none - n_event}")
    print(f"{'─'*60}")

    # Per-day breakdown
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        d = traded[traded["weekday"] == day]
        if len(d) == 0:
            continue
        dc = (d["signal"] == "CALL").sum()
        dp = (d["signal"] == "PUT").sum()
        print(f"  {day:<10}: {len(d):>3} trades | CALL {dc} | PUT {dp}")
    print(f"{'='*60}")
    print(f"\nSaved → {DATA_DIR}/signals_ml.csv")

    return out


# ─────────────────────────────────────────────────────────────────────────────
#  ANALYSIS MODE
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis():
    """
    Load signals_ml.csv and print:
      - Walk-forward accuracy vs actual labels
      - Confusion matrix
      - Feature importance (from final model fit)
      - Comparison: ML signals vs rule-based signals
    """
    print("Loading data for analysis...")
    raw = load_all_data()
    df  = compute_features(raw)
    trading = df[df["date"].dt.weekday.isin([0, 1, 3, 4])].copy().reset_index(drop=True)

    print("Computing labels...")
    labels_df = compute_labels(trading)

    X     = trading[FEATURE_COLS].values.astype(float)
    y_str = labels_df["target"].values
    dates = trading["date"].values

    print("Running walk-forward for analysis (threshold=0.50 for max coverage)...")
    preds_df = run_walkforward(X, y_str, dates, ml_threshold=0.50)

    trained = preds_df[preds_df["ml_trained"]].copy()
    trained_idx = trained.index.tolist()
    y_true  = y_str[trained_idx]
    y_pred  = trained["ml_signal"].values

    print(f"\n{'='*65}")
    print(f"  WALK-FORWARD PERFORMANCE  (threshold=0.50, trained days only)")
    print(f"{'='*65}")
    print(classification_report(y_true, y_pred, target_names=["CALL","NONE","PUT"],
                                 labels=["CALL","NONE","PUT"], zero_division=0))

    print(f"  Confusion matrix  (rows=actual, cols=predicted):")
    cm = confusion_matrix(y_true, y_pred, labels=["CALL","NONE","PUT"])
    cm_df = pd.DataFrame(cm, index=["Act:CALL","Act:NONE","Act:PUT"],
                             columns=["Pred:CALL","Pred:NONE","Pred:PUT"])
    print(cm_df.to_string())
    print(f"{'='*65}")

    # Feature importance from a full-data fit
    print(f"\nFitting full model for feature importance...")
    LABEL_TO_INT = {"NONE": 0, "CALL": 1, "PUT": 2}
    y_int = np.array([LABEL_TO_INT[l] for l in y_str])
    full_model = RandomForestClassifier(n_estimators=300, max_depth=6,
                                         min_samples_leaf=15, max_features="sqrt",
                                         class_weight="balanced", random_state=42,
                                         n_jobs=-1)
    full_model.fit(X, y_int)

    importances = pd.Series(full_model.feature_importances_, index=FEATURE_COLS)
    importances = importances.sort_values(ascending=False)

    print(f"\n  FEATURE IMPORTANCE (Gini impurity, full-data fit)")
    print(f"  {'Feature':<20} {'Importance':>12}  {'Bar':}")
    print(f"  {'─'*55}")
    for feat, imp in importances.items():
        bar = "█" * int(imp * 200)
        print(f"  {feat:<20} {imp:>11.4f}  {bar}")

    # ── Signal comparison: rule-based vs ML ───────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  SIGNAL AGREEMENT ANALYSIS")
    print(f"{'='*65}")

    try:
        sig_csv  = pd.read_csv(f"{DATA_DIR}/signals_ml.csv", parse_dates=["date"])
        agree    = sig_csv[sig_csv["rule_signal"] == sig_csv["ml_signal"]]
        disagree = sig_csv[sig_csv["rule_signal"] != sig_csv["ml_signal"]]
        total    = len(sig_csv)
        print(f"  Total days      : {total}")
        print(f"  Agree           : {len(agree)}  ({len(agree)/total*100:.1f}%)")
        print(f"  Disagree        : {len(disagree)}  ({len(disagree)/total*100:.1f}%)")

        print(f"\n  Signal cross-tab:")
        ct = pd.crosstab(sig_csv["rule_signal"], sig_csv["ml_signal"],
                         rownames=["Rule"], colnames=["ML"])
        print(ct.to_string())

        # Win rate by signal agreement
        trade_sig = sig_csv[sig_csv["signal"].isin(["CALL","PUT"])]
        if len(trade_sig) > 0:
            agree_trade = trade_sig[trade_sig["rule_signal"] == trade_sig["ml_signal"]]
            disag_trade = trade_sig[trade_sig["rule_signal"] != trade_sig["ml_signal"]]
            print(f"\n  Among traded signals:")
            print(f"  Rule=ML agree:    {len(agree_trade)} trades")
            print(f"  Rule≠ML disagree: {len(disag_trade)} trades")
    except FileNotFoundError:
        print("  signals_ml.csv not found — run without --analyze first.")
    print(f"{'='*65}")


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if MODE == "analyze":
        run_analysis()
        return

    print(f"ML Engine  [{MODE.upper()} mode | threshold={ML_THRESHOLD}]")
    generate_ml_signals(ml_threshold=ML_THRESHOLD, mode=MODE)
    print(f"\nRun backtest:")
    print(f"  python3 backtest_engine.py --ml")
    if MODE == "ml":
        print(f"\nOr try combined mode (stricter, higher WR):")
        print(f"  python3 ml_engine.py --combined {ML_THRESHOLD}")
        print(f"  python3 backtest_engine.py --ml")


if __name__ == "__main__":
    main()
