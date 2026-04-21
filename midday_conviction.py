#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
midday_conviction.py — Intraday trade conviction check
=======================================================
Fetches current BN spot + option LTP + intraday macro, reassesses
whether the morning signal thesis still holds, sends Telegram summary.

Usage:
  python3 midday_conviction.py          # live run
  python3 midday_conviction.py --dry-run # print without sending Telegram

Cron (11:00 AM IST = 5:30 AM UTC):
  30 5 * * 1-5 cd ~/dhan-trading && python3 midday_conviction.py >> logs/conviction.log 2>&1
"""

import os
import sys
import json
import requests
import pandas as pd
from datetime import date, datetime
from dotenv import load_dotenv
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

load_dotenv()

_HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(_HERE, "data")
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DRY_RUN   = "--dry-run" in sys.argv
IST       = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

import notify


def _log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S IST")
    print(f"[{ts}] {msg}")


# ── Data loaders ──────────────────────────────────────────────────────────────

def load_trade() -> dict | None:
    """Load today's oracle intent from today_trade.json. Returns None if stale."""
    path = os.path.join(DATA_DIR, "today_trade.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        t = json.load(f)
    if str(t.get("date", "")) != str(date.today()):
        _log(f"today_trade.json dated {t.get('date')} — not today's trade.")
        return None
    return t


def get_bn_spot() -> float | None:
    """Current BankNifty spot via Dhan option chain, with API structure unwrap."""
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        expiries = r.json().get("data", [])
        if not expiries:
            _log("expirylist returned empty — market closed or API issue")
            return None
        expiry = expiries[0]
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=10,
        )
        inner = r2.json().get("data") or {}
        # Dhan wraps chain in {"<id>": {"last_price": ..., "oc": {...}}}
        if isinstance(inner, dict) and "last_price" not in inner:
            inner = next(iter(inner.values()), {})
        spot = float(inner.get("last_price") or inner.get("underlyingPrice") or 0)
        return spot if spot > 0 else None
    except Exception as e:
        _log(f"BN spot unavailable: {e}")
        return None


def _get_ltp_from_marketfeed(security_id: str) -> float | None:
    """Fetch current option LTP from Dhan marketfeed/ltp using our security_id.
    This is the most direct route — no strike-key guessing needed."""
    try:
        sid_int = int(security_id)
        resp = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=HEADERS,
            json={"NSE_FNO": [sid_int]},
            timeout=10,
        )
        if resp.status_code != 200:
            _log(f"marketfeed/ltp {resp.status_code}: {resp.text[:80]}")
            return None
        d = resp.json()
        # Response: {"data": {"NSE_FNO": {<sid>: {"last_price": ...}}}} or similar
        fno_data = (d.get("data") or {}).get("NSE_FNO") or d.get("NSE_FNO") or {}
        entry = fno_data.get(sid_int) or fno_data.get(str(sid_int)) or fno_data.get(security_id) or {}
        ltp = float(entry.get("last_price") or entry.get("ltp") or entry.get("lastTradedPrice") or 0)
        if ltp > 0:
            _log(f"marketfeed/ltp: ₹{ltp:.0f}  [SID {security_id}]")
            return ltp
        _log(f"marketfeed/ltp returned 0 for SID {security_id}. Payload: {str(d)[:120]}")
    except Exception as e:
        _log(f"marketfeed/ltp failed: {e}")
    return None


def _get_ltp_from_option_chain(trade: dict) -> float | None:
    """Fetch current option premium from Dhan option chain at our known strike.

    Strategy:
      1. marketfeed/ltp by security_id — direct, reliable, no key-format guessing.
      2. Option chain scan by security_id — handles any oc key format.
      3. Option chain strike key lookup — last resort.
    """
    our_sid = str(trade.get("security_id", ""))

    # ── Strategy 1: marketfeed/ltp (fastest, most direct) ─────────────────────
    if our_sid:
        ltp = _get_ltp_from_marketfeed(our_sid)
        if ltp:
            return ltp

    # ── Strategy 2 & 3: option chain ──────────────────────────────────────────
    try:
        strike      = float(trade.get("strike", 0))
        opt_type_lc = "ce" if trade.get("signal", "CALL") == "CALL" else "pe"
        opt_type_uc = opt_type_lc.upper()

        # Use stored expiry from today_trade.json if available; else fetch nearest
        if trade.get("expiry"):
            expiry = trade["expiry"]   # already a string "YYYY-MM-DD"
        else:
            r = requests.post(
                "https://api.dhan.co/v2/optionchain/expirylist",
                headers=HEADERS,
                json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
                timeout=10,
            )
            expiry = r.json()["data"][0]

        # Fetch option chain
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=15,
        )
        data  = r2.json()
        inner = data.get("data") or {}

        # Dhan wraps chain in a single-key dict (instrument/scrip id → actual data)
        # e.g. {"805": {"last_price": ..., "oc": {...}}}
        if isinstance(inner, dict) and "oc" not in inner and "last_price" not in inner:
            inner = next(iter(inner.values()), {})
            _log(f"Unwrapped option chain nesting → inner keys: {list(inner.keys())[:5]}")

        oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}

        if not oc:
            _log(f"Option chain oc still empty after unwrap (expiry {expiry}). "
                 f"inner keys: {list(inner.keys()) if isinstance(inner, dict) else type(inner)}")
            return None

        _log(f"Option chain: {len(oc)} strikes  (expiry {expiry}). "
             f"Sample keys: {list(oc.keys())[:3]}")

        # Strategy 2: scan by security_id (format-agnostic)
        if our_sid:
            for strike_key, opts in oc.items():
                for otk in (opt_type_lc, opt_type_uc):
                    sub = opts.get(otk) if isinstance(opts, dict) else None
                    if not sub:
                        continue
                    sid_in_chain = str(sub.get("security_id") or sub.get("securityId") or "")
                    if sid_in_chain == our_sid:
                        ltp = float(sub.get("last_price") or sub.get("ltp")
                                    or sub.get("lastPrice") or 0)
                        _log(f"Option chain matched SID {our_sid}: ₹{ltp:.0f}  [{strike_key} {otk.upper()}]")
                        if ltp > 0:
                            return ltp
                        _log("Option chain SID match but LTP=0")

        # Strategy 3: strike key lookup
        key = (f"{strike:.6f}"  if f"{strike:.6f}"  in oc else
               str(int(strike)) if str(int(strike)) in oc else None)
        if key is None:
            _log(f"Strike {strike:.0f} not in oc keys. Sample: {list(oc.keys())[:5]}")
            return None
        sub = oc[key].get(opt_type_lc) or oc[key].get(opt_type_uc) or {}
        ltp = float(sub.get("last_price") or sub.get("ltp") or sub.get("lastPrice") or 0)
        if ltp > 0:
            _log(f"Option chain LTP by strike: ₹{ltp:.0f}  [{strike:.0f} {opt_type_uc}]")
            return ltp
        _log(f"Option chain strike key also returned 0 for {strike:.0f} {opt_type_uc}")
    except Exception as e:
        _log(f"Option chain LTP fallback failed: {e}")
    return None


def get_option_ltp(security_id: str, trade: dict | None = None) -> float | None:
    """Current LTP of our open BN option.
    1. Tries positions API (fastest, matches by security_id then any open BN pos).
    2. If LTP=0, falls back to option chain at our known strike (most accurate).
    """
    try:
        r = requests.get("https://api.dhan.co/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            _log(f"Positions API {r.status_code}: {r.text[:80]}")
        else:
            data  = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            bn_positions = [
                p for p in items
                if int(p.get("netQty", 0)) > 0
                and p.get("exchangeSegment", "") == "NSE_FNO"
                and "BANKNIFTY" in str(p.get("tradingSymbol", p.get("securityId", ""))).upper()
            ]
            # Prefer exact security_id match
            ltp_from_pos = None
            for p in bn_positions:
                if str(p.get("securityId", p.get("security_id", ""))) == str(security_id):
                    ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
                    _log(f"Positions matched security_id: ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
                    ltp_from_pos = ltp
                    break
            # Fallback: any open BN position
            if ltp_from_pos is None and bn_positions:
                p   = bn_positions[0]
                ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
                _log(f"Positions fallback match: ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
                ltp_from_pos = ltp
            if ltp_from_pos and ltp_from_pos > 0:
                return ltp_from_pos
            if bn_positions:
                _log("LTP=0 from positions — trying option chain...")
            else:
                _log("No open BN positions found — trying option chain...")
    except Exception as e:
        _log(f"Positions API error: {e}")

    # Option chain fallback — uses known strike+type from trade dict
    if trade:
        return _get_ltp_from_option_chain(trade)
    return None


def get_macro() -> dict:
    """Intraday SP500 futures + DXY + India VIX via yfinance."""
    import yfinance as yf
    out = {}

    def _fetch(ticker, key_now, key_prev, interval, period):
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close = df["Close"].dropna() if "Close" in df.columns else pd.Series(dtype=float)
            if len(close) < 2:
                _log(f"  macro {ticker}: only {len(close)} rows — skipping")
                return
            out[key_now]  = float(close.iloc[-1])
            out[key_prev] = float(close.iloc[0])
            _log(f"  macro {ticker}: {float(close.iloc[0]):.2f} → {float(close.iloc[-1]):.2f}")
        except Exception as e:
            _log(f"  macro {ticker} error: {e}")

    # SP500 futures — intraday (US markets open ~7:30 PM IST; use prior day if pre-open)
    _fetch("ES=F",      "sp500f_now",  "sp500f_open",  "5m",  "2d")
    # DXY — intraday
    _fetch("DX-Y.NYB",  "dxy_now",     "dxy_open",     "5m",  "2d")
    # India VIX — daily (no intraday available via yfinance)
    _fetch("^INDIAVIX", "vix_now",     "vix_prev",     "1d",  "5d")
    # Crude oil — intraday (proxy for inflation/FII pressure on banks)
    _fetch("CL=F",      "crude_now",   "crude_open",   "5m",  "2d")

    # Derived
    if "sp500f_now" in out and "sp500f_open" in out and out.get("sp500f_open", 0):
        out["sp500f_chg_pct"] = (out["sp500f_now"] - out["sp500f_open"]) / out["sp500f_open"] * 100
    if "dxy_now" in out and "dxy_open" in out and out.get("dxy_open", 0):
        out["dxy_chg_pct"] = (out["dxy_now"] - out["dxy_open"]) / out["dxy_open"] * 100
    if "vix_now" in out and "vix_prev" in out:
        out["vix_chg"] = out["vix_now"] - out["vix_prev"]
    if "crude_now" in out and "crude_open" in out and out.get("crude_open", 0):
        out["crude_chg_pct"] = (out["crude_now"] - out["crude_open"]) / out["crude_open"] * 100
    return out


# ── Conviction engine ─────────────────────────────────────────────────────────

def reassess(trade, bn_spot, option_ltp, macro) -> tuple[int, list, str]:
    """
    Score the 4 conviction factors in real-time.
    Returns (score -4..+4, factor lines, verdict string).
    """
    direction  = trade.get("signal", "CALL")
    entry_prem = float(trade.get("oracle_premium", 0))
    sl_price   = float(trade.get("sl_price", 0))
    tp_price   = float(trade.get("tp_price", 0))
    entry_spot = float(trade.get("spot_at_signal", 0))

    bull = direction == "CALL"
    score = 0
    lines = []

    # ── Factor 1: Premium vs SL ───────────────────────────────────────────────
    if option_ltp and entry_prem > 0:
        prem_chg_pct = (option_ltp - entry_prem) / entry_prem * 100
        sl_buffer    = (option_ltp - sl_price) / entry_prem * 100   # % of entry left before SL
        if bull:
            if prem_chg_pct >= 0:
                lines.append(f"✅ Premium +{prem_chg_pct:.1f}%  ₹{option_ltp:.0f} (entry ₹{entry_prem:.0f})")
                score += 1
            elif sl_buffer > 7:
                lines.append(f"🟡 Premium {prem_chg_pct:+.1f}%  ₹{option_ltp:.0f} — {sl_buffer:.0f}% buffer to SL")
            else:
                lines.append(f"🔴 Premium {prem_chg_pct:+.1f}%  ₹{option_ltp:.0f} — only {sl_buffer:.0f}% to SL ₹{sl_price:.0f}")
                score -= 1
    else:
        lines.append("⬜ Option LTP unavailable (SL/TP may have already fired)")

    # ── Factor 2: BN spot trend ───────────────────────────────────────────────
    if bn_spot and entry_spot:
        spot_chg = (bn_spot - entry_spot) / entry_spot * 100
        if bull:
            if spot_chg > 0.2:
                lines.append(f"✅ BN spot +{spot_chg:.2f}%  ₹{bn_spot:,.0f} (entry ₹{entry_spot:,.0f})")
                score += 1
            elif spot_chg > -0.3:
                lines.append(f"➡️ BN spot flat {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
            else:
                lines.append(f"🔴 BN spot {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
                score -= 1
        else:
            if spot_chg < -0.2:
                lines.append(f"✅ BN spot {spot_chg:.2f}%  ₹{bn_spot:,.0f} (PUT thesis: BN falling)")
                score += 1
            elif spot_chg < 0.3:
                lines.append(f"➡️ BN spot flat {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
            else:
                lines.append(f"🔴 BN spot +{spot_chg:.2f}%  ₹{bn_spot:,.0f} (PUT headwind)")
                score -= 1
    else:
        lines.append("⬜ BN spot unavailable")

    # ── Factor 3: SP500 futures (global risk sentiment) ───────────────────────
    if "sp500f_chg_pct" in macro:
        sp = macro["sp500f_chg_pct"]
        if bull:
            if sp > 0.3:
                lines.append(f"✅ SP500 futures +{sp:.1f}% — risk-on supports CALL")
                score += 1
            elif sp > -0.3:
                lines.append(f"➡️ SP500 futures flat {sp:+.1f}%")
            else:
                lines.append(f"🔴 SP500 futures {sp:+.1f}% — risk-off headwind for CALL")
                score -= 1
        else:
            if sp < -0.3:
                lines.append(f"✅ SP500 futures {sp:+.1f}% — risk-off supports PUT")
                score += 1
            elif sp < 0.3:
                lines.append(f"➡️ SP500 futures flat {sp:+.1f}%")
            else:
                lines.append(f"🔴 SP500 futures +{sp:.1f}% — risk-on headwind for PUT")
                score -= 1
    else:
        lines.append("⬜ SP500 futures unavailable")

    # ── Factor 4: India VIX ───────────────────────────────────────────────────
    if "vix_chg" in macro:
        vchg = macro["vix_chg"]
        vnow = macro.get("vix_now", "?")
        if bull:
            if vchg < -0.5:
                lines.append(f"✅ India VIX {vchg:+.1f} → {vnow:.1f} — fear easing, CALL tailwind")
                score += 1
            elif vchg > 0.5:
                lines.append(f"🔴 India VIX {vchg:+.1f} → {vnow:.1f} — fear rising, CALL headwind")
                score -= 1
            else:
                lines.append(f"➡️ India VIX flat {vchg:+.1f} → {vnow:.1f}")
        else:
            if vchg > 0.5:
                lines.append(f"✅ India VIX {vchg:+.1f} → {vnow:.1f} — fear rising, PUT tailwind")
                score += 1
            elif vchg < -0.5:
                lines.append(f"🔴 India VIX {vchg:+.1f} → {vnow:.1f} — fear easing, PUT headwind")
                score -= 1
            else:
                lines.append(f"➡️ India VIX flat {vchg:+.1f} → {vnow:.1f}")
    else:
        lines.append("⬜ India VIX unavailable")

    # ── Verdict ───────────────────────────────────────────────────────────────
    if score >= 2:
        verdict = "🟢 HOLD — thesis intact"
    elif score >= 0:
        verdict = "🟡 HOLD with caution — mixed signals"
    elif score == -1:
        verdict = "🟠 WEAKENING — 1+ factors against thesis"
    else:
        verdict = "🔴 THESIS BROKEN — consider reviewing SL"

    return score, lines, verdict


# ── Reversal helpers ─────────────────────────────────────────────────────────

def _check_position_open(security_id: str) -> bool:
    """Return True if we have an open BankNifty NSE_FNO position right now.
    Fail-open: returns True if the API is unreachable (prevents false skip)."""
    try:
        r = requests.get("https://api.dhan.co/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            _log(f"Positions API {r.status_code} — assuming open to be safe")
            return True
        data  = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        for p in items:
            if (int(p.get("netQty", 0)) != 0
                    and p.get("exchangeSegment", "") == "NSE_FNO"
                    and "BANKNIFTY" in str(
                        p.get("tradingSymbol", p.get("securityId", ""))).upper()):
                return True
        return False
    except Exception as e:
        _log(f"Position check error: {e} — assuming open to be safe")
        return True


def _detect_reversal(signal: str, conv_score: int,
                     factor_lines: list, macro: dict) -> dict:
    """Detect if trade is reversing and identify macro reasons.
    Returns {reversal_detected: bool, reason_codes: list[str]}."""
    bull         = signal == "CALL"
    reason_codes = []
    reversal     = conv_score <= -1

    if reversal:
        for line in factor_lines:
            if "🔴" not in line:
                continue
            if "BN spot" in line:
                reason_codes.append("BN_SELLING" if bull else "BN_RISING")
            elif "SP500" in line:
                reason_codes.append("SP500_WEAK" if bull else "SP500_STRONG")
            elif "VIX" in line:
                reason_codes.append("VIX_SURGE" if bull else "VIX_DROP")
            elif "Premium" in line:
                reason_codes.append("PREMIUM_ERODING")

        # Extra macro reasons not covered by the 4 reassess factors
        dxy_chg   = macro.get("dxy_chg_pct")
        crude_chg = macro.get("crude_chg_pct")
        if dxy_chg is not None:
            if bull  and dxy_chg >  0.3:
                reason_codes.append("DXY_STRONG")
            if not bull and dxy_chg < -0.3:
                reason_codes.append("DXY_WEAK")
        if crude_chg is not None and bull and crude_chg > 1.5:
            reason_codes.append("CRUDE_SPIKE")

    return {"reversal_detected": reversal, "reason_codes": reason_codes}


_CHECKPOINT_FIELDS = [
    "date", "signal", "conviction_score", "verdict",
    "reversal_detected", "bn_spot", "bn_chg_from_open_pct",
    "sp500f_chg_pct", "dxy_chg_pct", "vix_now", "vix_chg",
    "crude_chg_pct", "reason_codes",
]


def _write_midday_checkpoint(record: dict) -> None:
    """Append (or overwrite today's row in) data/midday_checkpoints.csv."""
    import csv as _csv
    from pathlib import Path

    path      = Path(DATA_DIR) / "midday_checkpoints.csv"
    today_str = str(date.today())

    existing = []
    if path.exists():
        try:
            with open(path) as f:
                existing = [r for r in _csv.DictReader(f)
                            if r.get("date") != today_str]
        except Exception:
            existing = []

    row = {k: record.get(k, "") for k in _CHECKPOINT_FIELDS}
    row["date"] = today_str
    if isinstance(row.get("reason_codes"), list):
        row["reason_codes"] = "|".join(row["reason_codes"])

    existing.append(row)
    try:
        with open(path, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_CHECKPOINT_FIELDS)
            w.writeheader()
            w.writerows(existing)
        _log(f"Midday checkpoint saved → {path.name}")
    except Exception as e:
        _log(f"Failed to write midday checkpoint: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _log("Midday check starting...")

    trade = load_trade()
    if not trade:
        _log("No today_trade.json for today — no open trade to assess.")
        return

    direction   = trade.get("signal", "CALL")
    is_spread   = trade.get("strategy") in ("bear_call_credit", "bull_put_credit")
    opt_type    = "CE" if direction == "CALL" else "PE"
    icon        = "📈" if direction == "CALL" else "📉"
    now_str     = datetime.now(IST).strftime("%I:%M %p IST")

    # ══════════════════════════════════════════════════════════════════════════
    # CREDIT SPREAD PATH
    # ══════════════════════════════════════════════════════════════════════════
    if is_spread:
        strategy      = trade["strategy"]
        strategy_name = ("Bear Call Spread" if strategy == "bear_call_credit"
                         else "Bull Put Spread")
        short_sid     = str(trade.get("short_sid", ""))
        long_sid      = str(trade.get("long_sid", ""))
        short_strike  = float(trade.get("short_strike", 0))
        long_strike   = float(trade.get("long_strike", 0))
        net_credit    = float(trade.get("net_credit", 0))
        lots          = int(trade.get("lots", 1))
        lot_size      = int(trade.get("lot_size", 30))
        dte           = int(trade.get("dte", 0))
        paper         = trade.get("order_mode") == "PAPER"
        mode_tag      = "[PAPER] " if paper else ""

        _log(f"Spread: {strategy_name}  SELL {int(short_strike)} / BUY {int(long_strike)}  credit ₹{net_credit:.0f}")

        # Position check — spread has short leg (netQty<0) + long leg (netQty>0)
        pos_open = _check_position_open(short_sid or long_sid)
        if not pos_open:
            _log("No open spread positions — both legs already closed.")
            _write_midday_checkpoint({
                "signal": direction, "conviction_score": 0,
                "verdict": "closed", "reversal_detected": False,
            })
            if trade.get("exit_done"):
                _log("exit_done=True — spread_monitor already sent exit alert. No duplicate.")
            else:
                closed_msg = (
                    f"ℹ️  <b>Midday check  ·  {now_str}</b>\n\n"
                    f"Your {strategy_name} is already closed.\n"
                    f"<i>Journal runs at 3:30 PM with final P&amp;L.</i>"
                )
                if DRY_RUN:
                    import re
                    print("\n── Telegram preview (spread closed) ──────")
                    print(re.sub(r"<[^>]+>", "", closed_msg))
                else:
                    notify.send(closed_msg)
                    _log("Spread-closed message sent.")
            return

        # Fetch LTP for both legs
        short_ltp = _get_ltp_from_marketfeed(short_sid) if short_sid else None
        long_ltp  = _get_ltp_from_marketfeed(long_sid)  if long_sid  else None
        macro     = get_macro()

        # Spread P&L and SL/TP proximity
        sl_trigger = net_credit * 1.5    # CREDIT_SL_FRAC = 0.5
        tp_trigger = net_credit * 0.35   # CREDIT_TP_FRAC = 0.65

        if short_ltp and long_ltp and net_credit > 0:
            current_cost  = short_ltp - long_ltp
            pnl_per_share = net_credit - current_cost
            pnl_inr       = round(pnl_per_share * lots * lot_size, 0)
            pnl_pct       = pnl_per_share / net_credit * 100

            sl_room_pct = (sl_trigger - current_cost) / net_credit * 100
            tp_room_pct = (current_cost - tp_trigger) / net_credit * 100
            pnl_icon    = "💰" if pnl_inr >= 0 else "📉"
            pnl_sign    = "+" if pnl_inr >= 0 else ""

            spread_line = (
                f"{pnl_icon} Spread: ₹{net_credit:.0f} credit → ₹{current_cost:.0f} cost now  "
                f"(P&amp;L {pnl_sign}₹{pnl_inr:,.0f}  /  {pnl_sign}{pnl_pct:.0f}% of credit)"
            )
            legs_line = (f"📍 Short @ ₹{short_ltp:.0f}  ·  Long @ ₹{long_ltp:.0f}")
            sl_line   = (f"🛡️ SL at ₹{sl_trigger:.0f}"
                         + (f"  ({sl_room_pct:.0f}% headroom left)"
                            if sl_room_pct > 0 else "  ⚠️ very close to SL!"))
            tp_line   = (f"🎯 TP at ₹{tp_trigger:.0f}"
                         + (f"  ({tp_room_pct:.0f}% to go)"
                            if tp_room_pct > 0 else "  ✅ past TP trigger!"))

            if current_cost <= tp_trigger:
                verdict_line = "✅ <b>At or past target — spread at max profit zone.</b>"
                verdict_sub  = "spread_monitor.py will trigger TP exit automatically."
                conv_score   = 2
            elif pnl_pct >= 30:
                verdict_line = "✅ <b>Going well — collecting credit as planned.</b>"
                verdict_sub  = "Nothing to worry about."
                conv_score   = 1
            elif pnl_pct >= -10:
                verdict_line = "🟡 <b>Roughly breakeven — spread staying in range.</b>"
                verdict_sub  = "Normal. Keep watching."
                conv_score   = 0
            elif current_cost >= sl_trigger * 0.85:
                verdict_line = "🔴 <b>Approaching SL — spread moving against us.</b>"
                verdict_sub  = "spread_monitor.py exits both legs automatically if SL hits."
                conv_score   = -2
            else:
                verdict_line = "🟠 <b>Slightly against us — not at SL yet.</b>"
                verdict_sub  = "Stop-loss is your safety net. System handles it."
                conv_score   = -1
        else:
            current_cost = 0
            pnl_inr      = 0
            conv_score   = 0
            spread_line  = f"Spread LTP unavailable  (entry credit was ₹{net_credit:.0f})"
            legs_line    = ""
            sl_line      = f"🛡️ SL triggers at ₹{sl_trigger:.0f}"
            tp_line      = f"🎯 TP triggers at ₹{tp_trigger:.0f}"
            verdict_line = "⬜ <b>Cannot check — option prices unavailable.</b>"
            verdict_sub  = "Check Dhan app manually."

        # Macro
        macro_parts = []
        if "sp500f_chg_pct" in macro:
            v = macro["sp500f_chg_pct"]
            macro_parts.append(f"S&P {'up' if v >= 0 else 'down'} {abs(v):.1f}%")
        if "dxy_chg_pct" in macro:
            v = macro["dxy_chg_pct"]
            macro_parts.append(f"dollar {'stronger' if v >= 0 else 'weaker'} {abs(v):.2f}%")
        if "vix_now" in macro:
            macro_parts.append(f"fear index {macro['vix_now']:.1f}")
        if "crude_chg_pct" in macro:
            v = macro["crude_chg_pct"]
            macro_parts.append(f"crude {'up' if v >= 0 else 'down'} {abs(v):.1f}%")
        macro_str = "  ·  ".join(macro_parts) if macro_parts else "unavailable"

        _write_midday_checkpoint({
            "signal":            direction,
            "conviction_score":  conv_score,
            "verdict":           "hold" if conv_score >= 0 else "reversal",
            "reversal_detected": conv_score < 0,
            "sp500f_chg_pct":    round(macro.get("sp500f_chg_pct", 0), 3),
            "dxy_chg_pct":       round(macro.get("dxy_chg_pct", 0), 3),
            "vix_now":           round(macro.get("vix_now", 0), 2),
            "vix_chg":           round(macro.get("vix_chg", 0), 2),
            "crude_chg_pct":     (round(macro["crude_chg_pct"], 3)
                                  if "crude_chg_pct" in macro else ""),
            "reason_codes":      [],
        })

        msg = (
            f"{icon}  <b>{mode_tag}Midday check  ·  {now_str}</b>\n\n"
            f"<b>{strategy_name}  ·  SELL {int(short_strike)} {opt_type} / BUY {int(long_strike)} {opt_type}"
            f"  ·  {lots} lot  ·  {dte}d to expiry</b>\n\n"
            f"{spread_line}\n"
            + (f"{legs_line}\n" if legs_line else "")
            + f"{sl_line}\n"
            f"{tp_line}\n\n"
            f"🌍 <b>Global:</b>  {macro_str}\n\n"
            f"{verdict_line}\n"
            f"<i>{verdict_sub}</i>"
        )

        _log(f"Spread conviction {conv_score:+d}  |  current_cost=₹{current_cost:.0f}")

        if DRY_RUN:
            import re
            print("\n── Telegram preview (spread) ─────────────────────")
            print(re.sub(r"<[^>]+>", "", msg))
            print("──────────────────────────────────────────────────")
            return
        notify.send(msg)
        _log("Spread midday message sent to Telegram.")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # NAKED OPTION PATH (existing logic)
    # ══════════════════════════════════════════════════════════════════════════
    strike      = int(trade.get("strike", 0))
    lots        = int(trade.get("lots", 1))
    entry_prem  = float(trade.get("oracle_premium", 0))
    sl_price    = float(trade.get("sl_price", 0))
    tp_price    = float(trade.get("tp_price", 0))
    dte         = int(trade.get("dte", 0))
    security_id = str(trade.get("security_id", ""))
    entry_spot  = float(trade.get("spot_at_signal", 0))

    _log(f"Trade: {direction} {strike}  entry ₹{entry_prem:.0f}  SL ₹{sl_price:.0f}  TP ₹{tp_price:.0f}")

    # ── [1] Check if position is still open ───────────────────────────────────
    pos_open = _check_position_open(security_id)
    if not pos_open:
        _log("No open BankNifty position found — trade already closed.")
        _write_midday_checkpoint({
            "signal": direction, "conviction_score": 0,
            "verdict": "closed", "reversal_detected": False,
        })
        closed_msg = (
            f"ℹ️  <b>Midday check  ·  {now_str}</b>\n\n"
            f"Your BankNifty {direction} trade (strike {strike:,} {opt_type}) "
            f"is already closed — the stop-loss or target triggered earlier.\n\n"
            f"Nothing left to monitor. "
            f"<i>The 3:30 PM summary will show the final result.</i>"
        )
        if DRY_RUN:
            import re
            print("\n── Telegram preview (position closed) ─────")
            print(re.sub(r"<[^>]+>", "", closed_msg))
        else:
            notify.send(closed_msg)
            _log("Closed-position message sent.")
        return

    # ── [2] Fetch live data ────────────────────────────────────────────────────
    bn_spot    = get_bn_spot()
    option_ltp = get_option_ltp(security_id, trade)
    macro      = get_macro()
    _log(f"BN spot: {bn_spot}  |  LTP: {option_ltp}  |  Macro: {list(macro.keys())}")

    # ── [3] Reassess conviction ────────────────────────────────────────────────
    conv_score, factor_lines, _ = reassess(trade, bn_spot, option_ltp, macro)

    # ── [4] Detect reversal ────────────────────────────────────────────────────
    reversal = _detect_reversal(direction, conv_score, factor_lines, macro)

    # ── [5] Write checkpoint ───────────────────────────────────────────────────
    bn_chg_pct = ((bn_spot - entry_spot) / entry_spot * 100
                  if bn_spot and entry_spot else None)
    _write_midday_checkpoint({
        "signal":               direction,
        "conviction_score":     conv_score,
        "verdict":              "reversal" if reversal["reversal_detected"] else "hold",
        "reversal_detected":    reversal["reversal_detected"],
        "bn_spot":              round(bn_spot, 0) if bn_spot else "",
        "bn_chg_from_open_pct": round(bn_chg_pct, 3) if bn_chg_pct is not None else "",
        "sp500f_chg_pct":       round(macro.get("sp500f_chg_pct", 0), 3),
        "dxy_chg_pct":          round(macro.get("dxy_chg_pct", 0), 3),
        "vix_now":              round(macro.get("vix_now", 0), 2),
        "vix_chg":              round(macro.get("vix_chg", 0), 2),
        "crude_chg_pct":        (round(macro["crude_chg_pct"], 3)
                                 if "crude_chg_pct" in macro else ""),
        "reason_codes":         reversal["reason_codes"],
    })

    # ── [6] Build plain-English Telegram message ───────────────────────────────

    # Option P&L
    if option_ltp and entry_prem:
        pnl_pct  = (option_ltp - entry_prem) / entry_prem * 100
        pnl_rs   = (option_ltp - entry_prem) * lots * 30
        pnl_icon = "💰" if pnl_pct >= 0 else "📉"
        sign     = "+" if pnl_pct >= 0 else ""
        prem_line = (
            f"{pnl_icon} Option: ₹{entry_prem:.0f} → ₹{option_ltp:.0f}  "
            f"({sign}{pnl_pct:.1f}%,  {'+' if pnl_rs >= 0 else ''}₹{pnl_rs:,.0f})"
        )
        sl_buffer = (option_ltp - sl_price) / entry_prem * 100 if sl_price else 0
        sl_line   = (f"🛡️ Stop-loss at ₹{sl_price:.0f}"
                     + (f"  ({sl_buffer:.0f}% room left)"
                        if sl_buffer > 0 else "  ⚠️ almost there"))
        tp_left   = (tp_price - option_ltp) / entry_prem * 100 if tp_price else 0
        tp_line   = f"🎯 Target at ₹{tp_price:.0f}  (need +{tp_left:.0f}% more)"
    else:
        prem_line = f"Option LTP unavailable  (entry was ₹{entry_prem:.0f})"
        sl_line   = f"🛡️ Stop-loss at ₹{sl_price:.0f}"
        tp_line   = f"🎯 Target at ₹{tp_price:.0f}"

    # BN spot line
    if bn_spot and entry_spot:
        spot_chg = (bn_spot - entry_spot) / entry_spot * 100
        spot_line = (f"📍 BankNifty at ₹{bn_spot:,.0f}  "
                     f"({'up' if spot_chg >= 0 else 'down'} {abs(spot_chg):.2f}% from entry)")
    else:
        spot_line = "📍 BankNifty: unavailable"

    # Macro summary in plain words
    macro_parts = []
    if "sp500f_chg_pct" in macro:
        v = macro["sp500f_chg_pct"]
        macro_parts.append(f"S&P {'up' if v >= 0 else 'down'} {abs(v):.1f}%")
    if "dxy_chg_pct" in macro:
        v = macro["dxy_chg_pct"]
        macro_parts.append(f"dollar {'stronger' if v >= 0 else 'weaker'} {abs(v):.2f}%")
    if "vix_now" in macro:
        macro_parts.append(f"fear index {macro['vix_now']:.1f}")
    if "crude_chg_pct" in macro:
        v = macro["crude_chg_pct"]
        macro_parts.append(f"crude {'up' if v >= 0 else 'down'} {abs(v):.1f}%")
    macro_str = "  ·  ".join(macro_parts) if macro_parts else "unavailable"

    # Overall verdict in plain English
    if conv_score >= 2:
        verdict_line = "✅ <b>Looking good — trade is working as expected.</b>"
        verdict_sub  = "Nothing to worry about right now."
    elif conv_score >= 0:
        verdict_line = "🟡 <b>Mixed picture — some signs OK, some not ideal.</b>"
        verdict_sub  = "Nothing alarming yet. Keep an eye on it."
    elif conv_score == -1:
        verdict_line = "🟠 <b>Getting shaky — market slowly turning against us.</b>"
        verdict_sub  = "Stay alert. Your stop-loss is your safety net."
    else:
        verdict_line = "🔴 <b>Trade going wrong — multiple things working against us.</b>"
        verdict_sub  = "Stop-loss may trigger soon. No action needed — the system handles it."

    msg = (
        f"{icon}  <b>Midday check  ·  {now_str}</b>\n\n"
        f"<b>BankNifty {direction}  ·  Strike {strike:,} {opt_type}"
        f"  ·  {lots} lot(s)  ·  {dte}d to expiry</b>\n\n"
        f"{prem_line}\n"
        f"{sl_line}\n"
        f"{tp_line}\n"
        f"{spot_line}\n\n"
        f"📊 <b>Market signals:</b>\n"
        + "\n".join(factor_lines) +
        f"\n\n🌍 <b>Global:</b>  {macro_str}\n\n"
        f"{verdict_line}\n"
        f"<i>{verdict_sub}</i>"
    )

    # Reversal alert (only when things are going wrong)
    if reversal["reversal_detected"] and reversal["reason_codes"]:
        _REASON_TEXT = {
            "BN_SELLING":      "BankNifty falling — going against your CALL trade",
            "BN_RISING":       "BankNifty rising — going against your PUT trade",
            "SP500_WEAK":      "Global markets weak — dragging India lower",
            "SP500_STRONG":    "Global markets strong — hurts PUT trades",
            "VIX_SURGE":       "Fear index spiking — uncertainty hurting options",
            "VIX_DROP":        "Fear easing — fewer big moves, hurts PUT trades",
            "PREMIUM_ERODING": "Option value close to stop-loss level",
            "DXY_STRONG":      "Dollar strengthening — foreign investors selling India",
            "DXY_WEAK":        "Dollar weakening — less pressure on India markets",
            "CRUDE_SPIKE":     "Crude oil surging — inflation fear, bad for banks",
        }
        bullets = "\n".join(
            f"  • {_REASON_TEXT.get(rc, rc)}" for rc in reversal["reason_codes"]
        )
        msg += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Why it's going wrong:</b>\n"
            f"{bullets}\n\n"
            f"<i>No action needed — stop-loss handles this automatically.</i>"
        )

    _log(f"Conviction {conv_score:+d}/4  |  Reversal: {reversal['reversal_detected']}")

    if DRY_RUN:
        import re
        print("\n── Telegram preview ──────────────────────")
        print(re.sub(r"<[^>]+>", "", msg))
        print("──────────────────────────────────────────")
        return

    notify.send(msg)
    _log("Midday message sent to Telegram.")


if __name__ == "__main__":
    main()
