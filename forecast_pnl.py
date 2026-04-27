#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
forecast_pnl.py — Daily 5 PM FY P&L forecast → Telegram.

5-layer model:
  Layer 1 — Confidence-weighted EWMA (span=20; ml_conf >= 0.8 → 1.5× weight)
  Layer 2 — Bootstrap with compounding (lot count updates every 10 trades as capital grows)
  Layer 3 — VIX regime multiplier derived from real trade history (falls back to static)
  Layer 4 — Strategy-level breakdown (IC vs Bull Put vs Straddle — separate EWMAs)
  Layer 5 — Exit-reason regime shift warning (SL rate > 30% of last 15 trades = alert)

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

_IST  = timezone(timedelta(hours=5, minutes=30))
_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Data files ────────────────────────────────────────────────────────────────
DATA_DIR     = os.path.join(_HERE, "data")
IC_CSV       = os.path.join(DATA_DIR, "live_ic_trades.csv")
SPREAD_CSV   = os.path.join(DATA_DIR, "live_spread_trades.csv")
STRADDLE_CSV = os.path.join(DATA_DIR, "live_straddle_trades.csv")
VIX_CSV      = os.path.join(DATA_DIR, "india_vix.csv")

FY_END = date(2027, 3, 31)

# ── Capital / lot-sizing constants (mirrors auto_trader.py) ──────────────────
INITIAL_CAPITAL        = 112_370   # capital at go-live 22 Apr 2026
IC_MARGIN_PER_LOT      =  93_202   # actual Dhan SPAN for NF IC (1 lot)
STRADDLE_MARGIN_PER_LOT = 230_000  # straddle threshold; auto-upgrade in auto_trader.py
MAX_LOTS_IC            = 10
MAX_LOTS_STRADDLE      =  5
STRADDLE_SCALE_VS_IC   = 2.3       # straddle per-lot avg P&L / IC per-lot (from 5yr backtest)

# ── Static VIX multipliers — used when real bucket has < 5 trades ────────────
_VIX_STATIC_MULT = [
    (0,  13,  1.05, "calm market — boosting estimate by 5%"),
    (13, 18,  1.00, "normal market"),
    (18, 22,  0.92, "slightly nervous — cutting estimate by 8%"),
    (22, 999, 0.88, "very nervous — cutting estimate by 12%"),
]

_HISTORICAL_SL_RATE = 0.153   # 84.7% WR backtest → 15.3% SL
_SL_ALERT_THRESHOLD = 0.30    # flag when recent SL rate exceeds this
_SL_WINDOW          = 15

# ── NSE holidays (2026 confirmed + 2027 tentative — update Dec 2026) ─────────
NSE_HOLIDAYS: set = {
    date(2026, 1, 26), date(2026, 2, 19), date(2026, 3, 20),
    date(2026, 4,  3), date(2026, 4,  6), date(2026, 4, 14),
    date(2026, 5,  1), date(2026, 6, 27), date(2026, 8, 15),
    date(2026, 8, 27), date(2026, 10, 2), date(2026, 10, 21),
    date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 24),
    date(2026, 12, 25),
    date(2027, 1, 26), date(2027, 3, 26),  # 2027 tentative
}

# ── Strategy groupings ────────────────────────────────────────────────────────
_IC_STRATS       = {"nf_iron_condor"}
_BP_STRATS       = {"bull_put_credit", "bear_call_credit"}
_STRADDLE_STRATS = {"nf_short_straddle"}

_WANT_COLS = ("date", "pnl_inr", "strategy", "signal", "lots", "ml_conf", "exit_reason")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ist_today() -> date:
    return datetime.now(_IST).date()


def _count_trading_days(from_date: date, to_date: date) -> int:
    n, d = 0, from_date
    while d <= to_date:
        if d.weekday() < 5 and d not in NSE_HOLIDAYS:
            n += 1
        d += timedelta(days=1)
    return n


def _fmt_inr(val: float) -> str:
    sign, v = ("-" if val < 0 else ""), abs(val)
    if v >= 100_000:
        return f"{sign}₹{v / 100_000:.2f}L"
    if v >= 1_000:
        return f"{sign}₹{v / 1_000:.1f}K"
    return f"{sign}₹{v:.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_trades() -> pd.DataFrame:
    frames = []
    for path in (IC_CSV, SPREAD_CSV, STRADDLE_CSV):
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            if "pnl_inr" not in df.columns or df.empty:
                continue
            keep = [c for c in _WANT_COLS if c in df.columns]
            sub  = df[keep].copy()
            sub["pnl_inr"] = pd.to_numeric(sub["pnl_inr"], errors="coerce")
            frames.append(sub.dropna(subset=["pnl_inr"]))
        except Exception:
            pass

    if not frames:
        return pd.DataFrame(columns=list(_WANT_COLS))

    out = pd.concat(frames, ignore_index=True)
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.sort_values("date").reset_index(drop=True)
    if "lots" in out.columns:
        out["lots"] = pd.to_numeric(out["lots"], errors="coerce").fillna(1).clip(lower=1)
    if "ml_conf" in out.columns:
        out["ml_conf"] = pd.to_numeric(out["ml_conf"], errors="coerce")
    return out


def _load_vix() -> pd.DataFrame | None:
    """Return df with columns [date, vix] or None."""
    if not os.path.exists(VIX_CSV):
        return None
    try:
        vdf = pd.read_csv(VIX_CSV)
        vdf.columns = [c.lower() for c in vdf.columns]
        close_col = next(
            (c for c in vdf.columns if c in ("close", "^indiavix", "vix", "adj close")), None
        )
        if close_col is None:
            return None
        vdf = vdf.rename(columns={close_col: "vix"})
        vdf["vix"]  = pd.to_numeric(vdf["vix"],  errors="coerce")
        if "date" not in vdf.columns and isinstance(vdf.index, pd.DatetimeIndex):
            vdf = vdf.reset_index().rename(columns={"index": "date"})
        vdf["date"] = pd.to_datetime(vdf["date"], errors="coerce")
        return vdf[["date", "vix"]].dropna()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Confidence-weighted EWMA
# ─────────────────────────────────────────────────────────────────────────────

def _build_weights(n: int, ml_conf_arr, span: int = 20) -> np.ndarray:
    alpha  = 2.0 / (span + 1)
    decay  = np.array([(1 - alpha) ** (n - 1 - i) for i in range(n)])
    factor = np.ones(n)
    if ml_conf_arr is not None and len(ml_conf_arr) == n:
        c = np.where(np.isnan(ml_conf_arr), 1.0, ml_conf_arr)
        factor = np.where(c >= 0.8, 1.5, np.where(c >= 0.7, 1.3, 1.0))
    w = decay * factor
    return w / w.sum()


def _ewma_stats(df: pd.DataFrame, span: int = 20) -> tuple[float, float]:
    pnls = df["pnl_inr"].values
    n = len(pnls)
    if n == 0:
        return 0.0, 0.0
    confs = df["ml_conf"].values if "ml_conf" in df.columns else None
    w     = _build_weights(n, confs, span)
    return float(np.dot(w, pnls)), float(np.dot(w, (pnls > 0).astype(float)))


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Bootstrap with compounding
# ─────────────────────────────────────────────────────────────────────────────

def _pnl_per_lot_arr(df: pd.DataFrame) -> np.ndarray:
    """Per-lot P&L array for bootstrap. CRITICAL: when 'lots' column is
    missing the previous default of 1 caused 5-lot trades to be sampled as
    1-lot returns → bootstrap pool inflated 5×. Now: refuse to default,
    skip rows with missing/invalid lots so the pool stays per-lot accurate.
    """
    if df.empty:
        return np.array([])
    if "lots" not in df.columns:
        # Cannot per-lot normalise. Refuse — caller should fall back to
        # broader df with lots column rather than silently mixing.
        return np.array([])
    pnls = pd.to_numeric(df["pnl_inr"], errors="coerce").values
    lots = pd.to_numeric(df["lots"],    errors="coerce").values
    valid = (~np.isnan(pnls)) & (lots >= 1)
    return pnls[valid] / lots[valid]


def _simulate_compounding(
    pnl_per_lot: np.ndarray,
    capital: float,
    n_trades: int,
    chunk_size: int = 10,
    n_sim: int = 10_000,
) -> tuple[float, float, float, float]:
    """
    10,000 bootstrap paths.  Every chunk_size trades lot count recalculates.
    Returns (p10, p50, p90, straddle_upgrade_probability).
    """
    if len(pnl_per_lot) == 0 or n_trades <= 0:
        return 0.0, 0.0, 0.0, 0.0

    rng       = np.random.default_rng(42)
    cum_pnl   = np.zeros(n_sim, dtype=float)
    upgraded  = np.zeros(n_sim, dtype=bool)

    # CRITICAL: STRADDLE_SCALE_VS_IC is applied below to convert IC-per-lot
    # samples into straddle-per-lot expected earnings on the auto-upgrade
    # path. This is correct ONLY when `pnl_per_lot` is sampled from PURE IC
    # trades. If the caller passes a mixed pool (IC + straddle) the
    # straddle returns are already at full straddle-per-lot scale and
    # multiplying again 2.3× double-counts. The caller in this file uses
    # `_pnl_per_lot_arr(ic_df if len(ic_df) >= 3 else df)` — when the
    # fallback to `df` triggers, the pool may be mixed. Document and warn.
    def _run_chunk(size: int) -> None:
        caps          = capital + cum_pnl
        straddle_mask = caps >= STRADDLE_MARGIN_PER_LOT
        upgraded[:]  |= straddle_mask
        lots          = np.where(
            straddle_mask,
            np.clip((caps / STRADDLE_MARGIN_PER_LOT).astype(int), 1, MAX_LOTS_STRADDLE),
            np.clip((caps / IC_MARGIN_PER_LOT).astype(int),        1, MAX_LOTS_IC),
        ).astype(float)

        samples   = rng.choice(pnl_per_lot, size=(n_sim, size), replace=True)
        chunk_sum = samples.sum(axis=1)
        chunk_sum[:] = np.where(straddle_mask, chunk_sum * STRADDLE_SCALE_VS_IC, chunk_sum)
        cum_pnl[:] += chunk_sum * lots

    n_chunks  = n_trades // chunk_size
    remainder = n_trades - n_chunks * chunk_size
    for _ in range(n_chunks):
        _run_chunk(chunk_size)
    if remainder > 0:
        _run_chunk(remainder)

    return (
        float(np.percentile(cum_pnl, 10)),
        float(np.percentile(cum_pnl, 50)),
        float(np.percentile(cum_pnl, 90)),
        float(upgraded.mean()),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — VIX regime multiplier from real trade history
# ─────────────────────────────────────────────────────────────────────────────

def _vix_bucket(v: float) -> str:
    if v < 13:  return "low"
    if v < 18:  return "normal"
    if v < 22:  return "elevated"
    return "high"


def _static_vix_mult(v: float) -> tuple[float, str]:
    for lo, hi, mult, suffix in _VIX_STATIC_MULT:
        if lo <= v < hi:
            return mult, suffix
    return 1.0, "neutral"


def _vix_regime(df: pd.DataFrame, vdf: pd.DataFrame | None) -> tuple[float, str, float | None]:
    """
    Data-driven when current VIX bucket has ≥5 real trades; static otherwise.
    Returns (multiplier, label, vix_now).
    """
    if vdf is None or vdf.empty:
        return 1.0, "VIX data unavailable", None

    vix_now = float(vdf.iloc[-1]["vix"])
    cur_bkt = _vix_bucket(vix_now)

    # Try data-driven
    if not df.empty and "date" in df.columns:
        try:
            tdf = df[["date", "pnl_inr"]].copy()
            tdf["date"] = pd.to_datetime(tdf["date"]).dt.normalize()
            vdf2 = vdf.copy()
            vdf2["date"] = pd.to_datetime(vdf2["date"]).dt.normalize()

            merged = tdf.merge(vdf2, on="date", how="left")
            merged["bucket"] = merged["vix"].apply(
                lambda x: _vix_bucket(x) if pd.notna(x) else None
            )

            bkt_rows    = merged[merged["bucket"] == cur_bkt]
            n_bkt       = len(bkt_rows)
            overall_avg = float(merged["pnl_inr"].mean())

            if n_bkt >= 5 and overall_avg != 0:
                bkt_avg  = float(bkt_rows["pnl_inr"].mean())
                mult     = float(np.clip(bkt_avg / overall_avg, 0.7, 1.3))
                pct      = (mult - 1) * 100
                if pct >= 1:
                    adj_text = f"boosting estimate by {pct:.0f}% (we earn more in this market)"
                elif pct <= -1:
                    adj_text = f"cutting estimate by {abs(pct):.0f}% (we earn less in this market)"
                else:
                    adj_text = "no adjustment (this market is normal for us)"
                return (
                    mult,
                    f"Fear gauge at {vix_now:.1f} ({cur_bkt}) — {adj_text} [based on {n_bkt} real trades]",
                    vix_now,
                )
            else:
                static_mult, suffix = _static_vix_mult(vix_now)
                return (
                    static_mult,
                    f"Fear gauge at {vix_now:.1f} — {suffix} [estimate, only {n_bkt} of 5 trades at this fear level]",
                    vix_now,
                )
        except Exception:
            pass

    static_mult, suffix = _static_vix_mult(vix_now)
    return static_mult, f"Fear gauge at {vix_now:.1f} — {suffix} [estimate]", vix_now


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — Strategy-level breakdown
# ─────────────────────────────────────────────────────────────────────────────

def _strategy_breakdown(df: pd.DataFrame) -> list[dict]:
    """Per-strategy EWMA stats. Only includes strategies with ≥3 trades."""
    if "strategy" not in df.columns:
        return []
    rows = []
    for label, strat_set in (
        ("Iron Condor (up-days)",  _IC_STRATS),
        ("Bull Put (down-days)",   _BP_STRATS),
        ("Short Straddle",         _STRADDLE_STRATS),
    ):
        sub = df[df["strategy"].isin(strat_set)]
        if len(sub) < 3:
            continue
        ewma_avg, ewma_wr = _ewma_stats(sub)
        rows.append({
            "label": label,
            "n": len(sub),
            "wr_pct": int(round(ewma_wr * 100)),
            "ewma_avg": ewma_avg,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Layer 5 — Exit-reason regime shift
# ─────────────────────────────────────────────────────────────────────────────

def _exit_regime_check(df: pd.DataFrame) -> tuple[float | None, bool]:
    """Returns (recent_sl_rate, is_elevated). Needs ≥5 rows with exit_reason."""
    if "exit_reason" not in df.columns or len(df) < 5:
        return None, False
    recent   = df.tail(_SL_WINDOW)
    reasons  = recent["exit_reason"].fillna("").str.upper()
    sl_rate  = float((reasons == "SL").sum() / len(recent))
    return sl_rate, sl_rate >= _SL_ALERT_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Capital display helper
# ─────────────────────────────────────────────────────────────────────────────

def _capital_line(capital: float) -> list[str]:
    """Returns list of plain-English lines describing capital + lots."""
    lots_now       = max(1, int(capital // IC_MARGIN_PER_LOT))
    next_threshold = (lots_now + 1) * IC_MARGIN_PER_LOT
    needed         = next_threshold - capital
    plural         = "s" if lots_now > 1 else ""
    lines = [
        f"Account balance: <b>{_fmt_inr(capital)}</b>",
        f"Trading <b>{lots_now}</b> lot{plural} per trade",
        f"Next lot kicks in at <b>{_fmt_inr(next_threshold)}</b> "
        f"(need <b>{_fmt_inr(needed)}</b> more)",
    ]
    if capital >= STRADDLE_MARGIN_PER_LOT:
        lines.append("🎯 <b>You hit Short Straddle tier — bigger strategy active</b>")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    today   = _ist_today()
    df      = _load_trades()
    n_total = len(df)

    if n_total == 0:
        notify.send(
            "📈 <b>Season Forecast</b> · No live trades yet.\n"
            "Forecast will appear once trading begins.",
            silent=dry_run,
        )
        return

    vdf = _load_vix()

    # ── Core EWMA stats (all trades, confidence-weighted) ────────────────────
    ewma_avg, ewma_wr = _ewma_stats(df)
    ytd_pnl           = float(df["pnl_inr"].sum())
    capital           = INITIAL_CAPITAL + ytd_pnl
    days_left         = _count_trading_days(today + timedelta(days=1), FY_END)

    # ── pnl_per_lot array for compounding sim ────────────────────────────────
    # MUST be IC-only — the bootstrap applies STRADDLE_SCALE_VS_IC = 2.3×
    # on straddle paths, expecting IC-per-lot input. Mixing strategies
    # double-counts straddle return on the auto-upgrade path.
    if "strategy" in df.columns:
        ic_df = df[df["strategy"].isin(_IC_STRATS)]
    else:
        ic_df = pd.DataFrame()
    if len(ic_df) >= 3:
        ppl = _pnl_per_lot_arr(ic_df)
    else:
        # Too few IC trades to bootstrap. Fall back to *Bull Put only* — same
        # credit-spread family with similar per-lot magnitude — rather than
        # mixing in straddle returns. If that's also too small, refuse to
        # forecast (signal "INSUFFICIENT DATA" upstream).
        bp_df = (df[df["strategy"] == "bull_put_credit"]
                 if "strategy" in df.columns else pd.DataFrame())
        ppl = _pnl_per_lot_arr(bp_df) if len(bp_df) >= 3 else np.array([])
        if len(ppl) == 0:
            notify.send(
                "📈 <b>Season Forecast</b> · Not enough IC / Bull-Put trades to forecast yet.\n"
                "Need ≥ 3 trades of either strategy. Comes back online after a few sessions.",
                silent=dry_run,
            )
            return

    # Layer 2 — bootstrap with compounding
    p10, p50, p90, upgrade_prob = _simulate_compounding(ppl, capital, days_left)

    # Layer 3 — VIX regime
    vix_mult, regime_label, _vix = _vix_regime(df, vdf)
    adj_p50 = p50 * vix_mult

    # Layer 4 — strategy breakdown
    strat_rows = _strategy_breakdown(df)

    # Layer 5 — SL regime alert
    sl_rate_recent, sl_elevated = _exit_regime_check(df)

    # ── Compose message ──────────────────────────────────────────────────────
    wr_pct   = int(round(ewma_wr * 100))
    fy_opt   = ytd_pnl + p90
    fy_mid   = ytd_pnl + adj_p50
    fy_cons  = ytd_pnl + p10

    lines = [
        f"📈 <b>Year-End Forecast</b> · {today.strftime('%d %b %Y')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>So far this season:</b>",
        f"  Trades done: <b>{n_total}</b>",
        f"  Winning <b>{wr_pct}%</b> of trades",
        f"  Avg profit per trade: <b>{_fmt_inr(ewma_avg)}</b>",
        f"  Earned so far: <b>{_fmt_inr(ytd_pnl)}</b>",
        "",
    ]
    lines += [f"  {ln}" for ln in _capital_line(capital)]

    # Strategy breakdown
    if strat_rows:
        lines += ["", "<b>Strategy breakdown:</b>"]
        for s in strat_rows:
            lines.append(
                f"  {s['label']}: <b>{s['n']}</b> trades, "
                f"won <b>{s['wr_pct']}%</b>, avg <b>{_fmt_inr(s['ewma_avg'])}</b>/trade"
            )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>By 31 Mar 2027</b> ({days_left} trading days left):",
        f"  Best case  (top 10% chance):    <b>{_fmt_inr(fy_opt)}</b>",
        f"  Most likely:                    <b>{_fmt_inr(fy_mid)}</b>",
        f"  Worst case (bottom 10%):        <b>{_fmt_inr(fy_cons)}</b>",
        "",
        "  <i>Numbers grow as account grows — more lots per trade = more profit</i>",
    ]

    if upgrade_prob >= 0.05:
        lines.append(
            f"  💡 Likely to switch to Short Straddle (bigger strategy) "
            f"before year end: <b>{upgrade_prob * 100:.0f}%</b> chance"
        )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>Today's market:</b> {regime_label}",
    ]

    if sl_elevated and sl_rate_recent is not None:
        lines += [
            "",
            f"⚠️ <b>Heads up:</b> hitting stop-loss too often lately — "
            f"<b>{sl_rate_recent * 100:.0f}%</b> of last {_SL_WINDOW} trades "
            f"(normal is around <b>{_HISTORICAL_SL_RATE * 100:.0f}%</b>). "
            f"Market may be turning rough.",
        ]

    if n_total < 15:
        lines += [
            "",
            f"⚠️ Only <b>{n_total}</b> trades done — these numbers will get more "
            f"reliable after 20+ trades.",
        ]

    notify.send("\n".join(lines), silent=dry_run)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
