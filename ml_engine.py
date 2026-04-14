#!/usr/bin/env python3
"""
ml_engine.py — Walk-forward ML direction engine for BankNifty options.

Design intent
-------------
The ML engine is a DIRECTION ORACLE, not a filter. It does not reduce the number
of trades — it takes the same eligible trading days and predicts the better direction
(CALL or PUT) using pattern recognition across all indicators.

For every Mon/Tue/Thu/Fri:
  1. Compute 21 features from OHLCV + global market data.
  2. Walk-forward RandomForest (train on past, predict present) outputs:
       P(CALL) = probability the day favours a bullish options trade
       P(PUT)  = probability the day favours a bearish options trade
  3. Signal = argmax(P(CALL), P(PUT)) — ALWAYS a direction, no skipping.
  4. Event days (RBI MPC, Budget) → NONE override.
  5. Pre-warmup (first 252 days) → fallback to rule-based direction.

Result: same trade count as rule-based; ML improves directional accuracy.

Labels for training
-------------------
Binary direction label derived from SL/TP simulation:
  CALL  if CALL trade wins and PUT does not  (definitive bullish day)
  PUT   if PUT  trade wins and CALL does not (definitive bearish day)
  tie   if both win or both lose             (argmax tiebreak from close vs open)

No NONE class — every day gets a direction label, so the RF always outputs one.

Modes
-----
  python3 ml_engine.py                  # direction oracle (default, all trades)
  python3 ml_engine.py --filter 0.60    # confidence gate: fewer trades, higher WR
  python3 ml_engine.py --analyze        # feature importance + confusion matrix
  python3 backtest_engine.py --ml       # backtest with signals_ml.csv
"""

import os
import sys
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
# Suppress warnings in joblib worker processes (they don't inherit filterwarnings)
os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning")

import gc
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import get_dte, PREMIUM_K

DATA_DIR     = "data"
MODELS_DIR   = "models"
CHAMPION_PKL = f"{MODELS_DIR}/champion.pkl"
CHAMPION_META= f"{MODELS_DIR}/champion_meta.json"

# Max age of champion model before falling back to retrain (calendar days)
CHAMPION_MAX_AGE_DAYS = 2

# Strategy params — keep in sync with backtest_engine.py and auto_trader.py
SL_PCT    = 0.15
RR        = 2.5
TP_PCT    = SL_PCT * RR   # 0.375

# Walk-forward params
MIN_TRAIN      = 252   # ~1 year before first ML prediction
RETRAIN_EVERY  = 5     # retrain every 5 trading days
MAX_TRAIN_DAYS = 756   # rolling 3-year window

SCORE_THRESHOLD = 1    # rule-based fallback threshold (pre-warmup)

# ── Event calendar ─────────────────────────────────────────────────────────────
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
MODE         = "direction"  # "direction" | "filter" | "analyze"
ML_THRESHOLD = 0.55         # only used in --filter mode

_args = sys.argv[1:]
i = 0
while i < len(_args):
    if _args[i] == "--filter":
        MODE = "filter"
        if i + 1 < len(_args):
            try:
                ML_THRESHOLD = float(_args[i + 1])
                i += 1
            except ValueError:
                pass
    elif _args[i] == "--analyze":
        MODE = "analyze"
    elif _args[i] == "--predict-today":
        MODE = "predict_today"
    else:
        try:
            ML_THRESHOLD = float(_args[i])
        except ValueError:
            pass
    i += 1


# ─────────────────────────────────────────────────────────────────────────────
#  DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_all_data():
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
    df = df.dropna(subset=["bn_close","nf_close","vix_close","sp_close",
                            "nk_close","spf_open","spf_close"])

    # ── PCR (optional) — merge from pcr_live.csv + pcr.csv if available ──────
    # Missing dates are filled with 0 so the model treats no-PCR days as neutral.
    for pcr_file in [f"{DATA_DIR}/pcr.csv", f"{DATA_DIR}/pcr_live.csv"]:
        if os.path.exists(pcr_file):
            try:
                pcr_df = pd.read_csv(pcr_file, parse_dates=["date"])[["date", "pcr"]]
                pcr_df = pcr_df.rename(columns={"pcr": "_pcr_src"})
                df = df.merge(pcr_df, on="date", how="left")
                if "pcr" in df.columns:
                    df["pcr"] = df["pcr"].combine_first(df["_pcr_src"])
                else:
                    df = df.rename(columns={"_pcr_src": "pcr"})
                df = df.drop(columns=["_pcr_src"], errors="ignore")
            except Exception:
                pass
    if "pcr" not in df.columns:
        df["pcr"] = 0.0
    df["pcr"] = df["pcr"].fillna(0.0)   # 0 = neutral when PCR missing

    return df


# ─────────────────────────────────────────────────────────────────────────────
#  INDICATORS & FEATURES
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
    Compute 21-feature matrix. All features derived from close prices or
    prior-day data — no same-day intraday leakage.
    """
    d = df.copy()

    # ── Core technicals ───────────────────────────────────────────────────────
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

    # ── Rule-based score (4 active indicators) ────────────────────────────────
    d["s_ema20"]     = np.where(d["bn_close"] > d["ema20"], 1, -1)
    d["s_trend5"]    = np.where(d["trend5"] > 1.0, 1, np.where(d["trend5"] < -1.0, -1, 0))
    d["s_vix"]       = np.where(d["vix_dir"] < 0,  1, np.where(d["vix_dir"] > 0, -1, 0))
    d["s_bn_nf_div"] = np.where(d["bn_nf_div"] > 0.5, 1, np.where(d["bn_nf_div"] < -0.5, -1, 0))
    d["rule_score"]  = d["s_ema20"] + d["s_trend5"] + d["s_vix"] + d["s_bn_nf_div"]
    d["rule_signal"] = np.where(d["rule_score"] >= SCORE_THRESHOLD, "CALL",
                       np.where(d["rule_score"] <= -SCORE_THRESHOLD, "PUT", "NONE"))

    # ── Extended ML features ──────────────────────────────────────────────────
    d["ema20_pct"]    = (d["bn_close"] - d["ema20"]) / d["ema20"] * 100
    d["vix_level"]    = d["vix_close"]
    d["vix_pct_chg"]  = d["vix_dir"] / d["vix_close"].shift(1) * 100
    d["vix_hv_ratio"] = d["vix_close"] / d["hv20"].replace(0, np.nan)
    d["bn_ret1"]      = (d["bn_close"] / d["bn_close"].shift(1) - 1) * 100
    d["bn_ret20"]     = (d["bn_close"] / d["bn_close"].shift(20) - 1) * 100
    d["dow"]          = d["date"].dt.weekday
    d["dte"]          = d["date"].apply(
                            lambda x: get_dte(x.date() if hasattr(x, "date") else x))

    req = ["ema20","rsi14","trend5","vix_dir","sp500_chg","nikkei_chg","spf_gap",
           "bn_nf_div","hv20","bn_gap","vix_pct_chg","vix_hv_ratio","bn_ret20"]
    return d.dropna(subset=req)


# 22 features fed into the RF
FEATURE_COLS = [
    # Rule-based score components (discrete ±1 signals)
    "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div",
    # Continuous versions of same signals
    "ema20_pct", "trend5", "vix_dir", "bn_nf_div",
    # Additional technical indicators
    "rsi14", "hv20", "bn_gap",
    # Global market
    "sp500_chg", "nikkei_chg", "spf_gap",
    # Volatility regime
    "vix_level", "vix_pct_chg", "vix_hv_ratio",
    # Momentum
    "bn_ret1", "bn_ret20",
    # Calendar
    "dow", "dte",
    # Options market sentiment (0 when not available, builds over time)
    "pcr",
]


# ─────────────────────────────────────────────────────────────────────────────
#  LABEL COMPUTATION — binary direction
# ─────────────────────────────────────────────────────────────────────────────

def simulate_outcome(bn_open, bn_high, bn_low, bn_close, signal, premium):
    """Simulate WIN/LOSS/PARTIAL for one trade (mirrors backtest_engine exactly)."""
    sl_pts = (SL_PCT * premium) / 0.5
    tp_pts = (TP_PCT * premium) / 0.5

    if signal == "CALL":
        sl_hit = bn_low  <= bn_open - sl_pts
        tp_hit = bn_high >= bn_open + tp_pts
        if sl_hit and tp_hit:
            return "WIN" if bn_close > bn_open else "LOSS"
        return "WIN" if tp_hit else ("LOSS" if sl_hit else "PARTIAL")
    else:
        sl_hit = bn_high >= bn_open + sl_pts
        tp_hit = bn_low  <= bn_open - tp_pts
        if sl_hit and tp_hit:
            return "WIN" if bn_close < bn_open else "LOSS"
        return "WIN" if tp_hit else ("LOSS" if sl_hit else "PARTIAL")


def compute_labels(df):
    """
    Binary direction label for every trading day — no NONE class.

    Priority:
      1. CALL wins + PUT loses → CALL
      2. PUT wins + CALL loses → PUT
      3. Tie (both WIN, both LOSS, both PARTIAL) → sign of (close - open)
         — bullish tiebreak → CALL, bearish → PUT

    Result: every day has a CALL or PUT label. RF always predicts a direction.
    """
    rows = []
    for _, r in df.iterrows():
        o, h, l, c = r["bn_open"], r["bn_high"], r["bn_low"], r["bn_close"]
        date  = r["date"]
        dte   = get_dte(date.date() if hasattr(date, "date") else date)
        prem  = o * PREMIUM_K * (dte ** 0.5)

        call_out = simulate_outcome(o, h, l, c, "CALL", prem)
        put_out  = simulate_outcome(o, h, l, c, "PUT",  prem)

        if call_out == "WIN" and put_out != "WIN":
            label = "CALL"
        elif put_out == "WIN" and call_out != "WIN":
            label = "PUT"
        else:
            # Tie: use net open-to-close direction as tiebreak
            label = "CALL" if c > o else "PUT"

        rows.append({"date": date, "call_out": call_out, "put_out": put_out,
                     "label": label})
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  WALK-FORWARD PREDICTION
# ─────────────────────────────────────────────────────────────────────────────

def run_walkforward(X, y_bin, dates, mode="direction", ml_threshold=0.55,
                    rule_signals=None):
    """
    Walk-forward binary RandomForest.

    For every day i:
      - If i < MIN_TRAIN: fallback to rule_signals[i] (pre-warmup)
      - Else: train on X[start:i], predict P(CALL) for X[i]

    mode="direction" : always output CALL or PUT — argmax, no threshold gate
    mode="filter"    : output CALL/PUT only when max(P) >= ml_threshold, else NONE

    Returns DataFrame: date, ml_signal, ml_p_call, ml_p_put, ml_conf, ml_trained.
    """
    n      = len(X)
    y      = np.array([1 if l == "CALL" else 0 for l in y_bin])   # CALL=1, PUT=0

    results      = []
    model        = None
    last_retrain = -RETRAIN_EVERY
    n_retrains   = 0
    print(f"  0/{n} days  (retraining every {RETRAIN_EVERY} days from day {MIN_TRAIN})", end="\r", flush=True)

    for i in range(n):
        date = dates[i]

        # ── Pre-warmup: fallback to rule-based direction ──────────────────────
        if i < MIN_TRAIN:
            fallback = rule_signals[i] if rule_signals is not None else "NONE"
            # Rule says NONE on score=0 days → force a direction using trend5 tiebreak
            if fallback == "NONE":
                fallback = "CALL" if X[i][FEATURE_COLS.index("trend5")] >= 0 else "PUT"
            results.append({
                "date": date, "ml_signal": fallback,
                "ml_p_call": 0.5, "ml_p_put": 0.5, "ml_conf": 0.5, "ml_trained": False,
            })
            continue

        # ── Retrain? ──────────────────────────────────────────────────────────
        if (i - last_retrain) >= RETRAIN_EVERY:
            start = max(0, i - MAX_TRAIN_DAYS)
            X_tr, y_tr = X[start:i], y[start:i]
            if len(np.unique(y_tr)) == 2:
                model = RandomForestClassifier(
                    n_estimators=60,        # 60 trees: sufficient accuracy, lower RAM/speed
                    max_depth=6,
                    min_samples_leaf=10,
                    max_features="sqrt",
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=1,               # single-thread: avoids joblib forking RAM copies
                )
                model.fit(X_tr, y_tr)
                n_retrains += 1
                gc.collect()               # free old model objects between retrains
            last_retrain = i
            print(f"  {i}/{n} days  [{n_retrains} models trained]", end="\r", flush=True)

        if model is None:
            # Single-class training slice — fall back to rule signal (no label leakage)
            fallback = rule_signals[i] if rule_signals is not None else "NONE"
            if fallback == "NONE":
                fallback = "CALL" if X[i][FEATURE_COLS.index("trend5")] >= 0 else "PUT"
            results.append({
                "date": date, "ml_signal": fallback,
                "ml_p_call": 0.5, "ml_p_put": 0.5, "ml_conf": 0.5, "ml_trained": False,
            })
            continue

        # ── Predict ───────────────────────────────────────────────────────────
        proba   = model.predict_proba(X[i].reshape(1, -1))[0]
        # model.classes_ is sorted: [0=PUT, 1=CALL] or [0=CALL, 1=PUT]
        cls_map = {c: j for j, c in enumerate(model.classes_)}
        p_call  = float(proba[cls_map[1]])   # P(CALL)
        p_put   = float(proba[cls_map[0]])   # P(PUT)

        if mode == "filter":
            # Confidence-gated: skip low-confidence days
            if p_call >= ml_threshold and p_call >= p_put:
                ml_signal = "CALL"
                ml_conf   = p_call
            elif p_put >= ml_threshold and p_put > p_call:
                ml_signal = "PUT"
                ml_conf   = p_put
            else:
                ml_signal = "NONE"
                ml_conf   = max(p_call, p_put)
        else:
            # Direction oracle: always output best direction (no threshold)
            ml_signal = "CALL" if p_call > p_put else "PUT"
            ml_conf   = max(p_call, p_put)

        results.append({
            "date":      date,
            "ml_signal": ml_signal,
            "ml_p_call": round(p_call, 4),
            "ml_p_put":  round(p_put,  4),
            "ml_conf":   round(ml_conf, 4),
            "ml_trained": True,
        })

    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def generate_ml_signals(mode="direction", ml_threshold=0.55):
    """
    Full pipeline: load → indicators → labels → walk-forward → save signals_ml.csv.

    mode="direction" : ML direction oracle — CALL or PUT on every eligible day.
                       Trade count ≥ rule-based. WR improves via better direction.
    mode="filter"    : confidence gate — fewer trades, higher WR.
    """
    print("Loading data...")
    raw = load_all_data()
    print(f"  Master dataset: {len(raw)} days  "
          f"({raw['date'].min().date()} to {raw['date'].max().date()})")

    print("Computing indicators and features...")
    df      = compute_features(raw)
    trading = df[df["date"].dt.weekday.isin([0, 1, 2, 3, 4])].copy().reset_index(drop=True)
    print(f"  Trading days (Mon/Tue/Thu/Fri): {len(trading)}")

    print("Computing directional labels...")
    labels_df = compute_labels(trading)
    dist      = labels_df["label"].value_counts()
    tot_lbl   = len(labels_df)
    print(f"  Labels: CALL={dist.get('CALL',0)} ({dist.get('CALL',0)/tot_lbl*100:.1f}%)  "
          f"PUT={dist.get('PUT',0)} ({dist.get('PUT',0)/tot_lbl*100:.1f}%)")

    # ── Feature matrix ────────────────────────────────────────────────────────
    X            = trading[FEATURE_COLS].values.astype(float)
    y_bin        = labels_df["label"].values
    dates        = trading["date"].values
    rule_signals = trading["rule_signal"].values

    # ── Walk-forward ──────────────────────────────────────────────────────────
    desc = ("direction oracle — all trades, ML picks direction"
            if mode == "direction"
            else f"filter mode — threshold={ml_threshold}, fewer trades, higher WR")
    print(f"Running walk-forward [{desc}]...")

    preds_df = run_walkforward(X, y_bin, dates, mode=mode,
                               ml_threshold=ml_threshold,
                               rule_signals=rule_signals)

    # ── Merge ─────────────────────────────────────────────────────────────────
    merged = trading.copy()
    merged = merged.merge(preds_df, on="date", how="left")
    merged = merged.merge(labels_df[["date","call_out","put_out","label"]],
                          on="date", how="left")

    # ── Event day override ────────────────────────────────────────────────────
    merged["event_day"] = merged["date"].apply(
        lambda d: (d.date() if hasattr(d, "date") else d) in EVENT_DATES)

    # Final signal: ML direction, except event days → NONE
    merged["signal"] = np.where(merged["event_day"], "NONE", merged["ml_signal"])

    # ── Tidy up output ────────────────────────────────────────────────────────
    merged["weekday"]   = merged["date"].dt.day_name()
    merged["date"]      = merged["date"].dt.date
    merged["score"]     = merged["rule_score"]
    merged["threshold"] = 1

    for col in ["bn_close","ema20","rsi14","trend5","vix_dir",
                "sp500_chg","nikkei_chg","spf_gap","bn_nf_div","hv20","bn_gap"]:
        if col in merged.columns:
            merged[col] = merged[col].round(2)

    out_cols = [
        "date", "weekday", "event_day",
        "bn_close", "ema20", "rsi14", "trend5", "vix_dir",
        "sp500_chg", "nikkei_chg", "spf_gap", "bn_nf_div", "hv20", "bn_gap",
        "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div",
        "rule_score", "rule_signal",
        "ml_signal", "ml_p_call", "ml_p_put", "ml_conf", "ml_trained",
        "call_out", "put_out", "label",
        "score", "signal", "threshold",
    ]
    out = merged[[c for c in out_cols if c in merged.columns]]
    out.to_csv(f"{DATA_DIR}/signals_ml.csv", index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    traded  = out[out["signal"].isin(["CALL","PUT"])]
    n_call  = (traded["signal"] == "CALL").sum()
    n_put   = (traded["signal"] == "PUT").sum()
    n_event = int(out["event_day"].sum())
    n_none  = int((out["signal"] == "NONE").sum())
    total   = len(out)

    # Compare vs rule-based
    rule_traded = out[out["rule_signal"].isin(["CALL","PUT"])]
    n_agree  = (traded["signal"] == traded["rule_signal"]).sum() if len(traded) > 0 else 0
    n_flip   = len(traded) - n_agree
    warmup   = int((~out["ml_trained"]).sum())

    print(f"\n{'='*62}")
    print(f"  ML ENGINE  [{mode.upper()} mode]")
    print(f"{'='*62}")
    print(f"  Total trading days       : {total}")
    print(f"  CALL signals             : {n_call}  ({n_call/total*100:.1f}%)")
    print(f"  PUT  signals             : {n_put}  ({n_put/total*100:.1f}%)")
    print(f"  NONE (event days)        : {n_event}")
    if mode == "filter":
        print(f"  NONE (low confidence)    : {n_none - n_event}")
    print(f"  Warmup days (rule-based fallback): {warmup}")
    print(f"{'─'*62}")
    print(f"  vs rule-based ({len(rule_traded)} trades):")
    print(f"  ML agrees with rule      : {n_agree}  ({n_agree/len(traded)*100:.1f}% of ML trades)" if len(traded) > 0 else "  ML agrees with rule      : N/A (0 trades)")
    print(f"  ML flipped direction     : {n_flip}  (rule said X, ML says opposite)")
    print(f"{'─'*62}")
    for day in ["Monday","Tuesday","Thursday","Friday"]:
        d = traded[traded["weekday"] == day]
        if len(d) == 0:
            continue
        dc = (d["signal"] == "CALL").sum()
        dp = (d["signal"] == "PUT").sum()
        print(f"  {day:<10}: {len(d):>3} trades | CALL {dc} | PUT {dp}")
    print(f"{'='*62}")
    print(f"\nSaved → {DATA_DIR}/signals_ml.csv")
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  ANALYSIS MODE
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis():
    """Feature importance, directional accuracy, and signal-vs-label cross-tab."""
    print("Step 1/3: Loading data and computing labels...")
    raw     = load_all_data()
    df      = compute_features(raw)
    trading = df[df["date"].dt.weekday.isin([0, 1, 2, 3, 4])].copy().reset_index(drop=True)

    labels_df    = compute_labels(trading)
    X            = trading[FEATURE_COLS].values.astype(float)
    y_bin        = labels_df["label"].values
    dates        = trading["date"].values
    rule_signals = trading["rule_signal"].values

    print("Step 2/3: Walk-forward (direction mode) — ~2 min...")
    preds_df = run_walkforward(X, y_bin, dates, mode="direction",
                               rule_signals=rule_signals)
    print("  Walk-forward done.")

    # ── Directional accuracy (trained days only) ──────────────────────────────
    trained = preds_df[preds_df["ml_trained"]].copy()
    idx     = trained.index.tolist()
    y_true  = y_bin[idx]
    y_pred  = trained["ml_signal"].values

    correct = (y_true == y_pred).sum()
    total   = len(y_true)
    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD DIRECTIONAL ACCURACY  (trained days only)")
    print(f"{'='*60}")
    print(f"  Correct direction: {correct} / {total}  ({correct/total*100:.1f}%)")
    print(f"  (baseline: 50% = random, rule-based: ~55-60% estimated)")
    print()
    print(classification_report(y_true, y_pred, target_names=["CALL","PUT"],
                                 labels=["CALL","PUT"], zero_division=0))

    cm = confusion_matrix(y_true, y_pred, labels=["CALL","PUT"])
    cm_df = pd.DataFrame(cm, index=["Act:CALL","Act:PUT"],
                             columns=["Pred:CALL","Pred:PUT"])
    print("  Confusion matrix (rows=actual, cols=predicted):")
    print(cm_df.to_string())

    # ── Feature importance ────────────────────────────────────────────────────
    print(f"\nStep 3/3: Training full model for feature importance...")
    print(f"\n{'='*60}")
    print(f"  FEATURE IMPORTANCE (full-data RF fit)")
    print(f"{'='*60}")
    LABEL_MAP = {"CALL": 1, "PUT": 0}
    y_int = np.array([LABEL_MAP[l] for l in y_bin])
    gc.collect()   # free walk-forward model objects before full-data fit
    full_model = RandomForestClassifier(n_estimators=60, max_depth=6,
                                         min_samples_leaf=10, max_features="sqrt",
                                         class_weight="balanced", random_state=42,
                                         n_jobs=1)
    full_model.fit(X, y_int)
    imps = pd.Series(full_model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)

    print(f"  {'Feature':<20} {'Importance':>10}  Bar")
    print(f"  {'─'*50}")
    for feat, imp in imps.items():
        bar = "█" * int(imp * 300)
        print(f"  {feat:<20} {imp:>10.4f}  {bar}")

    # ── Comparison: rule vs ML direction ─────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  ML vs RULE-BASED DIRECTION COMPARISON")
    print(f"{'='*60}")
    agree_ml   = (y_true == y_pred).sum()
    agree_rule = (y_true == rule_signals[idx]).sum() if len(idx) > 0 else 0
    print(f"  ML correct direction  : {correct}/{total} = {correct/total*100:.1f}%")
    print(f"  Rule correct direction: {agree_rule}/{total} = {agree_rule/total*100:.1f}%")
    print(f"  ML improvement        : +{(correct-agree_rule)/total*100:.1f}pp")

    # On days where rule and ML disagree — who was right?
    preds_merged = preds_df.copy()
    preds_merged["true_label"]   = pd.Series(y_bin)
    preds_merged["rule_signal"]  = pd.Series(rule_signals)
    trained_full = preds_merged[preds_merged["ml_trained"]].copy()
    disagree = trained_full[trained_full["ml_signal"] != trained_full["rule_signal"]]
    if len(disagree) > 0:
        ml_right_on_flip   = (disagree["ml_signal"]   == disagree["true_label"]).sum()
        rule_right_on_flip = (disagree["rule_signal"]  == disagree["true_label"]).sum()
        print(f"\n  On days where ML and rule DISAGREE ({len(disagree)} days):")
        print(f"  ML was right  : {ml_right_on_flip} ({ml_right_on_flip/len(disagree)*100:.1f}%)")
        print(f"  Rule was right: {rule_right_on_flip} ({rule_right_on_flip/len(disagree)*100:.1f}%)")
        print(f"  → ML gains WR by flipping these {len(disagree)} trades correctly")
    print(f"{'='*60}")


# ─────────────────────────────────────────────────────────────────────────────
#  CHAMPION MODEL — helpers for fast morning prediction
# ─────────────────────────────────────────────────────────────────────────────

def load_champion():
    """
    Load saved champion model from models/champion.pkl.
    Returns (model, meta_dict) or (None, None) if not found / too old.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    if not (os.path.exists(CHAMPION_PKL) and os.path.exists(CHAMPION_META)):
        return None, None

    try:
        import joblib
        with open(CHAMPION_META) as f:
            meta = _json.load(f)

        # Check freshness
        trained_at = _dt.fromisoformat(meta["trained_at"])
        if trained_at.tzinfo is None:
            trained_at = trained_at.replace(tzinfo=_tz(_td(hours=5, minutes=30)))
        age_days = (_dt.now(trained_at.tzinfo) - trained_at).days
        if age_days > CHAMPION_MAX_AGE_DAYS:
            print(f"  Champion model is {age_days} days old (max {CHAMPION_MAX_AGE_DAYS}) — will retrain.")
            return None, None

        model = joblib.load(CHAMPION_PKL)
        return model, meta

    except Exception as e:
        print(f"  Could not load champion model: {e}")
        return None, None


def get_today_features(feature_cols):
    """
    Build a single-row feature array for today using the specified feature_cols.
    Handles both base (21) and extended feature sets from model_evolver.
    """
    # Load base data + compute base features
    raw = load_all_data()
    df  = compute_features(raw)

    # Try to extend with new data sources if model was trained with them
    extended_cols = set(feature_cols) - set(FEATURE_COLS)
    if extended_cols:
        try:
            from model_evolver import load_extended_data, compute_extended_features
            df = compute_extended_features(load_extended_data())
        except Exception:
            # Extended data not available — fill new cols with 0
            for col in extended_cols:
                if col not in df.columns:
                    df[col] = 0.0

    today_ts   = pd.Timestamp(pd.Timestamp.now().date())
    today_rows = df[df["date"] == today_ts]
    if today_rows.empty:
        return None

    # Fill any missing feature columns with 0
    for col in feature_cols:
        if col not in today_rows.columns:
            today_rows = today_rows.copy()
            today_rows[col] = 0.0

    return today_rows[feature_cols].fillna(0).values.astype(float)


# ─────────────────────────────────────────────────────────────────────────────
#  PREDICT TODAY  (fast — single model, ~10 sec — used by auto_trader.py)
# ─────────────────────────────────────────────────────────────────────────────

def predict_today():
    """
    Predict today's ML direction.

    Fast path (preferred): loads saved champion model from models/champion.pkl
    in <5 sec — no retraining. Champion is updated nightly by model_evolver.py.

    Slow fallback: trains a fresh RandomForest on all history (~30 sec) when
    champion is missing or older than CHAMPION_MAX_AGE_DAYS.

    Appends/overwrites today's row in signals_ml.csv.
    Called by auto_trader.py each morning.
    """
    from datetime import date as _date
    import os as _os

    today_dt  = _date.today()
    today_ts  = pd.Timestamp(today_dt)
    signals_ml_path = f"{DATA_DIR}/signals_ml.csv"

    print(f"ML predict-today: {today_dt} ...")

    # ── Determine rule-based signal for today (always needed for fallback) ────
    raw     = load_all_data()
    df      = compute_features(raw)
    trading = df[df["date"].dt.weekday.isin([0, 1, 2, 3, 4])].copy().reset_index(drop=True)

    today_rows = trading[trading["date"] == today_ts]
    if today_rows.empty:
        print(f"  {today_dt} is not a Mon/Tue/Thu/Fri — no ML prediction needed.")
        return

    today_idx  = today_rows.index[0]
    rule_row   = trading.iloc[today_idx]
    rule_sig   = rule_row.get("rule_signal", "NONE")
    rule_score = rule_row.get("rule_score", 0)

    ml_trained = False

    # ── Fast path: load champion model ────────────────────────────────────────
    champion_model, champion_meta = load_champion()

    if champion_model is not None:
        feature_cols = champion_meta["feature_cols"]
        X_today = get_today_features(feature_cols)

        if X_today is not None and len(X_today) > 0:
            proba   = champion_model.predict_proba(X_today)[0]
            classes = list(champion_model.classes_)
            p_call  = proba[classes.index(1)] if 1 in classes else 0.5
            p_put   = proba[classes.index(0)] if 0 in classes else 0.5
            ml_signal  = "CALL" if p_call >= p_put else "PUT"
            ml_conf    = max(p_call, p_put)
            ml_trained = True
            mtype_name = {"rf": "RandomForest", "xgb": "XGBoost",
                          "lgb": "LightGBM"}.get(champion_meta["model_type"], "Champion")
            print(f"  Loaded champion: {mtype_name}  "
                  f"(trained {champion_meta.get('trained_at','?')[:10]})")
            print(f"  P(CALL)={p_call:.3f}  P(PUT)={p_put:.3f}  "
                  f"→ {ml_signal}  (conf {ml_conf:.1%})")
        else:
            print("  Champion loaded but today's features unavailable — falling back.")
            champion_model = None

    # ── Slow fallback: retrain RF from scratch ────────────────────────────────
    if champion_model is None:
        print("  No champion model — retraining RandomForest from scratch...")
        X_all     = trading[FEATURE_COLS].values.astype(float)
        labels_df = compute_labels(trading)
        y_all     = np.array([1 if l == "CALL" else 0 for l in labels_df["label"].values])

        X_train = X_all[:today_idx]
        y_train = y_all[:today_idx]
        X_today_base = X_all[today_idx : today_idx + 1]

        if today_idx < MIN_TRAIN or len(np.unique(y_train)) < 2:
            print(f"  Not enough history ({today_idx} days) — using rule-based fallback.")
            ml_signal  = rule_sig if rule_sig in ("CALL", "PUT") else "CALL"
            ml_conf    = 0.5
            ml_trained = False
            p_call = p_put = 0.5
        else:
            model = RandomForestClassifier(
                n_estimators=60, max_depth=6, min_samples_leaf=10,
                max_features="sqrt", class_weight="balanced",
                random_state=42, n_jobs=1,
            )
            model.fit(X_train, y_train)
            proba   = model.predict_proba(X_today_base)[0]
            classes = list(model.classes_)
            p_call  = proba[classes.index(1)] if 1 in classes else 0.0
            p_put   = proba[classes.index(0)] if 0 in classes else 0.0
            ml_signal  = "CALL" if p_call >= p_put else "PUT"
            ml_conf    = max(p_call, p_put)
            ml_trained = True
            print(f"  Retrained RF on {today_idx} days.")
            print(f"  P(CALL)={p_call:.3f}  P(PUT)={p_put:.3f}  "
                  f"→ {ml_signal}  (conf {ml_conf:.1%})")

    # Event day override
    is_event = today_dt in EVENT_DATES
    final_signal = "NONE" if is_event else ml_signal
    if is_event:
        print(f"  Event day override → NONE")

    # Build the today row (keep same column shape as signals_ml.csv)
    today_row = {
        "date":        today_dt,
        "weekday":     today_ts.day_name(),
        "event_day":   is_event,
        "score":       int(rule_score),
        "threshold":   1,
        "rule_signal": rule_sig,
        "rule_score":  int(rule_score),
        "ml_signal":   ml_signal,
        "ml_p_call":   round(p_call if ml_trained else 0.5, 4),
        "ml_p_put":    round(p_put  if ml_trained else 0.5, 4),
        "ml_conf":     round(ml_conf, 4),
        "ml_trained":  ml_trained,
        "signal":      final_signal,
    }

    # Upsert into signals_ml.csv
    if _os.path.exists(signals_ml_path):
        existing = pd.read_csv(signals_ml_path, parse_dates=["date"])
        existing = existing[existing["date"].dt.date != today_dt]  # drop old today row
        new_row  = pd.DataFrame([today_row])
        out      = pd.concat([existing, new_row], ignore_index=True)
    else:
        # signals_ml.csv missing — create minimal version from signals.csv + today
        print(f"  signals_ml.csv not found — creating from signals.csv + today.")
        base = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        new_row = pd.DataFrame([today_row])
        out = pd.concat([base, new_row], ignore_index=True)

    out["date"] = pd.to_datetime(out["date"]).dt.date
    out = out.sort_values("date").reset_index(drop=True)
    out.to_csv(signals_ml_path, index=False)
    print(f"  Saved → {signals_ml_path}  (today: {final_signal})")
    return today_row


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if MODE == "analyze":
        run_analysis()
        return

    if MODE == "predict_today":
        predict_today()
        return

    generate_ml_signals(mode=MODE, ml_threshold=ML_THRESHOLD)

    if MODE == "direction":
        print(f"\nRun backtest:")
        print(f"  python3 backtest_engine.py --ml")
        print(f"\nFor analysis (feature importance + accuracy):")
        print(f"  python3 ml_engine.py --analyze")
        print(f"\nFor fewer-trades / higher-WR filter mode:")
        print(f"  python3 ml_engine.py --filter 0.60")
        print(f"  python3 backtest_engine.py --ml")


if __name__ == "__main__":
    main()
