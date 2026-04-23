#!/usr/bin/env python3
"""
regime_watcher.py — Autonomous regime change detector and strategy updater.

What it does
------------
Runs monthly (cron, same day as lot_expiry_scanner.py). Detects two kinds of
regime changes that invalidate backtest results:

  1. Lot size change  (e.g., NF lot 65 → 75 after next NSE revision)
  2. Expiry day change (e.g., Tuesday expiry → Thursday, as happened Sep 2025)

On any change:
  a. Fetches fresh market data (data_fetcher.py, signal_engine.py)
  b. Runs full backtest across all strategies for the new regime
  c. Picks the best strategy (highest WR% with positive P&L in most recent 6 months)
  d. Auto-patches LOT_SIZE in auto_trader.py if lot size changed
  e. Sends a detailed Telegram report with findings and any manual steps

Usage
-----
  python3 regime_watcher.py           # standard run (checks for changes)
  python3 regime_watcher.py --force   # run full analysis even if no change detected
  python3 regime_watcher.py --dry-run # analysis only, no file writes, no Telegram
  python3 regime_watcher.py --show    # print current regime state and exit

Cron (monthly, 2nd of month at 10:15 AM IST = 4:45 AM UTC — after lot_expiry_scanner)
  45 4 2 * * cd /home/user/dhan-trading && python3 regime_watcher.py
"""

import os
import sys
import re
import json
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR        = Path("data")
REGIME_STATE    = DATA_DIR / "regime_state.json"
AUTO_TRADER     = Path("auto_trader.py")

DHAN_TOKEN      = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID  = os.getenv("DHAN_CLIENT_ID", "")
DHAN_HEADERS    = {
    "access-token": DHAN_TOKEN,
    "client-id":    DHAN_CLIENT_ID,
    "Content-Type": "application/json",
}


# ─────────────────────────────────────────────────────────────────────────────
#  State
# ─────────────────────────────────────────────────────────────────────────────

STRATEGY_KEYS = [
    "nf_iron_condor",
    "nf_bull_put_credit",
    "nf_hybrid_ic_bullput",
    "nf_short_straddle",
    "nf_bear_call_credit",
]

STRATEGY_LABELS = {
    "nf_iron_condor":        "Iron Condor",
    "nf_bull_put_credit":    "Bull Put Credit",
    "nf_hybrid_ic_bullput":  "IC(CALL)+BullPut(PUT) ★",
    "nf_short_straddle":     "Short Straddle",
    "nf_bear_call_credit":   "Bear Call Credit",
}

def _default_state():
    return {
        "lot_size":        65,
        "expiry_weekday":  1,      # 1=Tuesday
        "expiry_pattern":  "weekly_tuesday",
        "best_strategy":   "nf_hybrid_ic_bullput",
        "last_check":      None,
        "last_change":     None,
        "last_backtest":   None,
    }


def load_state():
    if not REGIME_STATE.exists():
        return _default_state()
    try:
        with open(REGIME_STATE) as f:
            s = json.load(f)
        # Ensure all keys exist (forward-compat)
        defaults = _default_state()
        for k, v in defaults.items():
            s.setdefault(k, v)
        return s
    except Exception:
        return _default_state()


def save_state(state, dry_run=False):
    if dry_run:
        return
    DATA_DIR.mkdir(exist_ok=True)
    with open(REGIME_STATE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
#  Dhan — current lot size and expiry day
# ─────────────────────────────────────────────────────────────────────────────

def fetch_current_lot_size():
    """
    Fetch current NF lot size from Dhan scrip master.
    Returns int or None.
    """
    if not DHAN_TOKEN:
        return None
    try:
        import csv, io
        r = requests.get(
            "https://images.dhan.co/api-data/api-scrip-master.csv",
            timeout=30,
        )
        if r.status_code != 200:
            return None
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            sym  = (row.get("SEM_TRADING_SYMBOL", "") or "").upper()
            name = (row.get("SM_SYMBOL_NAME",     "") or "").upper()
            inst = (row.get("SEM_INSTRUMENT_NAME","") or "").upper()
            if (("NIFTY" in sym and "BANK" not in sym)
                    or ("NIFTY" in name and "BANK" not in name)):
                if inst in ("OPTIDX", "FUTIDX"):
                    lot = row.get("SEM_LOT_UNITS") or row.get("SEM_LOT_SIZE") or ""
                    try:
                        lot_int = int(float(lot))
                        if lot_int > 0:
                            return lot_int
                    except ValueError:
                        continue
    except Exception as e:
        print(f"  Lot size fetch failed: {e}")
    return None


def fetch_expiry_weekday():
    """
    Get NF expiry weekday (0=Mon, 1=Tue…) from Dhan expirylist.
    Returns int (most common weekday in next 4 expiries) or None.
    """
    if not DHAN_TOKEN:
        return None
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=DHAN_HEADERS,
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        expiries = r.json().get("data", [])
        parsed = []
        for e in expiries[:4]:
            try:
                parsed.append(date.fromisoformat(e).weekday())
            except Exception:
                continue
        if not parsed:
            return None
        # Most common weekday
        from collections import Counter
        return Counter(parsed).most_common(1)[0][0]
    except Exception as e:
        print(f"  Expiry fetch failed: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Data refresh
# ─────────────────────────────────────────────────────────────────────────────

def refresh_data():
    """Run data_fetcher.py and signal_engine.py to get fresh signals."""
    print("  Refreshing market data...")
    for cmd in ["python3 data_fetcher.py", "python3 signal_engine.py"]:
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                print(f"  WARNING: {cmd} exited {result.returncode}: {result.stderr[:200]}")
            else:
                print(f"  {cmd} ✓")
        except Exception as e:
            print(f"  {cmd} failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Backtest — run programmatically and return structured results
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(regime_start_date=None):
    """
    Import backtest_hold_periods and run all strategies for the most recent regime.
    Returns dict: {strategy_key: {"trades": N, "wr": float, "total_pnl": float, "avg_pnl": float}}
    """
    try:
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location(
            "backtest_hold_periods", "backtest_hold_periods.py"
        )
        bt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bt)
    except Exception as e:
        print(f"  Cannot import backtest_hold_periods: {e}")
        return {}

    try:
        signals   = bt.load_signals(ml=True)
        ohlcv     = bt.load_nf_ohlcv()
        vix_df    = bt.load_vix()
        opts_daily = bt.load_opts_daily()
    except Exception as e:
        print(f"  Cannot load backtest data: {e}")
        return {}

    if regime_start_date is None:
        # Default: last 6 months
        regime_start_date = (date.today() - timedelta(days=180)).isoformat()

    results = {}
    for key in STRATEGY_KEYS:
        if key not in bt.STRATEGIES:
            continue
        try:
            trades = bt.run_strategy(
                key, 0, signals, ohlcv, vix_df, opts_daily,
                start_date=regime_start_date, end_date=None,
            )
            if not trades:
                results[key] = {"trades": 0, "wr": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
                continue
            agg = bt.aggregate(trades)
            results[key] = {
                "trades":    agg["trades"],
                "wr":        agg["wr_pct"] / 100.0,
                "total_pnl": agg["total_pnl"],
                "avg_pnl":   agg["avg_pnl"],
            }
        except Exception as e:
            print(f"  Strategy {key} error: {e}")
            results[key] = {"trades": 0, "wr": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}

    return results


def pick_best_strategy(results):
    """
    Pick best strategy: highest WR% among those with positive total P&L and ≥10 trades.
    Excludes permanently discarded strategies.
    Returns strategy_key or None.
    """
    DISCARD = {"nf_bear_call_credit"}   # 13.5% WR — permanently dumped
    PREFER  = "nf_hybrid_ic_bullput"    # tie-break: prefer the researched hybrid

    candidates = [
        (k, v) for k, v in results.items()
        if k not in DISCARD
        and v["trades"] >= 10
        and v["total_pnl"] > 0
    ]
    if not candidates:
        return None

    # Sort by WR descending, then P&L descending
    candidates.sort(key=lambda x: (x[1]["wr"], x[1]["total_pnl"]), reverse=True)

    # Prefer the researched hybrid if it's within 5% WR of the top
    top_wr = candidates[0][1]["wr"]
    for k, v in candidates:
        if k == PREFER and v["wr"] >= top_wr - 0.05:
            return PREFER
    return candidates[0][0]


# ─────────────────────────────────────────────────────────────────────────────
#  Monthly margin freshness check
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_bull_put_margin():
    """
    Query Dhan /v2/margincalculator/multi for a representative Bull Put spread (1 lot).
    Uses the nearest expiry and ATM PE / ATM-150 PE from the live option chain.
    Returns actual margin float or None on failure.
    """
    if not DHAN_TOKEN:
        return None
    try:
        # Get nearest expiry
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=DHAN_HEADERS,
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        expiry = r.json().get("data", [None])[0]
        if not expiry:
            return None

        # Get ATM PE and ATM-150 PE SIDs
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=DHAN_HEADERS,
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=10,
        )
        if r2.status_code != 200:
            return None
        inner = r2.json().get("data", {})
        spot  = float(inner.get("last_price", 0))
        oc    = inner.get("oc", {})
        if not spot or not oc:
            return None

        # Find ATM strike (round to nearest 50)
        atm = round(spot / 50) * 50
        long_strike = atm - 150   # BUY ATM-150 PE

        def _get_sid_ltp(strike, opt_type="pe"):
            for k in [f"{float(strike):.6f}", str(int(strike)), f"{float(strike):.1f}"]:
                if k in oc:
                    sub = oc[k].get(opt_type) or oc[k].get(opt_type.upper()) or {}
                    sid = sub.get("security_id") or sub.get("securityId")
                    ltp = float(sub.get("last_price") or sub.get("ltp") or 0)
                    if sid and ltp > 0:
                        return str(sid), ltp
            return None, 0.0

        short_sid, short_ltp = _get_sid_ltp(atm)
        long_sid,  long_ltp  = _get_sid_ltp(long_strike)
        if not short_sid or not long_sid:
            return None

        # Get current lot size from auto_trader constants
        text = AUTO_TRADER.read_text()
        m = re.search(r"^LOT_SIZE\s*=\s*(\d+)", text, re.MULTILINE)
        lot_size = int(m.group(1)) if m else 65

        payload = {
            "dhanClientId":    DHAN_CLIENT_ID,
            "includePosition": True,
            "includeOrders":   True,
            "scripList": [
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": lot_size, "productType": "MARGIN",
                 "securityId": short_sid, "price": float(short_ltp), "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "BUY",
                 "quantity": lot_size, "productType": "MARGIN",
                 "securityId": long_sid, "price": float(long_ltp), "triggerPrice": 0},
            ],
        }
        r3 = requests.post(
            "https://api.dhan.co/v2/margincalculator/multi",
            headers=DHAN_HEADERS, json=payload, timeout=10,
        )
        if r3.status_code == 200:
            d = r3.json()
            margin = float(d.get("total_margin") or d.get("totalMargin") or 0)
            if margin > 5_000:
                return margin
    except Exception as e:
        print(f"  Bull Put live margin fetch failed: {e}")
    return None


def check_and_patch_bull_put_margin(dry_run=False):
    """
    Compare BULL_PUT_MARGIN_PER_LOT fallback against live Dhan SPAN.
    If off by more than 10%, auto-patch the constant so the fallback stays relevant.
    Returns (live_margin, patched: bool, patch_msg: str).
    """
    live_margin = fetch_live_bull_put_margin()
    if live_margin is None:
        print("  Could not fetch live Bull Put margin — skipping freshness check.")
        return None, False, ""

    text = AUTO_TRADER.read_text()
    m = re.search(r"^BULL_PUT_MARGIN_PER_LOT\s*=\s*(\d+)", text, re.MULTILINE)
    if not m:
        print("  BULL_PUT_MARGIN_PER_LOT not found in auto_trader.py")
        return live_margin, False, ""

    current_fallback = int(m.group(1))
    drift_pct = abs(live_margin - current_fallback) / current_fallback

    print(f"  Live Bull Put SPAN margin: ₹{live_margin:,.0f}  "
          f"  Fallback: ₹{current_fallback:,.0f}  "
          f"  Drift: {drift_pct:.1%}")

    if drift_pct <= 0.10:
        print("  Fallback within 10% of live margin — no patch needed.")
        return live_margin, False, ""

    # Patch: round up to next 1000 for headroom
    new_fallback = int((live_margin // 1000 + 1) * 1000)
    new_text = re.sub(
        r"^(BULL_PUT_MARGIN_PER_LOT\s*=\s*)\d+",
        lambda match: f"{match.group(1)}{new_fallback}",
        text, flags=re.MULTILINE,
    )

    msg = f"BULL_PUT_MARGIN_PER_LOT: ₹{current_fallback:,} → ₹{new_fallback:,} (live SPAN ₹{live_margin:,.0f})"
    if dry_run:
        print(f"  [DRY RUN] Would patch: {msg}")
        return live_margin, True, msg

    try:
        AUTO_TRADER.write_text(new_text)
        print(f"  Patched auto_trader.py: {msg}")
        return live_margin, True, msg
    except Exception as e:
        print(f"  Failed to patch: {e}")
        return live_margin, False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  Auto-patch auto_trader.py
# ─────────────────────────────────────────────────────────────────────────────

def patch_lot_size(new_lot, dry_run=False):
    """
    Update LOT_SIZE and STRADDLE_MARGIN_PER_LOT in auto_trader.py.
    Returns (old_lot, patched: bool).
    """
    try:
        text = AUTO_TRADER.read_text()
    except Exception as e:
        print(f"  Cannot read auto_trader.py: {e}")
        return None, False

    # Find current LOT_SIZE
    m = re.search(r"^LOT_SIZE\s*=\s*(\d+)", text, re.MULTILINE)
    if not m:
        print("  LOT_SIZE not found in auto_trader.py")
        return None, False

    old_lot = int(m.group(1))
    if old_lot == new_lot:
        print(f"  LOT_SIZE already {new_lot} — no patch needed.")
        return old_lot, False

    new_text = re.sub(
        r"^(LOT_SIZE\s*=\s*)\d+",
        lambda match: f"{match.group(1)}{new_lot}",
        text, flags=re.MULTILINE,
    )

    if dry_run:
        print(f"  [DRY RUN] Would patch LOT_SIZE: {old_lot} → {new_lot}")
        return old_lot, True

    try:
        AUTO_TRADER.write_text(new_text)
        print(f"  Patched auto_trader.py: LOT_SIZE {old_lot} → {new_lot}")
        return old_lot, True
    except Exception as e:
        print(f"  Failed to patch auto_trader.py: {e}")
        return old_lot, False


# ─────────────────────────────────────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────────────────────────────────────

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def send_regime_report(changes, backtest_results, best_strategy, patches, dry_run=False):
    """
    Send a plain-English Telegram report with regime change findings.
    """
    if dry_run:
        print("\n[DRY RUN] Telegram report would read:")

    lines = []

    if changes:
        lines.append("🔄 <b>Regime Change Detected</b>")
        for change in changes:
            lines.append(f"  {change}")
    else:
        lines.append("📊 <b>Monthly Regime Check — No Changes</b>")

    lines.append("")

    if backtest_results:
        lines.append("Last 6 months backtest (all strategies):")
        for key in STRATEGY_KEYS:
            v = backtest_results.get(key)
            if v is None or v["trades"] == 0:
                continue
            label  = STRATEGY_LABELS.get(key, key)
            marker = " ★" if key == best_strategy else ""
            lines.append(
                f"  {label}{marker}: {v['wr']:.0%} WR  "
                f"₹{v['total_pnl']:,.0f} total  ({v['trades']} trades)"
            )

    if best_strategy:
        label = STRATEGY_LABELS.get(best_strategy, best_strategy)
        lines.append(f"\n✅ Best strategy for new regime: <b>{label}</b>")

    if patches:
        lines.append("\nAuto-applied changes:")
        for p in patches:
            lines.append(f"  • {p}")
    else:
        lines.append("\nNo auto-patches needed.")

    current_strategy_label = STRATEGY_LABELS.get("nf_hybrid_ic_bullput", "IC+BullPut")
    if best_strategy and best_strategy != "nf_hybrid_ic_bullput":
        new_label = STRATEGY_LABELS.get(best_strategy, best_strategy)
        lines.append(
            f"\n⚠️ Recommend switching from {current_strategy_label} to {new_label}. "
            f"Open Claude Code and ask it to apply the new strategy routing."
        )
    elif best_strategy == "nf_hybrid_ic_bullput":
        lines.append(f"\nCurrent strategy ({current_strategy_label}) is still the best performer. "
                     f"No routing changes needed.")

    msg = "\n".join(lines)

    if dry_run:
        print(msg)
        return

    try:
        import notify
        notify.send(msg)
        print("  Telegram report sent.")
    except Exception as e:
        print(f"  Telegram send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def show_state():
    state = load_state()
    print(json.dumps(state, indent=2, default=str))


def main():
    args  = sys.argv[1:]
    dry   = "--dry-run" in args
    force = "--force"   in args

    if "--show" in args:
        show_state()
        return

    today = date.today()
    state = load_state()

    print(f"Regime watcher — {today}")
    print("=" * 60)

    # 1. Fetch current lot size and expiry day
    print("\n[1] Fetching current NF lot size from Dhan...")
    current_lot = fetch_current_lot_size()
    if current_lot:
        print(f"    Current lot size: {current_lot}")
    else:
        print("    Could not fetch — using state value as fallback.")
        current_lot = state["lot_size"]

    print("\n[2] Fetching current NF expiry day from Dhan...")
    current_expiry_wd = fetch_expiry_weekday()
    if current_expiry_wd is not None:
        print(f"    Current expiry day: {DAY_NAMES[current_expiry_wd]}")
    else:
        print("    Could not fetch — using state value as fallback.")
        current_expiry_wd = state["expiry_weekday"]

    # 2. Detect changes
    print("\n[3] Comparing with last known regime...")
    changes  = []
    patches  = []

    lot_changed    = (current_lot != state["lot_size"])
    expiry_changed = (current_expiry_wd != state["expiry_weekday"])

    if lot_changed:
        msg = (f"Lot size changed: {state['lot_size']} → {current_lot} "
               f"(effective today or upcoming)")
        print(f"    CHANGE: {msg}")
        changes.append(msg)
    else:
        print(f"    Lot size unchanged: {current_lot}")

    if expiry_changed:
        old_d = DAY_NAMES[state["expiry_weekday"]]
        new_d = DAY_NAMES[current_expiry_wd]
        msg = f"Expiry day changed: {old_d} → {new_d}"
        print(f"    CHANGE: {msg}")
        changes.append(msg)
    else:
        old_d = DAY_NAMES[state["expiry_weekday"]]
        print(f"    Expiry day unchanged: {old_d}")

    if not changes and not force:
        print("\n    No regime changes detected. Exiting without analysis.")
        state["last_check"] = today.isoformat()
        save_state(state, dry_run=dry)
        return

    # 3. Refresh data
    print("\n[4] Refreshing market data for fresh backtest...")
    if not dry:
        refresh_data()
    else:
        print("    [DRY RUN] Skipping data refresh.")

    # 4. Run backtest on new regime
    # Use 6 months of history for the new regime
    regime_start = (today - timedelta(days=180)).isoformat()
    print(f"\n[5] Running backtest from {regime_start} (6-month new-regime window)...")
    backtest_results = run_backtest(regime_start_date=regime_start)

    if backtest_results:
        print("    Results:")
        for k, v in backtest_results.items():
            if v["trades"] > 0:
                label = STRATEGY_LABELS.get(k, k)
                print(f"      {label:35s}  WR {v['wr']:.0%}  "
                      f"P&L ₹{v['total_pnl']:>10,.0f}  ({v['trades']} trades)")
    else:
        print("    Backtest produced no results.")

    # 5. Pick best strategy
    best_strategy = pick_best_strategy(backtest_results)
    if best_strategy:
        print(f"\n[6] Best strategy: {STRATEGY_LABELS.get(best_strategy, best_strategy)}")
    else:
        print("\n[6] Could not determine best strategy from results.")

    # 6. Auto-patch lot size if changed
    if lot_changed:
        print(f"\n[7] Auto-patching LOT_SIZE in auto_trader.py: "
              f"{state['lot_size']} → {current_lot}...")
        old_lot, patched = patch_lot_size(current_lot, dry_run=dry)
        if patched:
            patches.append(f"auto_trader.py: LOT_SIZE updated {old_lot} → {current_lot}")
    else:
        print("\n[7] LOT_SIZE unchanged — no patch needed.")

    # 6b. Monthly Bull Put margin freshness check
    print("\n[7b] Checking Bull Put margin fallback freshness...")
    live_margin, margin_patched, margin_msg = check_and_patch_bull_put_margin(dry_run=dry)
    if margin_patched and margin_msg:
        patches.append(f"auto_trader.py: {margin_msg}")

    # 7. Update state
    new_state = dict(state)
    new_state["lot_size"]        = current_lot
    new_state["expiry_weekday"]  = current_expiry_wd
    new_state["expiry_pattern"]  = f"weekly_{DAY_NAMES[current_expiry_wd].lower()}"
    new_state["best_strategy"]   = best_strategy or state.get("best_strategy")
    new_state["last_check"]      = today.isoformat()
    new_state["last_backtest"]   = today.isoformat()
    if changes:
        new_state["last_change"] = today.isoformat()
    save_state(new_state, dry_run=dry)

    # 8. Send Telegram report
    print("\n[8] Sending Telegram report...")
    send_regime_report(changes, backtest_results, best_strategy, patches, dry_run=dry)

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    main()
