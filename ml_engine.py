#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
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
    # Keep VIX open so we can compute vix_open_chg at 9:15 AM
    vix_cols = ["date","close"] + (["open"] if "open" in vix.columns else [])
    vix = vix[vix_cols].rename(columns={"open":"vix_open","close":"vix_close"})
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

    # ── Global macro: crude oil, dollar index, US 10Y yield (optional) ─────────
    # All three are fetched daily by data_fetcher.py. They are the primary
    # drivers of FII behaviour and banking-sector risk-off moves.
    for _col, _file in [("crude_close",  "crude.csv"),
                         ("dxy_close",    "dxy.csv"),
                         ("us10y_close",  "us10y.csv"),
                         ("usdinr_close", "usdinr.csv"),
                         # Bank sector ETF + top-5 BN constituents (yfinance NS tickers)
                         ("bankbees_close", "bankbees.csv"),
                         ("hdfc_close",     "hdfcbank.csv"),
                         ("icici_close",    "icicibank.csv"),
                         ("kotak_close",    "kotakbank.csv"),
                         ("sbi_close",      "sbin.csv"),
                         ("axis_close",     "axisbank.csv")]:
        _path = f"{DATA_DIR}/{_file}"
        if os.path.exists(_path):
            try:
                _tmp = pd.read_csv(_path, parse_dates=["date"])[["date", "close"]]
                _tmp = _tmp.rename(columns={"close": _col})
                df   = df.merge(_tmp, on="date", how="left")
                df[_col] = df[_col].ffill(limit=5)
            except Exception:
                df[_col] = np.nan
        else:
            df[_col] = np.nan

    # BANKBEES volume (for ETF flow z-score)
    _bb_path = f"{DATA_DIR}/bankbees.csv"
    if os.path.exists(_bb_path):
        try:
            _bb = pd.read_csv(_bb_path, parse_dates=["date"])
            if "volume" in _bb.columns:
                df = df.merge(_bb[["date","volume"]].rename(columns={"volume":"bankbees_vol"}),
                              on="date", how="left")
                df["bankbees_vol"] = df["bankbees_vol"].ffill(limit=5)
            else:
                df["bankbees_vol"] = np.nan
        except Exception:
            df["bankbees_vol"] = np.nan
    else:
        df["bankbees_vol"] = np.nan

    # BN 15-min intraday for ORB (9:15 candle) — optional
    _orb_path = f"{DATA_DIR}/banknifty_15m_orb.csv"
    _ORB_COLS = ["orb_high", "orb_low", "orb_close"]
    if os.path.exists(_orb_path):
        try:
            _orb = pd.read_csv(_orb_path, parse_dates=["date"])
            keep = ["date"] + [c for c in _ORB_COLS if c in _orb.columns]
            df = df.merge(_orb[keep], on="date", how="left")
            for _c in _ORB_COLS:
                if _c not in df.columns:
                    df[_c] = np.nan
        except Exception:
            for _c in _ORB_COLS:
                df[_c] = np.nan
    else:
        for _c in _ORB_COLS:
            df[_c] = np.nan

    # ── Real ATM option premiums (optional) ─────────────────────────────────
    # options_atm_daily.csv: date, call_premium, put_premium, max_pain_dist,
    # gex_positive, straddle (ATM open prices + chain signals written by auto_trader).
    # Used in compute_labels (better SL/TP simulation) and as features.
    opt_path = f"{DATA_DIR}/options_atm_daily.csv"
    _OPT_COLS = ["call_premium", "put_premium", "max_pain_dist",
                 "gex_positive", "straddle"]
    if os.path.exists(opt_path):
        try:
            opt_full = pd.read_csv(opt_path, parse_dates=["date"])
            keep     = ["date"] + [c for c in _OPT_COLS if c in opt_full.columns]
            df       = df.merge(opt_full[keep], on="date", how="left")
            for _c in _OPT_COLS:
                if _c not in df.columns:
                    df[_c] = np.nan
                df[_c] = df[_c].ffill(limit=2)
        except Exception:
            for _c in _OPT_COLS:
                df[_c] = np.nan
    else:
        for _c in _OPT_COLS:
            df[_c] = np.nan

    # ── IV skew (optional) — ATM + OTM implied volatilities ─────────────────
    iv_skew_path = f"{DATA_DIR}/options_iv_skew.csv"
    if os.path.exists(iv_skew_path):
        try:
            iv_skew = pd.read_csv(iv_skew_path, parse_dates=["date"])[
                ["date", "call_iv_atm", "put_iv_atm", "call_iv_otm", "put_iv_otm"]]
            df = df.merge(iv_skew, on="date", how="left")
            for _col in ["call_iv_atm", "put_iv_atm", "call_iv_otm", "put_iv_otm"]:
                df[_col] = df[_col].ffill(limit=2)
        except Exception:
            for _col in ["call_iv_atm", "put_iv_atm", "call_iv_otm", "put_iv_otm"]:
                df[_col] = np.nan
    else:
        for _col in ["call_iv_atm", "put_iv_atm", "call_iv_otm", "put_iv_otm"]:
            df[_col] = np.nan

    # ── OI surface (optional) — ATM±3 CE/PE open interest ───────────────────
    oi_path = f"{DATA_DIR}/options_oi_surface.csv"
    _OI_COLS = [f"{t}_oi_{s}" for t in ("ce", "pe")
                               for s in ("m3","m2","m1","atm","p1","p2","p3")]
    if os.path.exists(oi_path):
        try:
            oi_df = pd.read_csv(oi_path, parse_dates=["date"])
            keep  = ["date"] + [c for c in _OI_COLS if c in oi_df.columns]
            df    = df.merge(oi_df[keep], on="date", how="left")
            for _c in _OI_COLS:
                if _c not in df.columns:
                    df[_c] = np.nan
                df[_c] = df[_c].ffill(limit=2)
        except Exception:
            for _c in _OI_COLS:
                df[_c] = np.nan
    else:
        for _c in _OI_COLS:
            df[_c] = np.nan

    # ── FII net cash (optional) ───────────────────────────────────────────────
    fii_path = f"{DATA_DIR}/fii_dii.csv"
    if os.path.exists(fii_path):
        try:
            fii = pd.read_csv(fii_path, parse_dates=["date"])
            if "fii_net_cash" in fii.columns:
                df = df.merge(fii[["date","fii_net_cash"]], on="date", how="left")
                df["fii_net_cash"] = df["fii_net_cash"].ffill(limit=3)
            else:
                df["fii_net_cash"] = np.nan
        except Exception:
            df["fii_net_cash"] = np.nan
    else:
        df["fii_net_cash"] = np.nan

    # ── PCR (optional) — merge from pcr_live.csv + pcr.csv if available ──────
    # pcr.csv stores EOD values for day T. At 9:15 AM on day T only day T-1's
    # EOD PCR is known. Shift by 1 so training sees the same PCR the live system
    # sees (previous day's close). pcr_live.csv is already correct (fetched pre-open).
    for pcr_file in [f"{DATA_DIR}/pcr.csv", f"{DATA_DIR}/pcr_live.csv"]:
        if os.path.exists(pcr_file):
            try:
                pcr_df = pd.read_csv(pcr_file, parse_dates=["date"])[["date", "pcr"]]
                if pcr_file.endswith("pcr.csv"):
                    # Shift historical EOD values forward by 1 trading day
                    pcr_df = pcr_df.sort_values("date").copy()
                    pcr_df["date"] = pcr_df["date"].shift(-1)
                    pcr_df = pcr_df.dropna(subset=["date"])
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
    Compute 21-feature matrix. All features use yesterday's close (_c) so
    that training and live prediction see identical inputs — at 9:15 AM the
    current day's close is unknown; only the prior day's close is available.
    """
    d = df.copy()
    # Shift BN and NF close by 1: yesterday's close is what's known at 9:15 AM.
    # Every rolling/ewm/pct_change on price must operate on _c, not bn_close,
    # to avoid training on data that leaks the same-day close into the label.
    _c    = d["bn_close"].shift(1)   # yesterday's BN close
    _c_nf = d["nf_close"].shift(1)   # yesterday's NF close
    _vix  = d["vix_close"].shift(1).clip(8, 85)  # yesterday's India VIX; clip outliers from yfinance data errors
    _sp   = d["sp_close"].shift(1)   # yesterday's S&P — closes 1:30 AM IST, not known at 9:30 AM IST
    _nk   = d["nk_close"].shift(1)   # yesterday's Nikkei — full-day close is noon IST, after trade entry

    # ── Core technicals ───────────────────────────────────────────────────────
    d["ema20"]      = _c.ewm(span=20, adjust=False).mean()
    d["rsi14"]      = _rsi(_c, 14)
    d["trend5"]     = (_c - _c.shift(5)) / _c.shift(5) * 100
    d["vix_dir"]    = _vix - _vix.shift(1)
    d["sp500_chg"]  = (_sp / _sp.shift(1) - 1) * 100
    d["nikkei_chg"] = (_nk / _nk.shift(1) - 1) * 100
    d["spf_gap"]    = (d["spf_open"] - d["spf_close"].shift(1)) / d["spf_close"].shift(1) * 100
    bn_chg          = (_c / _c.shift(1) - 1) * 100
    nf_chg          = (_c_nf / _c_nf.shift(1) - 1) * 100
    d["bn_nf_div"]  = bn_chg - nf_chg
    log_ret         = np.log(_c / _c.shift(1))
    d["hv20"]       = log_ret.rolling(20).std() * np.sqrt(252) * 100
    d["bn_gap"]     = (d["bn_open"] - _c) / _c * 100

    # ── Rule-based score (4 active indicators) ────────────────────────────────
    d["s_ema20"]     = np.where(_c > d["ema20"], 1, -1)
    d["s_trend5"]    = np.where(d["trend5"] > 1.0, 1, np.where(d["trend5"] < -1.0, -1, 0))
    d["s_vix"]       = np.where(d["vix_dir"] < 0,  1, np.where(d["vix_dir"] > 0, -1, 0))
    d["s_bn_nf_div"] = np.where(d["bn_nf_div"] > 0.5, 1, np.where(d["bn_nf_div"] < -0.5, -1, 0))
    d["rule_score"]  = d["s_ema20"] + d["s_trend5"] + d["s_vix"] + d["s_bn_nf_div"]
    d["rule_signal"] = np.where(d["rule_score"] >= SCORE_THRESHOLD, "CALL",
                       np.where(d["rule_score"] <= -SCORE_THRESHOLD, "PUT", "NONE"))

    # ── Extended ML features ──────────────────────────────────────────────────
    d["ema20_pct"]    = (_c - d["ema20"]) / d["ema20"] * 100
    d["vix_level"]    = _vix

    # ── Prev-day OHLC-derived features (all use yesterday's values — no leakage) ─
    # prev_range_pct: yesterday's high-low range as % of close.
    #   High range day → today likely high-range too (volatility clustering).
    #   Low range day → probably quiet again; SL/TP unlikely to be hit cleanly.
    _ph = d["bn_high"].shift(1)
    _pl = d["bn_low"].shift(1)
    _po = d["bn_open"].shift(1)
    d["prev_range_pct"]  = (_ph - _pl) / _c * 100
    # prev_body_pct: candle body / range. Close to 1 = strong trending candle
    # (high directional conviction); close to 0 = doji/indecision.
    d["prev_body_pct"]   = ((_c - _po) / (_ph - _pl).replace(0, np.nan)).fillna(0.0)
    # bn_ret60: 3-month return — medium-term trend regime.
    #   Positive = bull phase (favour CALL); negative = bear phase (favour PUT).
    d["bn_ret60"]        = (_c / _c.shift(60) - 1) * 100
    # bn_dist_high52: % below the 52-week rolling high.
    #   Near 0 = at all-time-high territory → strong bull momentum.
    #   Very negative = deep correction → potential mean-reversion.
    d["bn_dist_high52"]  = (_c / _c.rolling(252, min_periods=60).max() - 1) * 100

    # ── Short-term momentum ────────────────────────────────────────────────
    d["bn_ret5"]       = (_c / _c.shift(5) - 1) * 100

    # ── ADX14 (Average Directional Index) ──────────────────────────────────
    # Measures trend strength (0-100). High ADX = strong trend, directional signals reliable.
    _ph_adx = d["bn_high"].shift(1)   # yesterday's high
    _pl_adx = d["bn_low"].shift(1)    # yesterday's low
    _plus_dm  = (_ph_adx - _ph_adx.shift(1)).clip(lower=0)
    _minus_dm = (_pl_adx.shift(1) - _pl_adx).clip(lower=0)
    # Zero out when the other DM is larger
    _plus_dm  = np.where(_plus_dm > _minus_dm, _plus_dm, 0.0)
    _minus_dm = np.where(pd.Series(_minus_dm) > pd.Series(_plus_dm), _minus_dm, 0.0)
    _plus_dm  = pd.Series(_plus_dm, index=d.index)
    _minus_dm = pd.Series(_minus_dm, index=d.index)
    _tr = pd.concat([
        (_ph_adx - _pl_adx).abs(),
        (_ph_adx - _c.shift(1)).abs(),
        (_pl_adx - _c.shift(1)).abs()
    ], axis=1).max(axis=1)
    _atr14    = _tr.ewm(span=14, adjust=False).mean()
    _plus_di  = 100 * _plus_dm.ewm(span=14, adjust=False).mean() / _atr14.replace(0, np.nan)
    _minus_di = 100 * _minus_dm.ewm(span=14, adjust=False).mean() / _atr14.replace(0, np.nan)
    _dx       = ((_plus_di - _minus_di).abs() / (_plus_di + _minus_di).replace(0, np.nan) * 100).fillna(0)
    d["adx14"] = _dx.ewm(span=14, adjust=False).mean()

    # ── ADX-weighted trend interaction ─────────────────────────────────────
    # When ADX is high (strong trend), trend5 direction is more predictive
    d["adx_trend_interact"] = d["adx14"] * d["s_ema20"] / 100.0  # scaled
    # ADX-weighted gap: strong trend + gap = likely continuation
    d["adx_gap_interact"]   = d["adx14"] * d["bn_gap"] / 100.0

    # ── VIX open direction at 9:15 AM ────────────────────────────────────────
    # Moved here (before interaction features) so interactions can safely reference it.
    if "vix_open" in d.columns:
        d["vix_open_chg"] = (d["vix_open"] - _vix) / _vix.replace(0, np.nan) * 100
    else:
        d["vix_open_chg"] = 0.0
    d["vix_open_chg"] = d["vix_open_chg"].fillna(0.0)

    # ── Base features — ALL computed before interaction section ──────────────
    # Moved here so any interaction feature (including autoloop-added ones) can
    # safely reference vix_pct_chg, vix_hv_ratio, bn_ret1/20, dow, dte, etc.
    d["vix_pct_chg"]   = d["vix_dir"] / _vix.shift(1) * 100
    d["vix_hv_ratio"]  = _vix / d["hv20"].replace(0, np.nan)
    d["bn_ret1"]        = (_c / _c.shift(1) - 1) * 100
    d["bn_ret20"]       = (_c / _c.shift(20) - 1) * 100
    d["bn_dist_high20"] = (_c / _c.rolling(20).max() - 1) * 100
    d["dow"]           = d["date"].dt.weekday
    d["dte"]           = d["date"].apply(
                             lambda x: get_dte(x.date() if hasattr(x, "date") else x))

    # ── Interaction features ───────────────────────────────────────────────
    # NOTE FOR AUTOLOOP: add NEW features AFTER the final ADX block (line ~479),
    # just before the return statement. Never insert in the middle of this section.

    # Gap-momentum alignment: gap in same direction as 5-day momentum → continuation
    d["gap_mom_align"]    = d["bn_gap"] * d["bn_ret5"]

    # VIX-trend interaction: VIX falling + bullish trend = strong CALL signal
    d["vix_trend_interact"] = d["vix_dir"] * d["s_ema20"]

    # Prev-day body conviction aligned with short-term momentum
    d["prev_body_momentum"] = d["prev_body_pct"] * d["bn_ret5"]

    # IV × SPF gap: when IV is high, global overnight signal is more decisive
    # iv_proxy is computed later, so we compute a local version here
    _call_p = d.get("call_premium", pd.Series(np.nan, index=d.index))
    _put_p  = d.get("put_premium", pd.Series(np.nan, index=d.index))
    _straddle = _call_p + _put_p
    _straddle_ma = _straddle.shift(1).rolling(20, min_periods=5).mean()
    _iv_local = ((_straddle - _straddle_ma) / _straddle_ma.replace(0, np.nan)).fillna(0.0)
    d["iv_spf_interaction"] = _iv_local * d["spf_gap"]

    # 52-week high regime × EMA trend: near highs + bullish EMA = strong CALL
    d["high52_ema_interact"] = d["bn_dist_high52"] * d["s_ema20"]

    # ── NEW: PCR momentum signals ─────────────────────────────────────────────
    # pcr_ma5: 5-day smoothed PCR. Trend in sentiment is more reliable than
    #          the single-day reading (option writers hedge over days).
    # pcr_chg: day-over-day PCR change — sudden spike in put buying = bearish.
    if "pcr" in d.columns:
        d["pcr_ma5"] = d["pcr"].rolling(5, min_periods=2).mean().fillna(d["pcr"])
        d["pcr_chg"] = d["pcr"].diff().fillna(0.0)
    else:
        d["pcr_ma5"] = 1.0
        d["pcr_chg"] = 0.0

    # ── Global macro returns ─────────────────────────────────────────────────
    # crude_ret: crude oil daily % return. Rising crude → inflation risk →
    #            hawkish Fed → FII selling → bearish for banking index.
    # dxy_ret:   dollar index daily % return. Strong dollar → FII outflows
    #            from India → BN selling pressure.
    # us10y_chg: US 10Y yield change (bps-like). Rising yields → banks' cost
    #            of funds rises → HDFC/Kotak/SBI under pressure → BN PUT signal.
    # All macro series settle on US/London hours — shift by 1 so training uses
    # the same prior-day settlement that the live system sees at 9:30 AM IST.
    for _feat, _src, _mode in [("crude_ret",   "crude_close",  "pct"),
                                ("dxy_ret",     "dxy_close",    "pct"),
                                ("us10y_chg",   "us10y_close",  "diff"),
                                ("usdinr_ret",  "usdinr_close", "pct")]:
        if _src in d.columns:
            _s = d[_src].shift(1)   # yesterday's settlement
            if _mode == "pct":
                d[_feat] = (_s / _s.shift(1) - 1) * 100
            else:
                d[_feat] = _s.diff()
        else:
            d[_feat] = 0.0
        d[_feat] = d[_feat].fillna(0.0)

    # ── NEW: FII net cash flow (z-scored) ────────────────────────────────────
    # FII cash market activity is the dominant institutional flow driver.
    # Heavy FII selling (negative) = bearish regardless of technicals.
    # Z-scored over 60-day rolling window to normalise for changing market size.
    # FII data is previous day's — no lookahead.
    if "fii_net_cash" in d.columns:
        _fii = d["fii_net_cash"].fillna(0.0)
        _mu  = _fii.rolling(60, min_periods=10).mean()
        _std = _fii.rolling(60, min_periods=10).std().replace(0, np.nan)
        d["fii_net_cash_z"] = ((_fii - _mu) / _std).fillna(0.0)
    else:
        d["fii_net_cash_z"] = 0.0

    # ── NEW: Real options market signals ──────────────────────────────────────
    # put_call_skew: put_premium / call_premium at ATM open.
    #   > 1.0 → market pricing in more downside risk (PUT signal)
    #   < 1.0 → market pricing in more upside risk (CALL signal)
    # iv_proxy: actual ATM premium relative to formula. Captures IV regime —
    #   high IV days = larger intraday ranges = TP/SL more likely to be hit.
    # Both use today's option OPEN prices (known at 9:30 AM) — no lookahead.
    if "call_premium" in d.columns and "put_premium" in d.columns:
        _cp = d["call_premium"].replace(0, np.nan)
        _pp = d["put_premium"].replace(0, np.nan)
        d["put_call_skew"] = (_pp / _cp).fillna(1.0)
        # iv_proxy: average of call/put vs formula premium; z-scored over 60d
        _avg_prem    = ((_cp + _pp) / 2).fillna(np.nan)
        _dte_vals    = d["date"].apply(lambda x: get_dte(x.date() if hasattr(x, "date") else x))
        _formula_p   = d["bn_open"] * PREMIUM_K * (_dte_vals ** 0.5)
        _iv_raw      = (_avg_prem / _formula_p.replace(0, np.nan)).fillna(1.0)
        _iv_mu       = _iv_raw.rolling(60, min_periods=10).mean().fillna(1.0)
        _iv_std      = _iv_raw.rolling(60, min_periods=10).std().replace(0, np.nan)
        d["iv_proxy"] = ((_iv_raw - _iv_mu) / _iv_std).fillna(0.0)
    else:
        d["put_call_skew"] = 1.0
        d["iv_proxy"]      = 0.0

    # ── Straddle expansion vs 20-day mean ────────────────────────────────────
    # ratio > 1.2 = IV elevated vs recent norm → big move expected → TP more likely.
    if "call_premium" in d.columns and "put_premium" in d.columns:
        _straddle    = (d["call_premium"].fillna(0) + d["put_premium"].fillna(0)).replace(0, np.nan)
        _straddle_ma = _straddle.rolling(20, min_periods=5).mean().replace(0, np.nan)
        d["straddle_expansion"] = (_straddle / _straddle_ma).fillna(1.0)
    else:
        d["straddle_expansion"] = 1.0

    # ── ADX 14 (Average Directional Index) ──────────────────────────────────
    # ADX > 25 = market trending (momentum signals more reliable)
    # ADX < 15 = market ranging (signals unreliable, mean-reversion dominates)
    # Uses shifted OHLCV series (_ph, _pl, _c already shift(1)) — no leakage.
    _prev_c2 = _c.shift(1)
    _ph_prev = _ph.shift(1)
    _pl_prev = _pl.shift(1)
    _tr_adx  = pd.concat([
        _ph - _pl,
        (_ph - _prev_c2).abs(),
        (_pl - _prev_c2).abs(),
    ], axis=1).max(axis=1)
    _up_dm  = (_ph - _ph_prev).clip(lower=0)
    _dn_dm  = (_pl_prev - _pl).clip(lower=0)
    _dm_p_  = _up_dm.where(_up_dm > _dn_dm, 0.0)
    _dm_m_  = _dn_dm.where(_dn_dm > _up_dm, 0.0)
    _atr14_ = _tr_adx.ewm(com=13, min_periods=14).mean()
    _di_p_  = 100 * _dm_p_.ewm(com=13, min_periods=14).mean() / _atr14_.replace(0, np.nan)
    _di_m_  = 100 * _dm_m_.ewm(com=13, min_periods=14).mean() / _atr14_.replace(0, np.nan)
    _dx_    = 100 * (_di_p_ - _di_m_).abs() / (_di_p_ + _di_m_).replace(0, np.nan)
    d["adx14"] = _dx_.ewm(com=13, min_periods=14).mean().fillna(20.0)

    # ── Rule score momentum (yesterday's conviction) ─────────────────────────
    # Two consecutive strong rule_score days = sustained institutional momentum.
    d["rule_score_lag1"] = d["rule_score"].shift(1).fillna(0.0)

    # ── IV skew features ──────────────────────────────────────────────────────
    # call_skew:   OTM (ATM+3) call IV − ATM call IV. +ve = upside tail priced in.
    # put_skew:    OTM (ATM-3) put  IV − ATM put  IV. +ve = downside tail priced (normal).
    # skew_spread: put_skew − call_skew. +ve = market fears downside more → bearish.
    # skew_chg:    day-over-day change in skew_spread — fear momentum.
    # All shifted by 1 so training/live see identical prior-day values at 9:30 AM.
    for _iv_col in ["call_iv_atm", "put_iv_atm", "call_iv_otm", "put_iv_otm"]:
        if _iv_col not in d.columns:
            d[_iv_col] = np.nan
    _c_iv_atm  = d["call_iv_atm"].shift(1)
    _p_iv_atm  = d["put_iv_atm"].shift(1)
    _c_iv_otm  = d["call_iv_otm"].shift(1)
    _p_iv_otm  = d["put_iv_otm"].shift(1)
    _call_sk   = (_c_iv_otm - _c_iv_atm).fillna(0.0)
    _put_sk    = (_p_iv_otm - _p_iv_atm).fillna(0.0)
    _sk_spread = (_put_sk - _call_sk).fillna(0.0)
    d["call_skew"]   = _call_sk
    d["put_skew"]    = _put_sk
    d["skew_spread"] = _sk_spread
    d["skew_chg"]    = _sk_spread.diff().fillna(0.0)

    # ── Skew interactions ────────────────────────────────────────────────────
    # skew_trend_interact: skew_spread × s_ema20.
    #   +ve on bearish-trend day with put skew expanding → high-conviction PUT
    #   +ve on bullish-trend day with call skew expanding → high-conviction CALL
    #   Signs the skew reading by the underlying trend regime.
    # skew_vix_regime: skew_chg × vix_dir.
    #   +ve = both rising (fear momentum accelerating → strongest reversal signal)
    #   -ve = divergence (one easing while the other tightens → weak/noisy signal)
    d["skew_trend_interact"] = d["skew_spread"] * d["s_ema20"]
    d["skew_vix_regime"]     = d["skew_chg"] * d["vix_dir"]

    # ── OI surface features (from options_oi_surface.csv) ─────────────────────
    # oi_pcr_wide:       Σpe_oi / Σce_oi across ATM±3 — broader, more robust than ATM PCR
    # oi_imbalance_atm:  (ce_oi_atm − pe_oi_atm) / total — directional bias at ATM
    # call_wall_offset:  offset (-3..+3) of max CE OI strike — resistance position
    # put_wall_offset:   offset (-3..+3) of max PE OI strike — support position
    # All shifted by 1 (prior day's EOD OI is what's known at 9:30 AM).
    _CE_OI_COLS = ["ce_oi_m3","ce_oi_m2","ce_oi_m1","ce_oi_atm","ce_oi_p1","ce_oi_p2","ce_oi_p3"]
    _PE_OI_COLS = ["pe_oi_m3","pe_oi_m2","pe_oi_m1","pe_oi_atm","pe_oi_p1","pe_oi_p2","pe_oi_p3"]
    _OFFSETS    = [-3, -2, -1, 0, 1, 2, 3]
    for _c in _CE_OI_COLS + _PE_OI_COLS:
        if _c not in d.columns:
            d[_c] = np.nan
    # Prior-day OI (known at 9:30 AM) — pandas shift on DataFrame returns DataFrame
    _ce_oi = d[_CE_OI_COLS].shift(1).fillna(0.0)
    _pe_oi = d[_PE_OI_COLS].shift(1).fillna(0.0)
    _ce_sum = _ce_oi.sum(axis=1).replace(0, np.nan)
    _pe_sum = _pe_oi.sum(axis=1).replace(0, np.nan)
    d["oi_pcr_wide"] = (_pe_sum / _ce_sum).fillna(1.0)
    _ce_atm = _ce_oi["ce_oi_atm"]
    _pe_atm = _pe_oi["pe_oi_atm"]
    _atm_tot = (_ce_atm + _pe_atm).replace(0, np.nan)
    d["oi_imbalance_atm"] = ((_ce_atm - _pe_atm) / _atm_tot).fillna(0.0)
    # Argmax of OI across offsets — returns index (0..6); map to offset (-3..+3)
    _ce_max_idx = _ce_oi.values.argmax(axis=1)
    _pe_max_idx = _pe_oi.values.argmax(axis=1)
    d["call_wall_offset"] = [_OFFSETS[i] if _ce_sum.iloc[n] > 0 else 0
                              for n, i in enumerate(_ce_max_idx)]
    d["put_wall_offset"]  = [_OFFSETS[i] if _pe_sum.iloc[n] > 0 else 0
                              for n, i in enumerate(_pe_max_idx)]

    # ── Max pain + GEX (from options_atm_daily.csv, written by auto_trader) ──
    # max_pain_dist_prev: prior-day spot vs max-pain strike as %. Spot tends to
    #   drift toward max pain near expiry; large absolute values flag mean-revert setups.
    # gex_flag_prev:    1 if gamma exposure positive (ranging regime), -1 if negative
    #   (trend regime), 0 if unknown. Dealer hedging flow signal.
    if "max_pain_dist" in d.columns:
        d["max_pain_dist_prev"] = d["max_pain_dist"].shift(1).fillna(0.0)
    else:
        d["max_pain_dist_prev"] = 0.0
    if "gex_positive" in d.columns:
        _gex = d["gex_positive"].shift(1)
        # Handle bool/int/string representations gracefully
        d["gex_flag_prev"] = _gex.map(lambda x: 1 if str(x).lower() in ("true","1","1.0")
                                                  else (-1 if str(x).lower() in ("false","0","0.0")
                                                        else 0)).fillna(0).astype(int)
    else:
        d["gex_flag_prev"] = 0

    # ── VIX percentile (IVP — 252d rank) ─────────────────────────────────────
    # Raw VIX level is regime-dependent (14 in calm vs stressed era means different things).
    # Percentile rank 0-1 normalizes across regimes. Uses yesterday's VIX (_vix already shifted).
    d["vix_pct_rank_252"] = _vix.rolling(252, min_periods=60).rank(pct=True).fillna(0.5)

    # ── BN-Nifty relative strength ───────────────────────────────────────────
    # bn_nifty_rs:        BN / Nifty ratio (prior-day closes) — absolute leadership
    # bn_nifty_rs_slope5: 5-day % change in that ratio — leadership momentum
    # Different from bn_nf_div (which is single-day Δ% diff): this captures sustained
    # outperformance/underperformance rather than one-day swings.
    _c_f   = pd.to_numeric(_c,    errors="coerce")
    _cnf_f = pd.to_numeric(_c_nf, errors="coerce").replace(0, np.nan)
    _rs = _c_f / _cnf_f
    d["bn_nifty_rs"]        = _rs.ffill().fillna(1.0)
    d["bn_nifty_rs_slope5"] = ((_rs / _rs.shift(5) - 1) * 100).fillna(0.0)

    # ── Bank ETF flow (BANKBEES) ─────────────────────────────────────────────
    # bankbees_ret1: prior-day BANKBEES return — domestic institutional bank-sector flow
    # bankbees_vol_z: volume z-score over 60d — unusual flow detection
    if "bankbees_close" in d.columns:
        _bb_c = d["bankbees_close"].shift(1)
        d["bankbees_ret1"] = ((_bb_c / _bb_c.shift(1) - 1) * 100).fillna(0.0)
    else:
        d["bankbees_ret1"] = 0.0
    if "bankbees_vol" in d.columns:
        _bb_v = d["bankbees_vol"].shift(1)
        _bb_mu  = _bb_v.rolling(60, min_periods=10).mean()
        _bb_std = _bb_v.rolling(60, min_periods=10).std().replace(0, np.nan)
        d["bankbees_vol_z"] = ((_bb_v - _bb_mu) / _bb_std).fillna(0.0)
    else:
        d["bankbees_vol_z"] = 0.0

    # ── Bank breadth (top-5 BN constituents) ─────────────────────────────────
    # bank_breadth_d1: fraction of top-5 (HDFC/ICICI/KOTAK/SBI/AXIS) with +ve return
    #   yesterday. Proxy for how broad-based any BN move was — narrow moves (1-2 stocks
    #   carrying the index) often reverse; broad moves persist.
    # bank_breadth_z:  60d z-score of breadth — regime detector.
    _stock_cols = ["hdfc_close","icici_close","kotak_close","sbi_close","axis_close"]
    _returns = []
    for _sc in _stock_cols:
        if _sc in d.columns:
            _sc_s = d[_sc].shift(1)
            _ret  = (_sc_s / _sc_s.shift(1) - 1)
            _returns.append(_ret)
    if _returns:
        _ret_df = pd.concat(_returns, axis=1)
        d["bank_breadth_d1"] = (_ret_df > 0).sum(axis=1).fillna(0) / max(len(_returns), 1)
        _br_mu  = d["bank_breadth_d1"].rolling(60, min_periods=10).mean()
        _br_std = d["bank_breadth_d1"].rolling(60, min_periods=10).std().replace(0, np.nan)
        d["bank_breadth_z"] = ((d["bank_breadth_d1"] - _br_mu) / _br_std).fillna(0.0)
    else:
        d["bank_breadth_d1"] = 0.5
        d["bank_breadth_z"]  = 0.0

    # ── Opening Range Breakout (9:15-9:30 15-min candle, prior day) ──────────
    # orb_range_pct:  prior-day 9:15-9:30 candle range as % of spot — volatility proxy
    # orb_break_side: +1 if prior-day close > prior-day 9:15 candle high,
    #                 -1 if prior-day close < prior-day 9:15 candle low,
    #                  0 if closed inside the opening range.
    # Training uses prior day's ORB because today's 9:15-9:30 candle isn't known
    # until 9:30 AM (auto_trader would need to fetch live for today's value —
    # a separate task; for now, training + live both use shift(1)).
    if "orb_high" in d.columns and "orb_low" in d.columns:
        _orb_h = d["orb_high"].shift(1)
        _orb_l = d["orb_low"].shift(1)
        _bn_prev_close = _c  # _c is already shift(1)
        _range = (_orb_h - _orb_l).replace(0, np.nan)
        d["orb_range_pct"] = (_range / _c * 100).fillna(0.0)
        d["orb_break_side"] = np.where(_bn_prev_close > _orb_h, 1,
                              np.where(_bn_prev_close < _orb_l, -1, 0))
    else:
        d["orb_range_pct"]  = 0.0
        d["orb_break_side"] = 0

    # ── AUTOLOOP APPEND ZONE — add new features HERE, just above this line ──────
    # All features above are already computed. Adding code here means you can safely
    # reference ANY column that exists earlier in this function without KeyError.

    req = ["ema20","rsi14","trend5","vix_dir","sp500_chg","nikkei_chg","spf_gap",
           "bn_nf_div","hv20","bn_gap","vix_pct_chg","vix_hv_ratio","bn_ret20"]
    return d.dropna(subset=req)


# 31 features fed into the RF
FEATURE_COLS = [
    # Rule-based score components (discrete ±1 signals) + yesterday's conviction
    "s_ema20", "s_trend5", "s_vix", "s_bn_nf_div", "rule_score_lag1",
    # Continuous versions of same signals
    "ema20_pct", "trend5", "vix_dir", "bn_nf_div",
    # Additional technical indicators
    "rsi14", "hv20", "bn_gap", "adx14",
    # Global market
    "sp500_chg", "nikkei_chg", "spf_gap",
    # Volatility regime
    "vix_level", "vix_pct_chg", "vix_hv_ratio",
    # Momentum & drawdown
    "bn_ret1", "bn_ret20", "bn_dist_high20",
    # Calendar
    "dow", "dte",
    # VIX opening direction at 9:15 AM (risk-off/on signal at trade entry)
    "vix_open_chg",
    # Real options market signals (ATM open prices, known at 9:30 AM)
    "put_call_skew",        # put/call premium ratio — market's directional bias
    "iv_proxy",             # z-scored IV level — high IV = wider intraday range expected
    "straddle_expansion",   # today's straddle vs 20d mean — IV expansion signal
    # Prev-day candle structure (directional conviction + range regime)
    "prev_range_pct",  # yesterday's H-L range % — predicts today's range via volatility clustering
    "prev_body_pct",   # yesterday's body/range ratio — strong candle = trend continuation
    # Medium/long-term trend regime
    "bn_ret60",        # 3-month return — bull vs bear phase
    "bn_dist_high52",  # % below 52-week high — momentum / overbought signal
    # Interaction features
    "bn_ret5",              # 5-day momentum — short-term trend
    "gap_mom_align",        # bn_gap × bn_ret5 — gap aligned with momentum
    "iv_spf_interaction",   # iv_proxy × spf_gap — IV amplifies global signal
    "high52_ema_interact",  # dist_high52 × s_ema20 — regime × trend
    # VIX-trend interaction
    "vix_trend_interact",   # vix_dir × s_ema20 — VIX decline + bullish trend
    # Prev-day conviction × momentum
    "prev_body_momentum",   # prev_body_pct × bn_ret5 — candle conviction + momentum
    # Options/flow features (already computed)
    "pcr_ma5",              # 5-day smoothed put-call ratio
    "fii_net_cash_z",       # z-scored FII net cash flow
    # ADX interaction features (adx14 itself is already listed above)
    "adx_trend_interact",   # ADX × s_ema20 — strong trend amplifies direction
    "adx_gap_interact",     # ADX × bn_gap — strong trend + gap = continuation
    # IV skew dynamics (from options_iv_skew.csv — populated by data_fetcher.py --fetch-options)
    "call_skew",    # OTM call IV − ATM call IV — upside tail risk pricing
    "put_skew",     # OTM put IV  − ATM put IV  — downside tail risk pricing (normally +ve)
    "skew_spread",  # put_skew − call_skew — net downside fear signal
    "skew_chg",     # day-over-day Δ skew_spread — fear momentum
    # Skew interactions
    "skew_trend_interact",  # skew_spread × s_ema20 — skew signed by trend regime
    "skew_vix_regime",      # skew_chg × vix_dir — fear momentum × vol regime
    # OI surface (from options_oi_surface.csv — populated by data_fetcher.py --fetch-options)
    "oi_pcr_wide",       # Σpe_oi / Σce_oi across ATM±3 — broader PCR
    "oi_imbalance_atm",  # ATM CE vs PE OI imbalance — directional bias
    "call_wall_offset",  # offset (-3..+3) of max CE OI strike — resistance position
    "put_wall_offset",   # offset (-3..+3) of max PE OI strike — support position
    # Max pain + GEX (written daily by auto_trader into options_atm_daily.csv)
    "max_pain_dist_prev",  # % distance spot vs max-pain strike (prior day)
    "gex_flag_prev",       # +1 ranging / -1 trending / 0 unknown (dealer gamma regime)
    # VIX percentile + relative strength
    "vix_pct_rank_252",    # VIX 252-day percentile — regime-normalized fear level
    "bn_nifty_rs",         # BN/Nifty ratio — sector leadership level
    "bn_nifty_rs_slope5",  # 5-day % change in BN/Nifty ratio — leadership momentum
    # Bank sector ETF flow + breadth (requires yfinance BANKBEES + top-5 stocks)
    "bankbees_ret1",       # BANKBEES prior-day return — domestic bank ETF flow
    "bankbees_vol_z",      # BANKBEES volume z-score (60d) — unusual flow detection
    "bank_breadth_d1",     # % of top-5 BN constituents up yesterday — move conviction
    "bank_breadth_z",      # 60d z-score of breadth — regime detector
    # Opening range breakout (prior day's 9:15 candle — from banknifty_15m_orb.csv)
    "orb_range_pct",       # prior-day 9:15 candle range as % of spot — vol proxy
    "orb_break_side",      # +1/-1/0 — did prev close break above/below/inside 9:15 range
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
        formula_prem = o * PREMIUM_K * (dte ** 0.5)

        # Use real ATM premiums when available — formula is a rough approximation
        # that ignores IV crush, skew, and regime. Real premiums = accurate SL/TP.
        call_prem = r["call_premium"] if ("call_premium" in r.index and pd.notna(r["call_premium"])) else formula_prem
        put_prem  = r["put_premium"]  if ("put_premium"  in r.index and pd.notna(r["put_premium"]))  else formula_prem

        call_out = simulate_outcome(o, h, l, c, "CALL", call_prem)
        put_out  = simulate_outcome(o, h, l, c, "PUT",  put_prem)

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

ENSEMBLE_DIR  = f"{MODELS_DIR}/ensemble"
ENSEMBLE_META = f"{MODELS_DIR}/ensemble_meta.json"


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


def load_ensemble():
    """
    Load the per-type ensemble from models/ensemble/ (rf.pkl, xgb.pkl, lgb.pkl).
    Returns ([(model, meta), ...], trained_at) or ([], None) if unavailable / stale.

    Freshness check uses the same CHAMPION_MAX_AGE_DAYS as the single champion.
    Falls back gracefully — if only 2 of 3 files exist, those 2 are used.
    """
    import json as _json
    import joblib
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    if not os.path.exists(ENSEMBLE_META):
        return [], None

    try:
        with open(ENSEMBLE_META) as f:
            metas = _json.load(f)   # dict: {mtype: meta_dict}
    except Exception as e:
        print(f"  Could not read ensemble_meta.json: {e}")
        return [], None

    # Freshness: check first model's trained_at
    first_meta = next(iter(metas.values()), {})
    trained_at_str = first_meta.get("trained_at", "")
    try:
        trained_at = _dt.fromisoformat(trained_at_str)
        if trained_at.tzinfo is None:
            trained_at = trained_at.replace(tzinfo=_tz(_td(hours=5, minutes=30)))
        age_days = (_dt.now(trained_at.tzinfo) - trained_at).days
        if age_days > CHAMPION_MAX_AGE_DAYS:
            print(f"  Ensemble is {age_days} days old (max {CHAMPION_MAX_AGE_DAYS}) — will retrain.")
            return [], None
    except Exception:
        pass   # Can't parse date → proceed anyway

    loaded = []
    for mtype in ["rf", "xgb", "lgb", "cat", "tabpfn"]:
        pkl_path = f"{ENSEMBLE_DIR}/{mtype}.pkl"
        if not os.path.exists(pkl_path):
            continue
        if mtype not in metas:
            continue
        try:
            model = joblib.load(pkl_path)
            loaded.append((model, metas[mtype]))
        except Exception as e:
            print(f"  Could not load ensemble[{mtype}]: {e}")

    return loaded, trained_at_str if loaded else None


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
        # Pre-market: banknifty.csv has no today row yet (Dhan historical API
        # only returns closed candles). Use the latest available row's features
        # — those ARE today's entry conditions (based on yesterday's close).
        if df.empty:
            return None
        today_rows = df.iloc[[-1]]

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
        # Pre-market: banknifty.csv doesn't have today's candle yet.
        # Use the latest available trading day's features — those represent
        # current market conditions (yesterday's close + macro data).
        if trading.empty:
            print("  No trading data available — cannot predict.")
            return
        today_idx = len(trading) - 1
        feat_date = trading.iloc[-1]["date"].date()
        print(f"  Pre-market: using {feat_date} features for {today_dt} prediction")
    else:
        today_idx = today_rows.index[0]

    rule_row   = trading.iloc[today_idx]
    rule_sig   = str(rule_row.get("rule_signal", "NONE"))
    rule_score = int(rule_row.get("rule_score", 0))

    ml_trained = False
    p_call = p_put = 0.5

    # ── Fast path A: ensemble vote (RF + XGB + LGB majority) ─────────────────
    # Preferred over single champion — reduces per-model variance significantly.
    # All 3 models predict independently; majority direction wins.
    # ml_conf = average probability of the models that voted for the winning direction.
    ensemble_members, ensemble_trained_at = load_ensemble()

    if ensemble_members:
        votes  = []   # direction per model
        confs  = []   # max(p_call, p_put) per model
        pcalls = []

        for model, meta in ensemble_members:
            fc     = meta["feature_cols"]
            X_t    = get_today_features(fc)
            if X_t is None or len(X_t) == 0:
                continue
            proba   = model.predict_proba(X_t)[0]
            classes = list(model.classes_)
            pc = float(proba[classes.index(1)]) if 1 in classes else 0.5
            pp = float(proba[classes.index(0)]) if 0 in classes else 0.5
            votes.append("CALL" if pc >= pp else "PUT")
            confs.append(max(pc, pp))
            pcalls.append(pc)

        if votes:
            call_v = votes.count("CALL")
            put_v  = votes.count("PUT")
            ml_signal = "CALL" if call_v >= put_v else "PUT"
            # Confidence = average prob from models that agreed with winner
            agreed_confs = [c for v, c in zip(votes, confs) if v == ml_signal]
            ml_conf   = sum(agreed_confs) / len(agreed_confs)
            p_call    = sum(pcalls) / len(pcalls)   # avg across ensemble
            p_put     = 1.0 - p_call
            ml_trained = True
            names = {"rf": "RF", "xgb": "XGB", "lgb": "LGB", "cat": "CAT"}
            vote_str = "  ".join(
                f"{names.get(m[1]['model_type'], '?')}:{v}"
                for m, v in zip(ensemble_members, votes)
            )
            print(f"  Ensemble ({len(votes)} models, trained {(ensemble_trained_at or '')[:10]}):")
            n_models = len(votes)
            print(f"  {vote_str}  →  {call_v}/{n_models} CALL  {put_v}/{n_models} PUT")
            print(f"  → {ml_signal}  (avg agreed conf {ml_conf:.1%})")

    # ── Fast path B: single champion model (fallback if ensemble not ready) ───
    champion_model = None
    if not ensemble_members:
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
    if not ml_trained:
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
        signals_csv = f"{DATA_DIR}/signals.csv"
        if not _os.path.exists(signals_csv):
            print(f"  ERROR: both signals_ml.csv and signals.csv missing — cannot create base.")
            print(f"  Run: python3 data_fetcher.py && python3 signal_engine.py")
            try:
                import notify as _notify
                _notify.send(
                    "⚠️ <b>ML Engine</b>\n"
                    "Both signals_ml.csv and signals.csv are missing.\n"
                    "Run <code>data_fetcher.py → signal_engine.py</code> manually."
                )
            except Exception:
                pass
            return None
        print(f"  signals_ml.csv not found — creating from signals.csv + today.")
        base = pd.read_csv(signals_csv, parse_dates=["date"])
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
