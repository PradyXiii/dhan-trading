#!/usr/bin/env python3
"""
lot_expiry_scanner.py — Monthly NSE/Dhan scanner for BankNifty contract changes.

Problem
-------
NSE and SEBI change BankNifty lot size and expiry day frequently. Recent history:
  - Apr 2024: SEBI mandated min contract value ₹15L → lot 15 → 30 (effective Nov 20 2024)
  - Jun 2025: lot 30 → 35 on first post-mandate monthly contract
  - Jan 2026: lot 35 → 30 revision
  - Sep 2025: expiry shifted from last Wednesday → last Tuesday

Extensions are common: an announced change can be postponed. The scanner handles this
by storing pending changes with effective dates and updating them in place if revised.

Sources
-------
  1. NSE fo_mktlots.csv    — primary. Contains current + next ~3 months of contracts
                              with lot sizes per contract month → effective dates derivable.
  2. Dhan option chain API — fallback. Live but only shows current active contract.
  3. Dhan expirylist API   — for expiry day detection (auto_trader already uses this).

Outputs (all in data/, gitignored — machine-local state)
--------------------------------------------------------
  data/lot_size_overrides.json   — active + pending lot size overrides
  data/expiry_status.json        — last known expiry day pattern
  data/scanner.log               — append-only scan history

Behavior
--------
  - Baseline = backtest_engine.get_lot_size_baseline(date) hardcoded timeline
  - If NSE/Dhan reports current lot ≠ baseline → mismatch detected
  - Mismatch stored as entry with effective_date = first day NSE reports new size
  - On each scan:
      · Pending entries whose effective_date ≤ today become ACTIVE
      · Pending entries with shifted effective_date are updated in place (extension)
      · Stale entries (effective long past, value matches baseline) auto-pruned
  - Telegram alert sent on any new detection or activation
  - Never modifies Python source code — only writes to data/*.json

Usage
-----
  python3 lot_expiry_scanner.py              # normal scan
  python3 lot_expiry_scanner.py --dry-run    # scan but don't persist
  python3 lot_expiry_scanner.py --force      # send alert even if no change
  python3 lot_expiry_scanner.py --show       # print current override state

Cron (monthly, 1st of month at 10 AM IST = 4:30 AM UTC)
  30 4 1 * * cd /home/user/dhan-trading && python3 lot_expiry_scanner.py
"""

import os
import sys
import json
import csv
import io
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import get_lot_size as baseline_get_lot_size

DATA_DIR          = Path("data")
OVERRIDES_FILE    = DATA_DIR / "lot_size_overrides.json"
EXPIRY_STATUS     = DATA_DIR / "expiry_status.json"
SCAN_LOG          = DATA_DIR / "scanner.log"

NSE_LOT_URL_NEW   = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_LOT_URL_OLD   = "https://www1.nseindia.com/content/fo/fo_mktlots.csv"
NSE_HOME_URL      = "https://www.nseindia.com"

DHAN_TOKEN        = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID", "")
DHAN_HEADERS      = {
    "access-token": DHAN_TOKEN,
    "client-id":    DHAN_CLIENT_ID,
    "Content-Type": "application/json",
}

BN_SYMBOL_VARIANTS = ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK", "BANK NIFTY")
MONTH_MAP = {
    "JAN": 1,  "FEB": 2,  "MAR": 3,  "APR": 4,  "MAY": 5,  "JUN": 6,
    "JUL": 7,  "AUG": 8,  "SEP": 9,  "OCT": 10, "NOV": 11, "DEC": 12,
}


# ─────────────────────────────────────────────────────────────────────────────
#  NSE lot size CSV fetch + parse
# ─────────────────────────────────────────────────────────────────────────────

def _nse_session():
    """NSE blocks default UAs. Visit home first to get cookies, then fetch CSV."""
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/121.0 Safari/537.36",
        "Accept":          "text/csv,text/plain,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer":         NSE_HOME_URL + "/",
    })
    try:
        s.get(NSE_HOME_URL, timeout=10)
    except Exception:
        pass
    return s


def fetch_nse_lot_csv():
    """
    Fetch fo_mktlots.csv from NSE. Returns list of dicts for BANKNIFTY:
      [{"year": 2026, "month": 4, "lot_size": 30, "col_label": "APR-26"}, ...]
    First column tried: new archives URL. Falls back to old www1 URL.
    """
    session = _nse_session()
    raw = None
    for url in (NSE_LOT_URL_NEW, NSE_LOT_URL_OLD):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200 and len(r.text) > 500 and "SYMBOL" in r.text.upper():
                raw = r.text
                break
        except Exception as e:
            print(f"  NSE fetch failed ({url}): {e}")
    if raw is None:
        return None

    # Parse CSV. NSE files are space-padded and sometimes have comments — clean up.
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    if not rows:
        return None

    header = [h.strip().upper() for h in rows[0]]
    # Locate SYMBOL column
    try:
        sym_idx = next(i for i, h in enumerate(header) if "SYMBOL" in h)
    except StopIteration:
        return None

    # Find columns that look like MONTH-YEAR contracts (e.g. APR-26, MAY-26, 26-APR, APR2026)
    month_cols = []
    for i, h in enumerate(header):
        m = re.search(r"([A-Z]{3}).{0,3}(\d{2,4})", h)
        if m and m.group(1) in MONTH_MAP:
            year = int(m.group(2))
            if year < 100:
                year += 2000
            month_cols.append((i, h, MONTH_MAP[m.group(1)], year))
        else:
            m2 = re.search(r"(\d{2,4}).{0,3}([A-Z]{3})", h)
            if m2 and m2.group(2) in MONTH_MAP:
                year = int(m2.group(1))
                if year < 100:
                    year += 2000
                month_cols.append((i, h, MONTH_MAP[m2.group(2)], year))

    if not month_cols:
        return None

    # Find BANKNIFTY row
    bn_row = None
    for row in rows[1:]:
        if len(row) <= sym_idx:
            continue
        sym = row[sym_idx].strip().upper()
        if sym in BN_SYMBOL_VARIANTS or "BANKNIFTY" in sym.replace(" ", ""):
            bn_row = row
            break
    if bn_row is None:
        return None

    contracts = []
    for col_idx, label, month, year in month_cols:
        if col_idx >= len(bn_row):
            continue
        val = bn_row[col_idx].strip()
        try:
            lot = int(float(val))
            if lot > 0:
                contracts.append({
                    "year":      year,
                    "month":     month,
                    "lot_size":  lot,
                    "col_label": label,
                })
        except ValueError:
            continue

    contracts.sort(key=lambda c: (c["year"], c["month"]))
    return contracts or None


# ─────────────────────────────────────────────────────────────────────────────
#  Dhan fallback — current lot size + upcoming expiries
# ─────────────────────────────────────────────────────────────────────────────

def fetch_dhan_current_lot():
    """
    Query Dhan instrument master for BANKNIFTY FUT, extract lot size.
    Returns int or None.
    """
    if not DHAN_TOKEN or not DHAN_CLIENT_ID:
        return None
    try:
        # Dhan's scrip master CSV has all contract details
        r = requests.get(
            "https://images.dhan.co/api-data/api-scrip-master.csv",
            timeout=30,
        )
        if r.status_code != 200:
            return None
        # BANKNIFTY FUT/OPT rows contain SEM_LOT_UNITS column
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            sym = (row.get("SEM_TRADING_SYMBOL", "") or "").upper()
            name = (row.get("SM_SYMBOL_NAME", "") or "").upper()
            inst = (row.get("SEM_INSTRUMENT_NAME", "") or "").upper()
            if ("BANKNIFTY" in sym or "BANKNIFTY" in name) and inst in ("OPTIDX", "FUTIDX"):
                lot = row.get("SEM_LOT_UNITS") or row.get("SEM_LOT_SIZE") or ""
                try:
                    lot_int = int(float(lot))
                    if lot_int > 0:
                        return lot_int
                except ValueError:
                    continue
    except Exception as e:
        print(f"  Dhan lot fetch failed: {e}")
    return None


def fetch_dhan_expirylist():
    """
    Get upcoming BankNifty expiries from Dhan. Returns list of date objects.
    """
    if not DHAN_TOKEN or not DHAN_CLIENT_ID:
        return []
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=DHAN_HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        expiries = r.json().get("data", [])
        parsed = []
        for e in expiries:
            try:
                parsed.append(date.fromisoformat(e))
            except Exception:
                continue
        return sorted(parsed)
    except Exception as e:
        print(f"  Dhan expirylist failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Overrides file I/O
# ─────────────────────────────────────────────────────────────────────────────

def load_overrides():
    if not OVERRIDES_FILE.exists():
        return {"active": [], "pending": [], "last_scan": None, "last_source": None}
    try:
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    except Exception:
        return {"active": [], "pending": [], "last_scan": None, "last_source": None}


def save_overrides(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(OVERRIDES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_expiry_status():
    if not EXPIRY_STATUS.exists():
        return {"expected_day": "tuesday", "last_verified": None, "pattern": "monthly_last_tuesday"}
    try:
        with open(EXPIRY_STATUS) as f:
            return json.load(f)
    except Exception:
        return {"expected_day": "tuesday", "last_verified": None, "pattern": "monthly_last_tuesday"}


def save_expiry_status(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(EXPIRY_STATUS, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
#  Detection logic
# ─────────────────────────────────────────────────────────────────────────────

def month_start_date(year, month):
    return date(year, month, 1)


def detect_lot_changes(nse_contracts, overrides, today):
    """
    Compare NSE contracts vs baseline + overrides. Return list of change events.

    A "change event" is:
      {
        "effective_date": "YYYY-MM-DD",
        "lot_size":       int,
        "note":           "detected from NSE contract <label>",
        "detected_at":    iso timestamp,
        "source":         "nse_csv"
      }
    """
    if not nse_contracts:
        return []

    events = []
    prev_lot = None
    for c in nse_contracts:
        contract_start = month_start_date(c["year"], c["month"])
        # Baseline for this date = hardcoded + any already-active overrides
        effective_lot = effective_lot_size_on(contract_start, overrides)
        if prev_lot is None:
            prev_lot = effective_lot

        if c["lot_size"] != effective_lot:
            events.append({
                "effective_date": contract_start.isoformat(),
                "lot_size":       c["lot_size"],
                "note":           f"NSE contract {c['col_label']} reports lot={c['lot_size']}, "
                                  f"expected={effective_lot}",
                "detected_at":    datetime.now().isoformat(timespec="seconds"),
                "source":         "nse_csv",
            })
        prev_lot = c["lot_size"]
    return events


def effective_lot_size_on(d, overrides):
    """
    Return the currently-effective lot size for a given date.
    Priority: active overrides (sorted) → baseline hardcoded.
    """
    baseline = baseline_get_lot_size(d)
    # Active overrides take precedence if their effective_date ≤ d
    best = baseline
    best_eff = date(1900, 1, 1)
    for ov in overrides.get("active", []):
        try:
            eff = date.fromisoformat(ov["effective_date"])
        except Exception:
            continue
        if eff <= d and eff >= best_eff:
            best = ov["lot_size"]
            best_eff = eff
    return best


def merge_pending(existing_pending, new_events):
    """
    Merge new events into pending list, handling extensions.
    Keyed by (lot_size, year-month of detection) to catch shifted effective dates.
    """
    merged = list(existing_pending)
    for ev in new_events:
        # Look for an existing pending entry with same lot_size that might be an extension
        match_idx = None
        for i, p in enumerate(merged):
            if p["lot_size"] == ev["lot_size"]:
                # Same direction, possibly extended
                match_idx = i
                break
        if match_idx is not None:
            old = merged[match_idx]
            if old["effective_date"] != ev["effective_date"]:
                ev["note"] = f"EXTENSION: shifted from {old['effective_date']} → {ev['effective_date']}"
                ev["previous_effective"] = old["effective_date"]
            merged[match_idx] = ev
        else:
            merged.append(ev)
    return merged


def promote_pending_to_active(overrides, today):
    """
    Any pending entry whose effective_date ≤ today moves to active.
    Returns list of promoted entries (for alert).
    """
    promoted = []
    still_pending = []
    for p in overrides.get("pending", []):
        try:
            eff = date.fromisoformat(p["effective_date"])
        except Exception:
            still_pending.append(p)
            continue
        if eff <= today:
            p["promoted_at"] = datetime.now().isoformat(timespec="seconds")
            overrides.setdefault("active", []).append(p)
            promoted.append(p)
        else:
            still_pending.append(p)
    overrides["pending"] = still_pending
    return promoted


# ─────────────────────────────────────────────────────────────────────────────
#  Expiry day scanner
# ─────────────────────────────────────────────────────────────────────────────

def detect_expiry_drift(expiries, expected_weekday=1):
    """
    expected_weekday: 1=Tuesday, 2=Wednesday, 3=Thursday
    Phase 4 expects monthly last-Tuesday, so weekday=1.

    Returns None if all good, or dict with drift details.
    """
    if not expiries:
        return None
    unexpected = [e for e in expiries[:4] if e.weekday() != expected_weekday]
    if not unexpected:
        return None
    return {
        "expected_weekday": expected_weekday,
        "expected_name":    ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][expected_weekday],
        "anomalies": [
            {"date": e.isoformat(),
             "actual_day": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][e.weekday()]}
            for e in unexpected
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Telegram alert
# ─────────────────────────────────────────────────────────────────────────────

def send_alert(title, lines):
    try:
        import notify
        msg = f"🔔 <b>{title}</b>\n\n" + "\n".join(lines)
        notify.send(msg)
    except Exception as e:
        print(f"  Telegram send failed: {e}")


def log_scan(summary):
    DATA_DIR.mkdir(exist_ok=True)
    with open(SCAN_LOG, "a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {summary}\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def show_state():
    ov = load_overrides()
    es = load_expiry_status()
    print("── Lot size overrides ─────────────────────────────")
    print(json.dumps(ov, indent=2, default=str))
    print("\n── Expiry status ──────────────────────────────────")
    print(json.dumps(es, indent=2, default=str))
    print(f"\n── Baseline for today ({date.today()}): {baseline_get_lot_size(date.today())}")
    print(f"── Effective for today:                    {effective_lot_size_on(date.today(), ov)}")


def main():
    args = sys.argv[1:]
    if "--show" in args:
        show_state()
        return

    dry_run = "--dry-run" in args
    force   = "--force" in args
    today   = date.today()

    print(f"BankNifty lot/expiry scanner — {today}")
    print(f"{'='*60}")

    overrides = load_overrides()
    alert_lines = []

    # 1. Fetch NSE CSV (primary)
    print("\n[1] Fetching NSE fo_mktlots.csv...")
    nse_contracts = fetch_nse_lot_csv()
    if nse_contracts:
        print(f"    Parsed {len(nse_contracts)} BN contracts:")
        for c in nse_contracts[:6]:
            print(f"      {c['col_label']:10s} ({c['year']}-{c['month']:02d}): lot = {c['lot_size']}")
        source = "nse_csv"
    else:
        print("    NSE fetch/parse failed — falling back to Dhan.")
        source = None

    # 2. Dhan fallback for current lot
    dhan_lot = None
    if not nse_contracts:
        print("\n[2] Fetching Dhan scrip master for current lot...")
        dhan_lot = fetch_dhan_current_lot()
        if dhan_lot:
            print(f"    Dhan current BN lot size: {dhan_lot}")
            source = "dhan_api"
        else:
            print("    Dhan fetch failed.")

    # 3. Detect changes
    print("\n[3] Detecting changes vs baseline...")
    baseline_today = baseline_get_lot_size(today)
    effective_today = effective_lot_size_on(today, overrides)
    print(f"    Baseline lot for today: {baseline_today}")
    print(f"    Effective (with overrides): {effective_today}")

    new_events = []
    if nse_contracts:
        new_events = detect_lot_changes(nse_contracts, overrides, today)
        for ev in new_events:
            print(f"    DETECTED: {ev['note']}")
            alert_lines.append(
                f"• <b>Lot {ev['lot_size']}</b> effective {ev['effective_date']}\n  {ev['note']}"
            )
    elif dhan_lot and dhan_lot != effective_today:
        ev = {
            "effective_date": today.isoformat(),
            "lot_size":       dhan_lot,
            "note":           f"Dhan reports current lot={dhan_lot}, expected={effective_today}",
            "detected_at":    datetime.now().isoformat(timespec="seconds"),
            "source":         "dhan_api",
        }
        new_events.append(ev)
        print(f"    DETECTED: {ev['note']}")
        alert_lines.append(f"• <b>Lot {dhan_lot}</b> active now\n  {ev['note']}")
    else:
        print("    No lot size changes detected.")

    # 4. Merge pending (handles extensions)
    if new_events:
        overrides["pending"] = merge_pending(overrides.get("pending", []), new_events)

    # 5. Promote pending whose effective date has arrived
    print("\n[4] Promoting pending changes whose effective date has arrived...")
    promoted = promote_pending_to_active(overrides, today)
    for p in promoted:
        print(f"    ACTIVATED: lot={p['lot_size']} effective {p['effective_date']}")
        alert_lines.append(
            f"✅ <b>ACTIVATED: Lot {p['lot_size']}</b> (effective {p['effective_date']})"
        )

    # 6. Expiry day scan
    print("\n[5] Checking expiry day pattern...")
    expiries = fetch_dhan_expirylist()
    expiry_status = load_expiry_status()
    drift = None
    if expiries:
        print(f"    Next expiries: {[e.isoformat() for e in expiries[:4]]}")
        drift = detect_expiry_drift(expiries, expected_weekday=1)  # Tuesday
        if drift:
            print(f"    DRIFT: expected {drift['expected_name']}, got:")
            for a in drift["anomalies"]:
                print(f"      {a['date']}  ({a['actual_day']})")
            alert_lines.append(
                f"⚠️ <b>Expiry drift detected</b>\n"
                f"  Expected last-{drift['expected_name']}, got:\n"
                + "\n".join(f"  • {a['date']} ({a['actual_day']})" for a in drift['anomalies'])
            )
        else:
            print(f"    All next 4 expiries on {['Mon','Tue','Wed','Thu','Fri'][1]} ✓")
            expiry_status["last_verified"] = today.isoformat()

    # 7. Persist + alert
    overrides["last_scan"]   = datetime.now().isoformat(timespec="seconds")
    overrides["last_source"] = source

    if dry_run:
        print("\n[DRY RUN] No files written.")
    else:
        save_overrides(overrides)
        save_expiry_status(expiry_status)
        log_scan(
            f"scan={source or 'none'} "
            f"baseline={baseline_today} "
            f"effective={effective_today} "
            f"new_events={len(new_events)} "
            f"promoted={len(promoted)} "
            f"expiry_drift={'yes' if drift else 'no'}"
        )

    if alert_lines or force:
        if not alert_lines:
            alert_lines = ["No changes detected — scanner running normally."]
        send_alert("BankNifty Contract Scanner", alert_lines)

    print(f"\n{'='*60}")
    print(f"Done. Log: {SCAN_LOG}")


if __name__ == "__main__":
    main()
