#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
replay_today.py — Post-mortem ensemble replay
==============================================
Runs on the VM after model_evolver.py to answer:
  "Would the new ensemble have called today differently?
   And what would the P&L outcome have been?"

Shows:
  - Each model's individual vote (RF / XGB / LGB)
  - Ensemble majority signal + agreement confidence
  - Actual trade vs ensemble direction comparison
  - Estimated P&L for BOTH scenarios using today's BN OHLCV
  - Entry premium, SL level, TP level, trailing jump

Usage:
  python3 replay_today.py
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import date, datetime

DATA_DIR   = "data"
MODELS_DIR = "models"

SL_PCT  = 0.15       # must match auto_trader.py
RR      = 2.5
TP_PCT  = SL_PCT * RR   # 0.375
LOT_SIZE = 30

MTYPE_NAMES = {"rf": "RandomForest", "xgb": "XGBoost", "lgb": "LightGBM"}


# ─────────────────────────────────────────────────────────────────────────────
#  LOAD ENSEMBLE
# ─────────────────────────────────────────────────────────────────────────────

def load_ensemble():
    import joblib
    meta_path = f"{MODELS_DIR}/ensemble_meta.json"
    if not os.path.exists(meta_path):
        return []
    with open(meta_path) as f:
        metas = json.load(f)
    loaded = []
    for mtype in ["rf", "xgb", "lgb"]:
        pkl = f"{MODELS_DIR}/ensemble/{mtype}.pkl"
        if os.path.exists(pkl) and mtype in metas:
            try:
                model = joblib.load(pkl)
                loaded.append((mtype, model, metas[mtype]))
            except Exception as e:
                print(f"  Could not load {mtype}: {e}")
    return loaded


# ─────────────────────────────────────────────────────────────────────────────
#  P&L SIMULATION (mirrors ml_engine.simulate_outcome exactly)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_trade(signal, entry_premium, nf_open, nf_high, nf_low, nf_close, lots):
    """
    Simulate whether SL, TP, or EOD exit was hit, and compute P&L.

    NF index pts required to hit SL/TP are derived from premium + delta ≈ 0.5.
    This is the same formula used by ml_engine.simulate_outcome() for training labels,
    so simulation is consistent with what the model was trained on.
    """
    sl_pts = (SL_PCT * entry_premium) / 0.5   # NF pts where option SL hits
    tp_pts = (TP_PCT * entry_premium) / 0.5   # NF pts where option TP hits

    sl_price = round(entry_premium * (1 - SL_PCT), 1)
    tp_price = round(entry_premium * (1 + TP_PCT), 1)

    if signal == "CALL":
        sl_hit = nf_low  <= nf_open - sl_pts
        tp_hit = nf_high >= nf_open + tp_pts
    else:  # PUT
        sl_hit = nf_high >= nf_open + sl_pts
        tp_hit = nf_low  <= nf_open - tp_pts

    if tp_hit and sl_hit:
        # Both triggered intraday — conservative: assume SL hit first
        outcome = "SL (both hit)"
        exit_price = sl_price
    elif tp_hit:
        outcome = "TP ✓"
        exit_price = tp_price
    elif sl_hit:
        outcome = "SL ✗"
        exit_price = sl_price
    else:
        # Held to EOD — estimate option price at close via delta approximation
        outcome = "EOD"
        delta = 0.5
        if signal == "CALL":
            approx_move = (nf_close - nf_open) * delta * 0.5
        else:
            approx_move = (nf_open - nf_close) * delta * 0.5
        exit_price = max(0.5, entry_premium + approx_move)

    pnl_per_lot = (exit_price - entry_premium) * LOT_SIZE
    total_pnl   = round(pnl_per_lot * lots, 0)

    trailing_jump = min(5, max(1, round(entry_premium * SL_PCT, 1)))

    return {
        "outcome":       outcome,
        "exit_price":    round(exit_price, 1),
        "sl_price":      sl_price,
        "tp_price":      tp_price,
        "sl_pts_needed": round(sl_pts, 0),
        "tp_pts_needed": round(tp_pts, 0),
        "total_pnl":     total_pnl,
        "trailing_jump": trailing_jump,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    today_dt = date.today()
    print(f"\n{'═'*60}")
    print(f"  ENSEMBLE REPLAY  —  {today_dt.strftime('%A %d %b %Y')}")
    print(f"{'═'*60}")

    # ── 1. Load today's actual trade from oracle intent ───────────────────────
    trade_path = f"{DATA_DIR}/today_trade.json"
    actual = {}
    if os.path.exists(trade_path):
        with open(trade_path) as f:
            actual = json.load(f)

    actual_signal  = str(actual.get("signal", "?")).upper()
    actual_premium = float(actual.get("oracle_premium", 0) or 0)
    actual_lots    = int(actual.get("lots", 1) or 1)
    actual_score   = actual.get("signal_score", "?")
    actual_ml_conf = actual.get("ml_conf", "?")
    actual_sl      = float(actual.get("sl_price", 0) or 0)
    actual_tp      = float(actual.get("tp_price", 0) or 0)
    actual_strike  = actual.get("strike", "?")
    actual_expiry  = actual.get("expiry", "?")

    print(f"\n  ACTUAL TRADE (from today_trade.json)")
    print(f"  ─────────────────────────────────────")
    if actual_signal != "?":
        print(f"  Signal   : {actual_signal}  (score {actual_score:+d}/4)")
        print(f"  Strike   : {actual_strike}  Expiry {actual_expiry}")
        print(f"  Lots     : {actual_lots} × {LOT_SIZE} = {actual_lots * LOT_SIZE} qty")
        print(f"  Premium  : ₹{actual_premium:.0f}")
        print(f"  SL       : ₹{actual_sl:.0f}   TP : ₹{actual_tp:.0f}")
        if actual_ml_conf != "?":
            print(f"  ML conf  : {float(actual_ml_conf):.0%}  (was single-champion, pre-ensemble)")
    else:
        print(f"  ⚠  today_trade.json not found — was a trade placed today?")

    # ── 2. Load today's Nifty50 OHLCV ──────────────────────────────────────
    today_ts  = pd.Timestamp(today_dt)
    nf_open = nf_high = nf_low = nf_close = None
    using_date = today_dt
    data_source = "CSV"

    nf_path = f"{DATA_DIR}/nifty50.csv"
    if os.path.exists(nf_path):
        nf_df = pd.read_csv(nf_path, parse_dates=["date"])
        row   = nf_df[nf_df["date"] == today_ts]
        if not row.empty:
            nf_open  = float(row.iloc[0]["open"])
            nf_high  = float(row.iloc[0]["high"])
            nf_low   = float(row.iloc[0]["low"])
            nf_close = float(row.iloc[0]["close"])

    if nf_open is None:
        # CSV stale (e.g. post-holiday, market still open) — auto-fetch from yfinance
        print(f"\n  ⏳  Today's NF row not in CSV — fetching from yfinance (^NSEI)...")
        try:
            import yfinance as yf
            from datetime import timedelta
            yf_end = today_dt + timedelta(days=1)
            yf_df  = yf.download("^NSEI", start=str(today_dt), end=str(yf_end),
                                 progress=False, auto_adjust=True)
            if not yf_df.empty:
                yf_df = yf_df.reset_index()
                if isinstance(yf_df.columns, pd.MultiIndex):
                    yf_df.columns = yf_df.columns.get_level_values(0)
                yf_df.columns = [c.lower() for c in yf_df.columns]
                r = yf_df.iloc[0]
                nf_open  = float(r["open"])
                nf_high  = float(r["high"])
                nf_low   = float(r["low"])
                nf_close = float(r["close"])
                data_source = "yfinance ^NSEI"
                print(f"  ✓  Live data fetched from yfinance")
            else:
                print(f"  ⚠  yfinance returned no data — market may still be open or holiday")
        except Exception as e:
            print(f"  ⚠  yfinance fetch failed: {e}")

    if nf_open is None:
        print(f"\n  ❌  No NF OHLCV available for today — run after market close")
        return

    day_move  = (nf_close - nf_open) / nf_open * 100
    day_range = nf_high - nf_low

    print(f"\n  NIFTY50  {using_date}  [{data_source}]")
    print(f"  ─────────────────────────────────────")
    print(f"  Open  : {nf_open:>8,.0f}")
    print(f"  High  : {nf_high:>8,.0f}  (+{nf_high - nf_open:,.0f} pts)")
    print(f"  Low   : {nf_low:>8,.0f}  ({nf_low - nf_open:,.0f} pts)")
    print(f"  Close : {nf_close:>8,.0f}")
    print(f"  Day   : {day_move:+.2f}%   Range: {day_range:.0f} pts")

    # ── 3. Ensemble prediction ────────────────────────────────────────────────
    print(f"\n  ENSEMBLE PREDICTION (RF + XGB + LGB majority vote)")
    print(f"  ─────────────────────────────────────")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ml_engine import get_today_features, FEATURE_COLS

    members = load_ensemble()
    if not members:
        print("  ❌ Ensemble models not found — run model_evolver.py first")
        return

    votes  = []
    confs  = []
    pcalls = []

    for mtype, model, meta in members:
        fc    = meta.get("feature_cols", FEATURE_COLS)
        X_t   = get_today_features(fc)
        if X_t is None or len(X_t) == 0:
            print(f"  [{mtype.upper():3}]  Could not build features")
            continue

        proba   = model.predict_proba(X_t)[0]
        classes = list(model.classes_)
        pc  = float(proba[classes.index(1)]) if 1 in classes else 0.5
        pp  = float(proba[classes.index(0)]) if 0 in classes else 0.5
        direction = "CALL" if pc >= pp else "PUT"
        conf = max(pc, pp)
        votes.append(direction)
        confs.append(conf)
        pcalls.append(pc)

        if actual_signal not in ("?", ""):
            match = "✓" if direction == actual_signal else "✗"
        else:
            match = " "
        print(f"  [{mtype.upper():3}]  {MTYPE_NAMES[mtype]:<14}  "
              f"P(CALL)={pc:.3f}  P(PUT)={pp:.3f}  → {direction}  ({conf:.0%})  {match}")

    if not votes:
        print("  ❌ No model produced output")
        return

    call_v = votes.count("CALL")
    put_v  = votes.count("PUT")
    ens_signal = "CALL" if call_v >= put_v else "PUT"
    agreed_confs = [c for v, c in zip(votes, confs) if v == ens_signal]
    ens_conf = sum(agreed_confs) / len(agreed_confs) if agreed_confs else 0.5
    avg_pcall = sum(pcalls) / len(pcalls)

    print(f"\n  Vote   : {call_v}/3 CALL  ·  {put_v}/3 PUT")
    print(f"  Result : {ens_signal}  (avg agreed conf {ens_conf:.0%})")

    if actual_signal not in ("?", ""):
        if ens_signal == actual_signal:
            verdict = "✅  SAME as actual trade — ensemble agrees"
        else:
            verdict = f"⚡  DIFFERENT — actual was {actual_signal}, ensemble says {ens_signal}"
        print(f"  {verdict}")

    # ── 4. P&L simulation for both scenarios ─────────────────────────────────
    if actual_premium > 0:
        print(f"\n  P&L SIMULATION  (premium ₹{actual_premium:.0f},  {actual_lots} lot{'s' if actual_lots > 1 else ''})")
        print(f"  ─────────────────────────────────────")

        # Scenario A: what was actually traded
        sim_actual = simulate_trade(
            actual_signal, actual_premium,
            nf_open, nf_high, nf_low, nf_close,
            actual_lots
        )
        sign_a = "+" if sim_actual["total_pnl"] >= 0 else ""
        print(f"  ACTUAL   [{actual_signal:4}]  Outcome: {sim_actual['outcome']:<14}  "
              f"P&L: {sign_a}₹{sim_actual['total_pnl']:,.0f}")

        # Scenario B: what ensemble would have done
        if ens_signal != actual_signal:
            sim_ens = simulate_trade(
                ens_signal, actual_premium,
                nf_open, nf_high, nf_low, nf_close,
                actual_lots
            )
            sign_e = "+" if sim_ens["total_pnl"] >= 0 else ""
            print(f"  ENSEMBLE [{ens_signal:4}]  Outcome: {sim_ens['outcome']:<14}  "
                  f"P&L: {sign_e}₹{sim_ens['total_pnl']:,.0f}")

            diff = sim_ens["total_pnl"] - sim_actual["total_pnl"]
            sign_d = "+" if diff >= 0 else ""
            outcome_label = "ensemble would have been BETTER" if diff > 0 else "ensemble would have been WORSE"
            print(f"\n  Swing    : {sign_d}₹{diff:,.0f}  ({outcome_label})")
        else:
            sim_ens = sim_actual
            print(f"  ENSEMBLE  Same direction → same outcome")

        # ── 5. Trade parameters breakdown ────────────────────────────────────
        ref = sim_actual  # use actual scenario for parameter display
        print(f"\n  TRADE PARAMETERS  ({actual_signal}  @  ₹{actual_premium:.0f})")
        print(f"  ─────────────────────────────────────")
        print(f"  Entry premium  : ₹{actual_premium:.0f}")
        print(f"  SL price       : ₹{ref['sl_price']:.0f}  (−{SL_PCT*100:.0f}%  of premium)")
        print(f"  TP price       : ₹{ref['tp_price']:.0f}  (+{TP_PCT*100:.0f}%  of premium,  RR {RR}×)")
        print(f"  Trailing jump  : ₹{ref['trailing_jump']:.1f}  (Super Order trail increment)")
        print(f"  ─────────────────────────────────────")
        print(f"  NF pts to SL   : {ref['sl_pts_needed']:.0f} pts  {'against' if actual_signal=='CALL' else 'in favour'}")
        print(f"  NF pts to TP   : {ref['tp_pts_needed']:.0f} pts  {'in favour' if actual_signal=='CALL' else 'against'}")
        print(f"  Actual NF drop : {nf_open - nf_low:.0f} pts (low vs open)"
              if actual_signal == "CALL" else
              f"  Actual NF rise : {nf_high - nf_open:.0f} pts (high vs open)")
        print(f"  ─────────────────────────────────────")
        print(f"  Max loss  1 lot: ₹{SL_PCT * actual_premium * LOT_SIZE:,.0f}")
        print(f"  Max gain  1 lot: ₹{TP_PCT * actual_premium * LOT_SIZE:,.0f}")
        print(f"  Max loss  total: ₹{SL_PCT * actual_premium * LOT_SIZE * actual_lots:,.0f}")
        print(f"  Max gain  total: ₹{TP_PCT * actual_premium * LOT_SIZE * actual_lots:,.0f}")

        # ── 6. What actually moved vs what was needed ─────────────────────────
        print(f"\n  NF MOVEMENT vs REQUIRED")
        print(f"  ─────────────────────────────────────")
        if actual_signal == "CALL":
            adverse_move  = nf_open - nf_low    # how far down it went
            favourable    = nf_high - nf_open   # how far up
            print(f"  Max adverse  (down): {adverse_move:>5.0f} pts  "
                  f"vs SL trigger {ref['sl_pts_needed']:.0f} pts  "
                  f"→ {'SL HIT ✗' if adverse_move >= ref['sl_pts_needed'] else 'SL clear ✓'}")
            print(f"  Max favourable (up): {favourable:>5.0f} pts  "
                  f"vs TP trigger {ref['tp_pts_needed']:.0f} pts  "
                  f"→ {'TP HIT ✓' if favourable >= ref['tp_pts_needed'] else 'TP not reached'}")
        else:  # PUT
            adverse_move  = nf_high - nf_open   # how far up (bad for PUT)
            favourable    = nf_open - nf_low    # how far down (good for PUT)
            print(f"  Max adverse   (up): {adverse_move:>5.0f} pts  "
                  f"vs SL trigger {ref['sl_pts_needed']:.0f} pts  "
                  f"→ {'SL HIT ✗' if adverse_move >= ref['sl_pts_needed'] else 'SL clear ✓'}")
            print(f"  Max favourable (dn): {favourable:>5.0f} pts  "
                  f"vs TP trigger {ref['tp_pts_needed']:.0f} pts  "
                  f"→ {'TP HIT ✓' if favourable >= ref['tp_pts_needed'] else 'TP not reached'}")

    # ── 7. Signals comparison ─────────────────────────────────────────────────
    print(f"\n  SIGNALS SNAPSHOT")
    print(f"  ─────────────────────────────────────")
    sig_csv = f"{DATA_DIR}/signals_ml.csv"
    if os.path.exists(sig_csv):
        sdf = pd.read_csv(sig_csv, parse_dates=["date"])
        row_sig = sdf[sdf["date"] == today_ts]
        if row_sig.empty:
            row_sig = sdf.iloc[[-1]]
        if not row_sig.empty:
            r = row_sig.iloc[0]
            for field in ["rule_score", "rule_signal", "ml_signal", "ml_conf", "signal",
                          "s_ema20", "s_trend5", "s_vix", "s_nf_gap"]:
                if field in r.index:
                    print(f"  {field:<16}: {r[field]}")

    print(f"\n{'═'*60}\n")


if __name__ == "__main__":
    main()
