#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
forecast_pnl.py — Daily 5 PM FY P&L forecast → Telegram.

3-layer model:
  Layer 1 — EWMA WR + avg P&L (span=20; last 20 trades carry most weight)
  Layer 2 — Bootstrap 10,000 scenarios of remaining trading days → P10/P50/P90
  Layer 3 — VIX regime multiplier (high VIX compresses expected P&L by up to 12%)

Usage:
    python3 forecast_pnl.py              # live send
    python3 forecast_pnl.py --dry-run   # compute + print, no Telegram

Cron: 30 11 * * 1-5  (11:30 UTC = 5:00 PM IST, Mon-Fri)
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import date, timedelta, datetime, timezone

import notify

_IST = timezone(timedelta(hours=5, minutes=30))
_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Data files ────────────────────────────────────────────────────────────────
DATA_DIR     = os.path.join(_HERE, "data")
IC_CSV       = os.path.join(DATA_DIR, "live_ic_trades.csv")
SPREAD_CSV   = os.path.join(DATA_DIR, "live_spread_trades.csv")
STRADDLE_CSV = os.path.join(DATA_DIR, "live_straddle_trades.csv")
VIX_CSV      = os.path.join(DATA_DIR, "india_vix.csv")

FY_END = date(2027, 3, 31)  # projection target

# ── NSE holidays (2026 confirmed + 2027 tentative — update Dec 2026) ─────────
NSE_HOLIDAYS: set = {
    # 2026
    date(2026, 1, 26), date(2026, 2, 19), date(2026, 3, 20),
    date(2026, 4,  3), date(2026, 4,  6), date(2026, 4, 14),
    date(2026, 5,  1), date(2026, 6, 27), date(2026, 8, 15),
    date(2026, 8, 27), date(2026, 10, 2), date(2026, 10, 21),
    date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 24),
    date(2026, 12, 25),
    # 2027 tentative (moon-based dates marked ~)
    date(2027, 1, 26),  # Republic Day
    date(2027, 3, 26),  # Holi (~ based on calendar)
}


def _ist_today() -> date:
    return datetime.now(_IST).date()


def _count_trading_days(from_date: date, to_date: date) -> int:
    count = 0
    d = from_date
    while d <= to_date:
        if d.weekday() < 5 and d not in NSE_HOLIDAYS:
            count += 1
        d += timedelta(days=1)
    return count


def _load_trades() -> pd.DataFrame:
    """Merge all 3 live trade CSVs into a single date-sorted frame."""
    frames = []
    for path in (IC_CSV, SPREAD_CSV, STRADDLE_CSV):
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            if "pnl_inr" not in df.columns or df.empty:
                continue
            df["pnl_inr"] = pd.to_numeric(df["pnl_inr"], errors="coerce")
            keep_cols = [c for c in ("date", "pnl_inr", "strategy") if c in df.columns]
            frames.append(df[keep_cols].dropna(subset=["pnl_inr"]))
        except Exception:
            pass

    if not frames:
        return pd.DataFrame(columns=["date", "pnl_inr"])

    merged = pd.concat(frames, ignore_index=True)
    if "date" in merged.columns:
        merged["date"] = pd.to_datetime(merged["date"], errors="coerce")
        merged = merged.sort_values("date")
    return merged


def _ewma_stats(pnls: np.ndarray, span: int = 20) -> tuple[float, float]:
    """EWMA-weighted avg P&L and WR. Most recent trade = highest weight."""
    n = len(pnls)
    if n == 0:
        return 0.0, 0.0
    alpha = 2.0 / (span + 1)
    weights = np.array([(1 - alpha) ** (n - 1 - i) for i in range(n)])
    weights /= weights.sum()
    ewma_avg = float(np.dot(weights, pnls))
    ewma_wr  = float(np.dot(weights, (pnls > 0).astype(float)))
    return ewma_avg, ewma_wr


def _bootstrap_projection(pnls: np.ndarray, n_trades: int,
                           n_sim: int = 10_000) -> tuple[float, float, float]:
    """
    10,000 bootstrap scenarios of n_trades drawn from pnls (with replacement).
    Returns (p10_total, p50_total, p90_total).
    """
    if len(pnls) == 0 or n_trades <= 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(42)
    samples = rng.choice(pnls, size=(n_sim, n_trades), replace=True)
    totals = samples.sum(axis=1)
    return (
        float(np.percentile(totals, 10)),
        float(np.percentile(totals, 50)),
        float(np.percentile(totals, 90)),
    )


def _vix_regime() -> tuple[float, str, float | None]:
    """
    Read last VIX close from india_vix.csv (column: close, lowercased by data_fetcher).
    Returns (multiplier, label, vix_value).
    Multiplier is applied to the bootstrap P50 to account for regime risk.
    """
    if not os.path.exists(VIX_CSV):
        return 1.0, "VIX unavailable", None
    try:
        df = pd.read_csv(VIX_CSV)
        df.columns = [c.lower() for c in df.columns]
        close_col = next(
            (c for c in df.columns if c in ("close", "^indiavix", "vix", "adj close")),
            None
        )
        if close_col is None:
            return 1.0, "VIX column not found", None
        series = pd.to_numeric(df[close_col], errors="coerce").dropna()
        if series.empty:
            return 1.0, "VIX data empty", None
        vix = float(series.iloc[-1])

        if vix >= 22:
            return 0.88, f"🔴 HIGH VIX {vix:.1f} — cautious (-12% on projection)", vix
        elif vix >= 18:
            return 0.92, f"🟡 ELEVATED VIX {vix:.1f} (-8% on projection)", vix
        elif vix <= 13:
            return 1.05, f"🟢 LOW VIX {vix:.1f} — calm (+5% on projection)", vix
        else:
            return 1.0, f"🟢 NORMAL VIX {vix:.1f}", vix
    except Exception as e:
        return 1.0, f"VIX read error: {e}", None


def _fmt_inr(val: float) -> str:
    sign = "-" if val < 0 else ""
    v = abs(val)
    if v >= 100_000:
        return f"{sign}₹{v / 100_000:.2f}L"
    elif v >= 1_000:
        return f"{sign}₹{v / 1_000:.1f}K"
    return f"{sign}₹{v:.0f}"


def run(dry_run: bool = False) -> None:
    today   = _ist_today()
    df      = _load_trades()
    n_total = len(df)

    if n_total == 0:
        msg = (
            "📈 <b>Season Forecast</b> · No live trades yet.\n"
            "Forecast will appear once trading begins."
        )
        notify.send(msg, silent=dry_run)
        return

    pnls = df["pnl_inr"].values

    # Layer 1 — EWMA stats
    ewma_avg, ewma_wr = _ewma_stats(pnls, span=20)
    last_n  = pnls[-20:] if len(pnls) >= 20 else pnls
    last_wr = int(round((last_n > 0).mean() * 100))
    last_avg = float(last_n.mean())

    # Layer 2 — bootstrap
    days_left   = _count_trading_days(today + timedelta(days=1), FY_END)
    trades_left = days_left  # 1 trade per trading day
    p10, p50, p90 = _bootstrap_projection(pnls, trades_left)

    # Layer 3 — VIX regime multiplier
    vix_mult, regime_label, _vix_val = _vix_regime()
    adj_p50 = p50 * vix_mult

    ytd_pnl     = float(pnls.sum())
    fy_mid      = ytd_pnl + adj_p50
    fy_optimistic   = ytd_pnl + p90
    fy_conservative = ytd_pnl + p10

    # Confidence caveat when sample is thin
    caveat = ""
    if n_total < 15:
        caveat = f"\n⚠️ Only {n_total} trades — projections widen as sample grows (need ≥20 for stability)"

    msg = (
        f"📈 <b>Season Forecast</b> · {today.strftime('%d %b %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades this season: <b>{n_total}</b>  ·  WR (last 20): <b>{last_wr}%</b>\n"
        f"Avg P&amp;L/trade (EWMA): <b>{_fmt_inr(ewma_avg)}</b>\n"
        f"Trading days left to 31 Mar 2027: <b>{days_left}</b>\n"
        f"\n"
        f"<b>Bootstrap projections ({trades_left} trades remaining):</b>\n"
        f"  Optimistic  (top 10%):  <b>{_fmt_inr(fy_optimistic)}</b>\n"
        f"  Mid (50th %):           <b>{_fmt_inr(ytd_pnl + p50)}</b>\n"
        f"  Conservative (10th %): <b>{_fmt_inr(fy_conservative)}</b>\n"
        f"\n"
        f"<b>Regime-adjusted mid: {_fmt_inr(fy_mid)}</b>\n"
        f"  {regime_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"YTD banked: <b>{_fmt_inr(ytd_pnl)}</b>"
        f"{caveat}"
    )

    notify.send(msg, silent=dry_run)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
