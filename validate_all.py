#!/usr/bin/env python3
"""
validate_all.py — End-to-end system health check
Run on VM to confirm all components are working after a pull.

  python3 validate_all.py
"""

import os, sys, json, subprocess
from datetime import date, datetime, timedelta

import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

DATA_DIR   = "data"
MODELS_DIR = "models"
TODAY      = date.today()

_pass, _fail, _warn = "✅", "❌", "⚠️ "
_results = []

def check(label, ok, detail="", warn_only=False):
    icon = _pass if ok else (_warn if warn_only else _fail)
    print(f"  {icon}  {label:<46}  {detail}")
    _results.append((label, ok, warn_only))

def section(title):
    print(f"\n{'━'*64}")
    print(f"  {title}")
    print(f"{'━'*64}")


# ─────────────────────────────────────────────────────────────────────────────
#  1. DATA FILES — freshness, row count, no corrupt values
# ─────────────────────────────────────────────────────────────────────────────
section("1. DATA FILES")

CORE_CSVS = [
    ("data/banknifty.csv",     "close",  3, 1500),
    ("data/nifty50.csv",       "close",  3, 1500),
    ("data/india_vix.csv",     "close",  3, 1500),
    ("data/sp500.csv",         "close",  4, 1500),
    ("data/nikkei.csv",        "close",  4, 1500),
    ("data/sp500_futures.csv", "close",  4, 1500),
    ("data/signals.csv",       "signal", 3,  500),
    ("data/signals_ml.csv",    "signal", 3,  500),
]

for path, col, max_stale, min_rows in CORE_CSVS:
    try:
        df = pd.read_csv(path, parse_dates=["date"])
        last   = pd.to_datetime(df["date"].iloc[-1]).date()
        stale  = (TODAY - last).days
        detail = f"last={last}  ({stale}d ago)  rows={len(df)}"
        check(path, stale <= max_stale and len(df) >= min_rows, detail)
    except FileNotFoundError:
        check(path, False, "FILE MISSING")
    except Exception as e:
        check(path, False, str(e)[:60])

# VIX quality
try:
    vix = pd.read_csv("data/india_vix.csv")
    bad = vix[vix["close"] > 85]
    last_vix = float(vix["close"].iloc[-1])
    check("VIX: no outliers > 85",
          bad.empty, f"clean — last value: {last_vix:.1f}" if bad.empty else
                     f"{len(bad)} bad rows: {bad['close'].tolist()}")
    check("VIX: last value in range [8, 85]",
          8 <= last_vix <= 85, f"{last_vix:.1f}")
except Exception as e:
    check("VIX quality", False, str(e)[:60])

# Optional macro files (warn only if missing)
for path in ["data/crude.csv", "data/dxy.csv", "data/us10y.csv",
             "data/usdinr.csv", "data/pcr.csv", "data/fii_dii.csv",
             "data/options_atm_daily.csv"]:
    ex = os.path.exists(path)
    check(path, ex, "present" if ex else "missing", warn_only=not ex)


# ─────────────────────────────────────────────────────────────────────────────
#  2. ML FEATURES — all FEATURE_COLS computed, new features present
# ─────────────────────────────────────────────────────────────────────────────
section("2. ML FEATURES")

try:
    sys.path.insert(0, ".")
    from ml_engine import load_all_data, compute_features, FEATURE_COLS

    df_raw  = load_all_data()
    df_feat = compute_features(df_raw)

    missing = [c for c in FEATURE_COLS if c not in df_feat.columns]
    check(f"All {len(FEATURE_COLS)} FEATURE_COLS present",
          not missing,
          "OK" if not missing else f"MISSING: {missing}")

    # New features added in this session
    for feat in ["adx14", "straddle_expansion", "rule_score_lag1",
                 "prev_range_pct", "prev_body_pct", "bn_ret60", "bn_dist_high52"]:
        if feat in df_feat.columns:
            vals    = df_feat[feat].dropna()
            last_v  = vals.iloc[-1] if len(vals) else float("nan")
            check(f"  feature: {feat}",
                  len(vals) > 100, f"non-null={len(vals)}  last={last_v:.4f}")
        else:
            check(f"  feature: {feat}", False, "NOT in columns")

    # VIX clip in effect: vix_level (= shifted vix_close) must not exceed 85
    if "vix_level" in df_feat.columns:
        max_vl = df_feat["vix_level"].max()
        check("VIX clip: max vix_level ≤ 85", max_vl <= 85, f"max={max_vl:.1f}")

    # Last row NaN budget
    last_row  = df_feat.iloc[-1][FEATURE_COLS]
    nan_count = last_row.isna().sum()
    check("Last row: NaN count ≤ 5", nan_count <= 5, f"{nan_count} NaN cols")

    # Training row count
    check("Training rows ≥ 1400", len(df_feat) >= 1400, f"{len(df_feat)} rows")

except Exception as e:
    check("compute_features()", False, str(e)[:80])


# ─────────────────────────────────────────────────────────────────────────────
#  3. MODELS — existence, freshness, feature list alignment
# ─────────────────────────────────────────────────────────────────────────────
section("3. MODELS")

for path in ["models/champion.pkl", "models/champion_meta.json",
             "models/ensemble/xgb.pkl", "models/ensemble/lgb.pkl",
             "models/ensemble/cat.pkl", "models/ensemble/rf.pkl",
             "models/ensemble_meta.json"]:
    try:
        age_h = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600
        check(path, age_h < 72, f"modified {age_h:.0f}h ago")
    except FileNotFoundError:
        check(path, False, "FILE MISSING")

try:
    with open("models/champion_meta.json") as f:
        meta = json.load(f)
    mtype  = meta.get("model_type", "?")
    n_feat = meta.get("n_features", 0)
    feat_l = meta.get("feature_list", [])
    check("Champion model type",        True, mtype)
    check("Champion feature count ≥ 20", n_feat >= 20, str(n_feat))
    new_in = [f for f in ["adx14", "straddle_expansion", "rule_score_lag1"] if f in feat_l]
    check("New features in champion",  bool(new_in),
          f"found: {new_in}" if new_in else "adx14/straddle_expansion/rule_score_lag1 absent")
except Exception as e:
    check("champion_meta.json", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  4. CONFIG + RUNTIME FILES
# ─────────────────────────────────────────────────────────────────────────────
section("4. CONFIG & RUNTIME FILES")

# VIX threshold
try:
    with open("data/vix_threshold.json") as f:
        vt = json.load(f)
    thr = vt.get("vix_min_trade")
    cov = vt.get("coverage_pct")
    check("data/vix_threshold.json",
          thr is not None and 8 <= thr <= 20,
          f"vix_min_trade={thr}  coverage={cov}%")
except FileNotFoundError:
    check("data/vix_threshold.json", False,
          "run: python3 analyze_confidence.py --write-threshold")
except Exception as e:
    check("data/vix_threshold.json", False, str(e)[:60])

# News sentiment (within last 4 days — weekend tolerance)
try:
    with open("data/news_sentiment.json") as f:
        ns = json.load(f)
    ns_date = ns.get("date", "")
    fresh   = ns_date >= str(TODAY - timedelta(days=4))
    check("data/news_sentiment.json",
          fresh,
          f"{ns_date}  {ns['direction']} ({ns['confidence']})  {ns['n_headlines']} headlines")
except FileNotFoundError:
    check("data/news_sentiment.json", True,
          "absent — will be written at 9:15 AM cron", warn_only=False)
except Exception as e:
    check("data/news_sentiment.json", False, str(e)[:60])

# today_trade.json (only relevant on trade days — skip weekend)
if TODAY.weekday() < 5:
    try:
        with open("data/today_trade.json") as f:
            tt = json.load(f)
        tt_date = tt.get("date", "?")
        same_day = tt_date == str(TODAY)
        check("data/today_trade.json",
              True,
              f"date={tt_date}  signal={tt.get('signal','?')}"
              + ("" if same_day else "  (from previous trading day)"),
              warn_only=not same_day)
    except FileNotFoundError:
        check("data/today_trade.json", True,
              "absent — OK before 9:30 AM or on non-trade days")
    except Exception as e:
        check("data/today_trade.json", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  5. ENVIRONMENT — .env keys present
# ─────────────────────────────────────────────────────────────────────────────
section("5. ENVIRONMENT")

for key in ["DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID",
            "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    val = os.getenv(key, "")
    check(f"  {key}", bool(val), val[:8] + "..." if val else "NOT SET")


# ─────────────────────────────────────────────────────────────────────────────
#  6. LIVE PREDICTION — ml_engine --predict-today
# ─────────────────────────────────────────────────────────────────────────────
section("6. LIVE PREDICTION  (ml_engine --predict-today)")

try:
    r = subprocess.run(
        [sys.executable, "ml_engine.py", "--predict-today"],
        capture_output=True, text=True, timeout=60
    )
    output = r.stdout + r.stderr
    ok = r.returncode == 0 and any(x in output for x in ["CALL", "PUT", "NONE", "signal"])
    # Print last 8 lines (the prediction summary)
    lines = [l for l in output.strip().splitlines() if l.strip()]
    for line in lines[-8:]:
        print(f"    {line}")
    check("ml_engine --predict-today", ok,
          f"exit={r.returncode}")
except subprocess.TimeoutExpired:
    check("ml_engine --predict-today", False, "TIMED OUT (>60s)")
except Exception as e:
    check("ml_engine --predict-today", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  7. SIGNAL ENGINE — last signal in CSV
# ─────────────────────────────────────────────────────────────────────────────
section("7. SIGNAL ENGINE")

try:
    sig = pd.read_csv("data/signals_ml.csv", parse_dates=["date"])
    last = sig.iloc[-1]
    sig_date   = str(last["date"])[:10]
    sig_signal = last.get("signal", "?")
    sig_score  = last.get("score", "?")
    stale = (TODAY - pd.to_datetime(sig_date).date()).days
    check("signals_ml.csv last row", stale <= 5,
          f"{sig_date}  signal={sig_signal}  score={sig_score}")

    exp_cols = ["date", "signal", "score", "s_ema20", "s_trend5", "s_vix"]
    missing  = [c for c in exp_cols if c not in sig.columns]
    check("signals_ml.csv has expected columns", not missing,
          "OK" if not missing else f"missing: {missing}")
except Exception as e:
    check("signals_ml.csv", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  8. AUTO TRADER — dry run (no API calls, no real orders)
# ─────────────────────────────────────────────────────────────────────────────
section("8. AUTO TRADER  (--dry-run  |  no real orders placed)")

try:
    r = subprocess.run(
        [sys.executable, "auto_trader.py", "--dry-run"],
        capture_output=True, text=True, timeout=120
    )
    output = r.stdout + r.stderr
    ok = r.returncode == 0
    # Show last 15 lines (the decision summary)
    lines = [l for l in output.strip().splitlines() if l.strip()]
    for line in lines[-15:]:
        print(f"    {line}")
    check("auto_trader --dry-run", ok, f"exit={r.returncode}")
except subprocess.TimeoutExpired:
    check("auto_trader --dry-run", False, "TIMED OUT (>120s)")
except Exception as e:
    check("auto_trader --dry-run", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  9. MORNING BRIEF — logic check (no Claude API call, just VIX sanity)
# ─────────────────────────────────────────────────────────────────────────────
section("9. MORNING BRIEF  (import + VIX sanity, no API call)")

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("mb", "morning_brief.py")
    mb   = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mb)

    ctx = mb._build_live_context()
    ok  = bool(ctx) and ctx != "Market context unavailable."
    # Flag if VIX outlier sneaked through
    bad_vix = "90." in ctx or "VIX: 6" in ctx or "VIX: 7" in ctx or "VIX: 8" in ctx
    check("morning_brief: live context built", ok, ctx[:120])
    check("morning_brief: VIX looks sane",
          not bad_vix,
          "OK" if not bad_vix else f"Suspicious VIX in context: {ctx}")
except Exception as e:
    check("morning_brief import", False, str(e)[:80])


# ─────────────────────────────────────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
section("SUMMARY")

total  = len(_results)
passed = sum(1 for _, ok, _ in _results if ok)
warns  = [(n, w) for n, ok, w in _results if not ok and w]
failed = [(n, w) for n, ok, w in _results if not ok and not w]

print(f"\n  {passed}/{total} checks passed", end="")
if warns:
    print(f"   {len(warns)} warnings", end="")
if failed:
    print(f"   {len(failed)} FAILURES", end="")
print()

if failed:
    print(f"\n  {_fail} FAILURES:")
    for name, _ in failed:
        print(f"      {name}")
if warns:
    print(f"\n  {_warn} Warnings:")
    for name, _ in warns:
        print(f"      {name}")

print()
sys.exit(0 if not failed else 1)
