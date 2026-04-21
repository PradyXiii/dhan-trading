#!/usr/bin/env python3
# ─── REAL-OPTIONS RULE (April 2026) ──────────────────────────────────────────
# This evaluator uses OHLCV-formula premium — QUICK SANITY CHECK ONLY.
# Any SL_PCT / RR change that looks good here MUST be re-tested with
# `python3 backtest_engine.py --real-options` before promotion. OHLCV ignores
# theta decay + IV compression + slippage. See "REAL-OPTIONS RULE" in CLAUDE.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
autoexperiment_backtest.py — Backtest evaluator for auto_trader.py constant changes.

Reads SL_PCT and RR from auto_trader.py via regex (no import — auto_trader.py has
import-time side effects: _acquire_lock(), API client setup, etc.).

Runs backtest_engine.run_backtest() with the current SL_PCT and RR values, computes
a composite metric on a 0–1 scale, and prints a single JSON line.

Used by: autoloop_nf.py (routes here when proposed change targets auto_trader.py)

Usage:
    python3 autoexperiment_backtest.py
    → {"composite": 0.534, "pnl_proxy": 0.534, "n_val": 430, "n_train": 0,
       "win_rate": 0.534, "max_dd_pct": -18.2, "sl_pct": 0.15, "rr": 2.5}

Composite formula:
    0.70 × win_rate + 0.30 × drawdown_score
    where drawdown_score = max(0, min(1, 1 + max_dd_pct/50))
    i.e., -50% drawdown → 0.0, 0% drawdown → 1.0
"""

import io
import json
import os
import re
import sys
import warnings
from contextlib import redirect_stdout

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning")

# Approximate typical Nifty50 ATM option premium (used for trailing jump calc)
_TYPICAL_PREMIUM = 900.0


def _parse_float(pattern: str, text: str, default: float) -> float:
    """Extract a float from auto_trader.py source text via regex."""
    m = re.search(pattern, text, re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def _read_trader_constants() -> dict:
    """Parse SL_PCT and RR from auto_trader.py without importing it."""
    try:
        with open("auto_trader.py", "r") as f:
            src = f.read()
    except FileNotFoundError:
        return {"SL_PCT": 0.15, "RR": 2.5}
    return {
        "SL_PCT": _parse_float(r"^SL_PCT\s*=\s*([0-9.]+)", src, 0.15),
        "RR":     _parse_float(r"^RR\s*=\s*([0-9.]+)", src, 2.5),
    }


def run():
    constants  = _read_trader_constants()
    SL_PCT     = constants["SL_PCT"]
    RR         = constants["RR"]

    # Trailing jump mirrors the fix in auto_trader.py: max(1, round(premium * SL_PCT, 0))
    # Use a typical premium for evaluation (the backtest uses the same formula per trade).
    trail_jump = int(max(1, round(_TYPICAL_PREMIUM * SL_PCT)))

    try:
        import backtest_engine as bt
    except ImportError as e:
        print(json.dumps({"error": f"cannot import backtest_engine: {e}", "composite": 0.0}))
        sys.exit(1)

    # Suppress backtest's internal print output — we only emit the final JSON line.
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            trade_df, _ = bt.run_backtest(
                trail_jump_opt=trail_jump,
                sl_pct=SL_PCT,
                flat_rr=RR,
                use_actual_dte=True,
                ml=True,
                use_real_premiums=True,
            )
    except Exception as e:
        print(json.dumps({"error": f"run_backtest failed: {e}", "composite": 0.0}))
        sys.exit(1)

    # Active trades: WIN / LOSS / TRAIL_SL / PARTIAL
    active = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])]
    if len(active) < 50:
        print(json.dumps({
            "error": f"only {len(active)} active trades — need at least 50",
            "composite": 0.0,
        }))
        sys.exit(1)

    wins     = (active["result"] == "WIN").sum()
    losses   = (active["result"] == "LOSS").sum()
    total    = len(active)
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    # Max drawdown from the capital curve (negative %)
    cap_series  = trade_df["capital_after"]
    rolling_max = cap_series.cummax()
    max_dd_pct  = ((cap_series - rolling_max) / rolling_max * 100).min()

    # Composite: 70% win_rate + 30% drawdown score
    # drawdown_score: -50% → 0.0, -25% → 0.5, 0% → 1.0 (linear)
    dd_score  = max(0.0, min(1.0, 1.0 + max_dd_pct / 50.0))
    composite = round(0.70 * win_rate + 0.30 * dd_score, 4)

    print(json.dumps({
        "composite":  composite,
        "pnl_proxy":  round(win_rate, 4),
        "n_val":      total,
        "n_train":    0,        # no train/val split — backtest covers full history
        "win_rate":   round(win_rate, 4),
        "max_dd_pct": round(float(max_dd_pct), 2),
        "sl_pct":     SL_PCT,
        "rr":         RR,
    }))


if __name__ == "__main__":
    run()
