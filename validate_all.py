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
          f"found: {new_in}" if new_in else "not selected by RF importance this run (normal)",
          warn_only=True)
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
#  10. LIVE TRADES — journal health
# ─────────────────────────────────────────────────────────────────────────────
section("10. LIVE TRADES  (data/live_trades.csv)")

try:
    lt = pd.read_csv("data/live_trades.csv", parse_dates=["date"])
    n_rows  = len(lt)
    n_labeled = lt["oracle_correct"].notna().sum() if "oracle_correct" in lt.columns else 0
    last_date = str(lt["date"].iloc[-1])[:10]
    check("live_trades.csv exists", True, f"{n_rows} rows  |  {n_labeled} labeled")
    check("live_trades.csv: ≥ 3 rows", n_rows >= 3, str(n_rows))
    check("live_trades.csv: recent row ≤ 7 days old",
          (TODAY - pd.to_datetime(last_date).date()).days <= 7, last_date)
    # Check no duplicate dates
    dupes = lt["date"].duplicated().sum()
    check("live_trades.csv: no duplicate dates", dupes == 0,
          "OK" if dupes == 0 else f"{dupes} duplicate rows")
    # Live feedback threshold: 5 labeled rows activates 10x weight
    check("live_trades.csv: ≥ 5 labeled (activates 10x weight)",
          n_labeled >= 5,
          f"{n_labeled}/5 — {'ACTIVE' if n_labeled >= 5 else 'not yet active'}",
          warn_only=n_labeled < 5)
    if "oracle_correct" in lt.columns:
        recent = lt.tail(10)
        acc = recent["oracle_correct"].mean()
        check("Recent 10-trade accuracy", True, f"{acc:.0%}")
except FileNotFoundError:
    check("data/live_trades.csv", False, "FILE MISSING — run backfill_live_trades.py")
except Exception as e:
    check("live_trades.csv", False, str(e)[:80])


# ─────────────────────────────────────────────────────────────────────────────
#  11. MIDDAY & PAPER MODEL STATE
# ─────────────────────────────────────────────────────────────────────────────
section("11. MIDDAY CHECKPOINTS & PAPER MODEL")

# midday_checkpoints.csv
try:
    mc = pd.read_csv("data/midday_checkpoints.csv", parse_dates=["date"])
    n_rev = (mc["reversal_detected"].astype(str).str.lower() == "true").sum()
    last_mc = str(mc["date"].iloc[-1])[:10]
    check("data/midday_checkpoints.csv", True,
          f"{len(mc)} rows  |  {n_rev} reversals  |  last={last_mc}")
except FileNotFoundError:
    check("data/midday_checkpoints.csv", True,
          "not yet created — will be written at next 11 AM cron", warn_only=False)
except Exception as e:
    check("data/midday_checkpoints.csv", False, str(e)[:60])

# paper_performance.csv
try:
    pp = pd.read_csv("data/paper_performance.csv")
    n = len(pp)
    if n > 0:
        streak = pp.get("streak", pd.Series([0])).iloc[-1] if "streak" in pp.columns else "?"
        adv    = pp.get("combined_advantage", pd.Series([0])).iloc[-1] if "combined_advantage" in pp.columns else "?"
        check("data/paper_performance.csv", True,
              f"{n} rows  |  streak={streak}/3  |  last_advantage={adv}")
    else:
        check("data/paper_performance.csv", True, "empty (fresh start after promotion)")
except FileNotFoundError:
    check("data/paper_performance.csv", True,
          "not yet created — written on first autoloop run", warn_only=False)
except Exception as e:
    check("data/paper_performance.csv", False, str(e)[:60])

# paper model diff vs live
try:
    import filecmp
    same = filecmp.cmp("ml_engine.py", "ml_engine_paper.py", shallow=False)
    check("paper ≠ live model (diverged)", not same,
          "SAME FILE — paper will diverge on next autoloop run" if same else "diverged OK",
          warn_only=same)
except Exception as e:
    check("paper vs live model", False, str(e)[:60])

# trade_journal endpoint sanity (grep check — no API call)
try:
    with open("trade_journal.py") as f:
        tj_src = f.read()
    has_correct = "/v2/trades" in tj_src
    has_wrong   = "/v2/tradebook" in tj_src
    check("trade_journal.py uses /v2/trades",
          has_correct and not has_wrong,
          "OK" if (has_correct and not has_wrong) else
          "/v2/tradebook still present — endpoint fix not applied!")
except Exception as e:
    check("trade_journal.py endpoint", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  12. CRON — jobs installed
# ─────────────────────────────────────────────────────────────────────────────
section("12. CRON JOBS")

try:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    cron_txt = r.stdout
    jobs = {
        "auto_trader.py":        "0  4   * * 1-5",
        "model_evolver.py":      "30 17  * * 1-5",
        "autoloop_bn.py":        "30 18  * * 1-5",
        "midday_conviction.py":  "30 5   * * 1-5",
        "trade_journal.py":      "0  10  * * 1-5",
        "health_ping.py":        "35 3   * * 1-5",
        "renew_token.py":        "*/5 *  * * *",
    }
    for script, expected_time in jobs.items():
        present = script in cron_txt
        check(f"cron: {script}", present,
              "installed" if present else "MISSING from crontab")
except FileNotFoundError:
    check("crontab", False, "crontab command not found — not on the trading VM?")
except Exception as e:
    check("cron jobs", False, str(e)[:60])


# ─────────────────────────────────────────────────────────────────────────────
#  13. GIT AUTH — SSH key, remote, push capability
# ─────────────────────────────────────────────────────────────────────────────
section("13. GIT / AUTORESEARCH PUSH")

# SSH key present
ssh_key = os.path.expanduser("~/.ssh/id_ed25519")
check("SSH key exists (~/.ssh/id_ed25519)", os.path.exists(ssh_key),
      "present" if os.path.exists(ssh_key) else "MISSING — autoloop can't push to GitHub")

# Remote uses SSH (not HTTPS, which needs password)
try:
    r = subprocess.run(["git", "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    remote = r.stdout.strip()
    uses_ssh = remote.startswith("git@")
    check("git remote uses SSH (not HTTPS)", uses_ssh,
          remote if uses_ssh else f"HTTPS remote — push will hang: {remote}")
except Exception as e:
    check("git remote", False, str(e)[:60])

# SSH auth to GitHub (non-blocking, 5s timeout)
try:
    r = subprocess.run(
        ["ssh", "-T", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=5", "git@github.com"],
        capture_output=True, text=True, timeout=10
    )
    auth_ok = "successfully authenticated" in (r.stdout + r.stderr).lower()
    check("SSH auth to github.com works", auth_ok,
          "OK" if auth_ok else (r.stdout + r.stderr).strip()[:80])
except subprocess.TimeoutExpired:
    check("SSH auth to github.com", False, "TIMED OUT — network issue?")
except Exception as e:
    check("SSH auth to github.com", False, str(e)[:60])

# Current branch
try:
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True)
    branch = r.stdout.strip()
    check("git branch (not detached HEAD)", branch != "HEAD",
          branch if branch != "HEAD" else "DETACHED HEAD — autoloop push will fail")
except Exception as e:
    check("git branch", False, str(e)[:60])

# Uncommitted changes to key files
try:
    r = subprocess.run(["git", "status", "--short"],
                       capture_output=True, text=True)
    # Only flag M (modified) or D (deleted) — not ?? (untracked, can't affect live code)
    dirty = [l for l in r.stdout.splitlines()
             if not l.startswith("??") and
             any(f in l for f in ["ml_engine", "auto_trader", "model_evolver",
                                  "autoloop_bn", "trade_journal", "midday_conviction"])]
    check("No uncommitted changes to key files", not dirty,
          "clean" if not dirty else f"uncommitted: {', '.join(dirty)}")
except Exception as e:
    check("git status", False, str(e)[:60])


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
