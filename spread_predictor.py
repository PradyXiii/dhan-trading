#!/usr/bin/env python3
"""
spread_predictor.py — Second-order ML: predict spread win/loss BEFORE trading.

Why this exists:
  The main ML (ml_engine.py) predicts DIRECTION (CALL/PUT).
  The credit spread bets AGAINST the direction (fade strategy) — it wins when
  the daily move is smaller than the premium implies.
  Direction accuracy ≠ spread win rate.

  This model asks a different question: given today's 63 features, will the
  credit spread be PROFITABLE by EOD?

  Labels come from backtest P&L (real 1-min option data), so they represent
  actual outcomes on the specific spread positions the system would place.

Architecture:
  1. Load backtest trade logs (bc_opt.csv + bp_opt.csv from optimize_params.py)
  2. Join on date → match with 63 features from ml_engine.compute_features()
  3. Train binary classifier (win=1 if pnl>0) using RF + temporal walk-forward
  4. Evaluate: precision, recall, WR improvement when filter applied
  5. Save model → models/spread_predictor_bc.pkl + models/spread_predictor_bp.pkl

Usage:
    # Step 1: generate enriched trade logs
    python3 backtest_spreads.py --strategy bear_call_credit --ml --save /tmp/bc_opt.csv
    python3 backtest_spreads.py --strategy bull_put_credit  --ml --save /tmp/bp_opt.csv

    # Step 2: train + evaluate
    python3 spread_predictor.py --train

    # Step 3: evaluate optimal confidence threshold
    python3 spread_predictor.py --eval --threshold 0.65

    # Step 4: backtest WITH predictor filter
    python3 spread_predictor.py --backtest

    # Check today's spread probability (used by auto_trader.py)
    python3 spread_predictor.py --predict-today CALL
"""
import os, sys, pickle, argparse
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import classification_report, roc_auc_score

sys.path.insert(0, os.path.dirname(__file__))
import ml_engine
from backtest_spreads import run_spread_backtest, _DATA_CACHE

DATA_DIR    = "data"
MODELS_DIR  = "models"
TRADE_LOGS  = {
    "bear_call_credit": "/tmp/bc_opt.csv",
    "bull_put_credit":  "/tmp/bp_opt.csv",
}
PREDICTOR_PATHS = {
    "bear_call_credit": f"{MODELS_DIR}/spread_predictor_bc.pkl",
    "bull_put_credit":  f"{MODELS_DIR}/spread_predictor_bp.pkl",
}
MIN_TRAIN_SAMPLES = 60    # need at least this many historical trades before predicting
WALK_FORWARD_WINDOW = 30  # re-train every 30 new trades (temporal validation)


# ── Feature loading ────────────────────────────────────────────────────────────

def load_features() -> pd.DataFrame:
    """Return DataFrame of (date, feature_1..feature_63) from ml_engine."""
    print("Loading features from ml_engine.compute_features()...")
    raw  = ml_engine.load_all_data()
    feat = ml_engine.compute_features(raw)
    feat["date"] = pd.to_datetime(feat["date"]).dt.date
    return feat


def load_trades(strategy: str) -> pd.DataFrame | None:
    path = TRADE_LOGS[strategy]
    if not os.path.exists(path):
        print(f"  Trade log missing: {path}")
        print(f"  Run: python3 backtest_spreads.py --strategy {strategy} --ml --save {path}")
        return None
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["result"].isin(["SL", "TP", "EOD"])].copy()
    df["win"] = (df["pnl"] > 0).astype(int)
    return df


def build_dataset(strategy: str, feat_df: pd.DataFrame):
    """Join trade outcomes with features. Returns (dates, X, y)."""
    trades = load_trades(strategy)
    if trades is None or len(trades) < MIN_TRAIN_SAMPLES:
        return None, None, None

    merged = trades.merge(feat_df, on="date", how="inner")
    if len(merged) < MIN_TRAIN_SAMPLES:
        print(f"  Only {len(merged)} matched rows after feature join (need {MIN_TRAIN_SAMPLES})")
        return None, None, None

    feat_cols = [c for c in ml_engine.FEATURE_COLS if c in merged.columns]
    merged = merged.sort_values("date").reset_index(drop=True)

    dates = merged["date"].values
    X     = merged[feat_cols].fillna(0).values.astype(np.float32)
    y     = merged["win"].values
    return dates, X, y


# ── Walk-forward training ──────────────────────────────────────────────────────

def walk_forward_eval(dates, X, y, min_train=MIN_TRAIN_SAMPLES):
    """
    Temporal walk-forward: train on past, predict future.
    Returns (predictions, probabilities) aligned with y.
    """
    n    = len(y)
    preds = np.full(n, -1, dtype=int)
    probs = np.full(n, 0.5)

    for i in range(min_train, n):
        X_tr, y_tr = X[:i], y[:i]
        if len(np.unique(y_tr)) < 2:
            continue
        clf = RandomForestClassifier(
            n_estimators=150, max_depth=6, min_samples_leaf=5,
            class_weight="balanced", random_state=42, n_jobs=-1,
        )
        clf.fit(X_tr, y_tr)
        preds[i] = clf.predict(X[i:i+1])[0]
        probs[i] = clf.predict_proba(X[i:i+1])[0][1]  # P(win)

    return preds, probs


# ── Full-data training (for deployment) ───────────────────────────────────────

def train_full(X, y) -> RandomForestClassifier:
    """Train on full dataset for deployment."""
    clf = RandomForestClassifier(
        n_estimators=300, max_depth=7, min_samples_leaf=4,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    clf.fit(X, y)
    return clf


# ── Evaluation helpers ────────────────────────────────────────────────────────

def eval_threshold(y_true, y_pred_proba, threshold=0.60):
    """
    Show WR, trade count, P&L when filtering by prob_win >= threshold.
    """
    mask     = y_pred_proba >= threshold
    n_total  = len(y_true)
    n_trade  = mask.sum()
    if n_trade == 0:
        return None
    wr_all      = y_true.mean()
    wr_filtered = y_true[mask].mean()
    return {
        "threshold": threshold,
        "n_trade":   int(n_trade),
        "n_skip":    n_total - int(n_trade),
        "wr_all":    wr_all,
        "wr_filt":   wr_filtered,
        "wr_delta":  wr_filtered - wr_all,
    }


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_train(args):
    feat_df = load_features()
    os.makedirs(MODELS_DIR, exist_ok=True)

    for strategy in STRATEGIES_ORDER:
        print(f"\n{'─'*60}")
        print(f"Strategy: {strategy}")

        dates, X, y = build_dataset(strategy, feat_df)
        if X is None:
            continue

        n = len(y)
        wr_base = y.mean()
        print(f"  Dataset: {n} trades  base WR={wr_base:.1%}")

        # Walk-forward eval
        print(f"  Walk-forward eval (train on first {MIN_TRAIN_SAMPLES}, predict rest)...")
        preds, probs = walk_forward_eval(dates, X, y)

        # Only evaluate on predicted (non -1) positions
        eval_mask = preds >= 0
        if eval_mask.sum() > 0:
            auc = roc_auc_score(y[eval_mask], probs[eval_mask])
            print(f"  AUC-ROC: {auc:.3f}  (> 0.55 = useful, > 0.65 = good)")

            print("\n  Threshold scan (walk-forward predictions):")
            print(f"  {'Thresh':>7}  {'Trades':>7}  {'Skip':>6}  {'WR':>7}  {'WR delta':>9}")
            for t in [0.50, 0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]:
                r = eval_threshold(y[eval_mask], probs[eval_mask], t)
                if r:
                    print(f"  {r['threshold']:>7.2f}  {r['n_trade']:>7}  "
                          f"{r['n_skip']:>6}  {r['wr_filt']:>7.1%}  "
                          f"{r['wr_delta']:>+9.1%}")

        # Train on full data and save
        print(f"\n  Training full model on {n} samples...")
        clf = train_full(X, y)

        feat_cols = [c for c in ml_engine.FEATURE_COLS if c in feat_df.columns]
        model_pkg = {
            "clf":        clf,
            "feat_cols":  feat_cols,
            "strategy":   strategy,
            "n_train":    n,
            "base_wr":    float(wr_base),
            "trained_at": pd.Timestamp.now().isoformat()[:19],
        }
        path = PREDICTOR_PATHS[strategy]
        with open(path, "wb") as f:
            pickle.dump(model_pkg, f)
        print(f"  Saved → {path}")

        # Top features
        importances = clf.feature_importances_
        feat_imp = sorted(zip(feat_cols, importances), key=lambda x: -x[1])
        print(f"\n  Top 10 spread-outcome features:")
        for fname, imp in feat_imp[:10]:
            print(f"    {fname:<30} {imp:.4f}")


def cmd_eval(args):
    """Re-run threshold scan from saved walk-forward results."""
    feat_df = load_features()
    threshold = args.threshold

    for strategy in STRATEGIES_ORDER:
        print(f"\n{strategy}")
        dates, X, y = build_dataset(strategy, feat_df)
        if X is None:
            continue
        _, probs = walk_forward_eval(dates, X, y)
        eval_mask = probs > 0
        r = eval_threshold(y[eval_mask], probs[eval_mask], threshold)
        if r:
            print(f"  Base WR:    {r['wr_all']:.1%}  ({len(y)} trades)")
            print(f"  Filtered:   {r['wr_filt']:.1%}  ({r['n_trade']} trades, "
                  f"{r['n_skip']} skipped) at threshold={threshold}")
            print(f"  WR gain:    {r['wr_delta']:+.1%}")


def cmd_backtest(args):
    """
    Full backtest WITH spread predictor filter applied.
    Shows P&L and WR vs baseline.
    """
    from backtest_spreads import print_spread_summary

    feat_df = load_features()

    for strategy in STRATEGIES_ORDER:
        path = PREDICTOR_PATHS[strategy]
        if not os.path.exists(path):
            print(f"  Model missing for {strategy} — run --train first")
            continue

        with open(path, "rb") as f:
            pkg = pickle.load(f)

        clf       = pkg["clf"]
        feat_cols = pkg["feat_cols"]

        # Run full backtest
        _DATA_CACHE.clear()
        df = run_spread_backtest(strategy, ml=True, adaptive=False)
        active = df[df["result"].isin(["SL", "TP", "EOD"])].copy()

        # Predict win probability for each trade
        merged = active.merge(feat_df, on="date", how="inner")
        if merged.empty:
            print(f"  No feature matches for {strategy}")
            continue

        X_pred = merged[[c for c in feat_cols if c in merged.columns]].fillna(0).values
        probs  = clf.predict_proba(X_pred)[:, 1]
        merged["spread_win_prob"] = probs

        # Baseline vs filtered
        base_wr  = (active["pnl"] > 0).mean()
        base_pnl = active["pnl"].sum()

        for thresh in [0.55, 0.60, 0.65]:
            filt = merged[merged["spread_win_prob"] >= thresh]
            if len(filt) < 20:
                continue
            wr  = (filt["pnl"] > 0).mean()
            pnl = filt["pnl"].sum()
            print(f"  {strategy}  thresh={thresh:.2f}:  "
                  f"trades={len(filt)}  WR={wr:.1%} ({wr-base_wr:+.1%})  "
                  f"P&L=₹{pnl/1e5:.2f}L ({(pnl-base_pnl)/1e5:+.2f}L vs base)")


def cmd_predict_today(args):
    """
    Return today's spread win probability for a given signal (CALL/PUT).
    Used by auto_trader.py as an optional filter.
    """
    signal   = args.signal.upper()
    strategy = "bear_call_credit" if signal == "CALL" else "bull_put_credit"
    path     = PREDICTOR_PATHS[strategy]

    if not os.path.exists(path):
        print(f"Model not found: {path} — run --train first")
        sys.exit(1)

    with open(path, "rb") as f:
        pkg = pickle.load(f)

    clf       = pkg["clf"]
    feat_cols = pkg["feat_cols"]

    # Get today's features
    from ml_engine import load_all_data, compute_features
    raw      = load_all_data()
    feat_df  = compute_features(raw)
    feat_df["date"] = pd.to_datetime(feat_df["date"]).dt.date
    today_dt = feat_df["date"].max()
    today_row = feat_df[feat_df["date"] == today_dt]

    if today_row.empty:
        print("Today's features unavailable")
        sys.exit(1)

    X = today_row[[c for c in feat_cols if c in today_row.columns]].fillna(0).values
    prob = float(clf.predict_proba(X)[0][1])
    print(f"Spread win probability ({signal} / {strategy}): {prob:.1%}")
    print(f"Base win rate at train time: {pkg['base_wr']:.1%}")
    if prob >= 0.65:
        print("Signal: STRONG — trade")
    elif prob >= 0.55:
        print("Signal: NEUTRAL — trade with caution")
    else:
        print("Signal: WEAK — consider skipping")
    return prob


# ── CLI ────────────────────────────────────────────────────────────────────────

STRATEGIES_ORDER = ["bear_call_credit", "bull_put_credit"]

def main():
    ap = argparse.ArgumentParser(description="Spread outcome predictor (second-order ML)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--train",    action="store_true",
                      help="Train walk-forward models + save to models/")
    mode.add_argument("--eval",     action="store_true",
                      help="Evaluate specific threshold on walk-forward predictions")
    mode.add_argument("--backtest", action="store_true",
                      help="Full backtest with predictor filter applied")
    mode.add_argument("--predict-today", metavar="SIGNAL",
                      help="Return win probability for today's trade (CALL or PUT)")
    ap.add_argument("--threshold", type=float, default=0.62,
                    help="Win probability threshold for --eval (default 0.62)")
    args = ap.parse_args()

    if args.train:
        cmd_train(args)
    elif args.eval:
        cmd_eval(args)
    elif args.backtest:
        cmd_backtest(args)
    elif args.predict_today:
        args.signal = args.predict_today
        cmd_predict_today(args)


if __name__ == "__main__":
    main()
