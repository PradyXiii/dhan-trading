#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
system_health.py — Daily system evolution report.

Reads:
  data/paper_performance.csv     — daily ML composite scores
  data/live_ic_trades.csv        — IC live trade outcomes
  data/live_spread_trades.csv    — Bull Put / Bear Call live trade outcomes
  data/live_straddle_trades.csv  — Straddle live trade outcomes (when capital ≥ ₹2.3L)
  data/experiment_history.json   — every autoresearch experiment (kept + discarded)
  models/champion_meta.json      — current champion model accuracy + feature count

Sends:
  Plain-English Telegram report — composite trend, live WR, P&L, recent research.

Cron: 7:00 AM IST (1:30 UTC) daily.
"""

import os
import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import notify

_HERE   = Path(__file__).parent
_DATA   = _HERE / "data"
_MODELS = _HERE / "models"
_IST    = timezone(timedelta(hours=5, minutes=30))


# ─── helpers ─────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _avg(vals) -> float | None:
    nums = []
    for v in vals:
        if v in (None, "", "nan", "NaN"):
            continue
        try:
            nums.append(float(v))
        except (ValueError, TypeError):
            continue
    return sum(nums) / len(nums) if nums else None


def _pnl_value(row: dict) -> float:
    """Extract P&L from a trade row — different files use different column names."""
    for key in ("pnl_inr", "actual_pnl", "net_pnl"):
        if key in row and row[key] not in (None, ""):
            try:
                return float(row[key])
            except (ValueError, TypeError):
                continue
    return 0.0


def _is_closed(row: dict) -> bool:
    """Trade is closed if exit_reason is set and not OPEN."""
    er = (row.get("exit_reason") or "").strip().upper()
    return er not in ("", "OPEN", "PENDING")


def _wr_and_pnl(rows: list[dict]) -> tuple[float | None, int, float, int]:
    """Win rate %, closed trade count, total P&L, open positions count.
    Only closed trades count toward WR + P&L.
    """
    if not rows:
        return None, 0, 0.0, 0
    closed = [r for r in rows if _is_closed(r)]
    open_n = len(rows) - len(closed)
    if not closed:
        return None, 0, 0.0, open_n
    pnls = [_pnl_value(r) for r in closed]
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls) * 100, len(pnls), sum(pnls), open_n


def _trend_arrow(today: float | None, baseline: float | None) -> str:
    if today is None or baseline is None:
        return ""
    if today > baseline * 1.02: return " ↑"
    if today < baseline * 0.98: return " ↓"
    return " →"


def _fmt_score(v):  return f"{v:.4f}" if v is not None else "—"
def _fmt_pct(v):    return f"{v:.1f}%" if v is not None else "—"
def _fmt_money(v):  return f"₹{v:,.0f}" if v is not None else "—"


# ─── main report builder ─────────────────────────────────────────────────────

def build_report() -> str:
    today_str = datetime.now(_IST).strftime("%Y-%m-%d (%a)")

    # 1. ML composite trend (paper_performance.csv)
    pp = _read_csv(_DATA / "paper_performance.csv")
    today_score = float(pp[-1]["live_score"]) if pp and pp[-1].get("live_score") else None
    avg7  = _avg([r.get("live_score") for r in pp[-7:]])
    avg30 = _avg([r.get("live_score") for r in pp[-30:]])

    # Combined advantage (paper model lead vs live model)
    combined_adv_today = None
    if pp and pp[-1].get("combined_advantage"):
        try:
            combined_adv_today = float(pp[-1]["combined_advantage"])
        except (ValueError, TypeError):
            pass

    # 2. Champion model
    champ = _read_json(_MODELS / "champion_meta.json", {}) or {}
    champ_type = champ.get("model_type", "—")
    champ_acc  = champ.get("accuracy")
    champ_n    = champ.get("n_features", "—")
    # Total features in pipeline (vs champion's selected count)
    try:
        from ml_engine import FEATURE_COLS
        total_features = len(FEATURE_COLS)
    except Exception:
        total_features = "—"

    # 3. Live trades — pool all 3 CSVs (IC, spreads, straddle)
    all_trades = []
    for csv_name in ("live_ic_trades.csv", "live_spread_trades.csv", "live_straddle_trades.csv"):
        all_trades.extend(_read_csv(_DATA / csv_name))
    # Sort by date
    all_trades.sort(key=lambda r: r.get("date", ""))

    wr5,  n5,  _,        _      = _wr_and_pnl(all_trades[-5:])
    wr30, n30, pnl30,    _      = _wr_and_pnl(all_trades[-30:])
    wr_all, n_all, pnl_all, open_n = _wr_and_pnl(all_trades)

    # 4. Recent experiments
    exp_history = _read_json(_DATA / "experiment_history.json", []) or []
    recent_kept     = [e for e in exp_history if e.get("kept")]
    recent_discarded= [e for e in exp_history if not e.get("kept")]
    last_kept = recent_kept[-1] if recent_kept else None
    last_disc = recent_discarded[-1] if recent_discarded else None

    # Last 30-day kept count (research velocity)
    cutoff = (datetime.now(_IST) - timedelta(days=30)).strftime("%Y-%m-%d")
    kept_30d = sum(1 for e in recent_kept if e.get("date", "") >= cutoff)
    disc_30d = sum(1 for e in recent_discarded if e.get("date", "") >= cutoff)

    # 5. Verdict — direction of system
    verdict = "→ Stable"
    if today_score is not None and avg30 is not None:
        if today_score > avg30 * 1.02:
            verdict = "↑ <b>Improving</b> — composite up vs 30-day avg"
        elif today_score < avg30 * 0.98:
            verdict = "↓ <b>Weakening</b> — composite below 30-day avg"
        else:
            verdict = "→ <b>Stable</b> — composite tracking 30-day avg"

    # Format kept feature description (truncate if long)
    def _exp_desc(e):
        if not e: return "—"
        d = e.get("description", "—")
        return d[:70] + "…" if len(d) > 70 else d

    # ─── compose Telegram message (HTML parse mode) ──────────────────────────
    msg = f"""📊 <b>NF System Health — {today_str}</b>

<b>ML Composite Score</b> (paper experiment baseline)
  Today:      {_fmt_score(today_score)}
  7-day avg:  {_fmt_score(avg7)}{_trend_arrow(today_score, avg7)}
  30-day avg: {_fmt_score(avg30)}{_trend_arrow(today_score, avg30)}

<b>Champion Model</b> (predicts tomorrow's signal)
  Type:     {champ_type}
  Accuracy: {_fmt_pct(champ_acc * 100 if champ_acc else None)}
  Features: {champ_n} selected of {total_features} total

<b>Live Trades</b> (closed positions only)
  Last 5:   {_fmt_pct(wr5)}  ({n5} closed)
  Last 30:  {_fmt_pct(wr30)} ({n30} closed, {_fmt_money(pnl30)})
  Lifetime: {_fmt_pct(wr_all)} ({n_all} closed, {_fmt_money(pnl_all)})
  Open positions: {open_n}

<b>Research (last 30 days)</b>
  Features kept:      {kept_30d}
  Features discarded: {disc_30d}
  Last kept:      {_exp_desc(last_kept)}
  Last discarded: {_exp_desc(last_disc)}

<b>Verdict:</b> {verdict}"""

    return msg


# ─── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    msg = build_report()
    notify.send(msg)
