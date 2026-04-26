#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
weekly_audit.py — Saturday safety net for missing trade journal rows.

Runs every Saturday 7:30 AM IST. Walks last week's trading days (Mon–Fri),
checks Dhan tradebook for each. If Dhan shows NF F&O fills on a date but
the relevant CSV (live_ic_trades / live_spread_trades / live_straddle_trades)
has no row for that date → calls backfill_dhan_history.py to recover it.

Also flags rows that exist but have no exit_reason or empty oracle_correct
(the broken-row class) so they can be re-pulled from Dhan.

Catches:
  - 3:30 PM trade_journal cron failure (VM crash, API outage, etc.)
  - exit_positions / spread_monitor outages that left today_trade.json stale
  - any other gap between "Dhan executed a trade" and "CSV has a clean row"

Telegram report summarises: dates audited, missing rows recovered, broken
rows flagged. If everything is clean, sends a one-line "all clean" pulse.

Cron (7:30 AM IST Saturday = 2:00 UTC Sat):
  0 2 * * 6 cd ~/dhan-trading && python3 weekly_audit.py >> logs/weekly_audit.log 2>&1
"""
import csv
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import notify
import dhan_journal

_IST  = timezone(timedelta(hours=5, minutes=30))
_HERE = Path(__file__).parent
_DATA = _HERE / "data"

CSVS = {
    "ic":       _DATA / "live_ic_trades.csv",
    "spread":   _DATA / "live_spread_trades.csv",
    "straddle": _DATA / "live_straddle_trades.csv",
}

DRY_RUN = "--dry-run" in sys.argv


def _last_week_trading_days() -> list[str]:
    """Return Mon–Fri dates in the previous week (relative to today, IST)."""
    today = datetime.now(_IST).date()
    # Find last Monday: today - (weekday + 7) gets us 1+ weeks back's Monday
    days_since_mon = today.weekday()  # Mon=0
    last_mon = today - timedelta(days=days_since_mon + 7)
    return [(last_mon + timedelta(days=i)).isoformat() for i in range(5)]


def _csv_has_clean_row(csv_path: Path, target_date: str) -> tuple[bool, str]:
    """Returns (clean, reason). Clean = row exists, exit_reason set, oracle_correct in true/false."""
    if not csv_path.exists():
        return False, "csv-missing"
    try:
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        return False, f"csv-read-error: {e}"
    matches = [r for r in rows if r.get("date") == target_date]
    if not matches:
        return False, "no-row"
    r = matches[0]
    er = (r.get("exit_reason") or "").strip().upper()
    if er in ("", "OPEN", "PENDING"):
        return False, f"row-open ({er or 'blank'})"
    oc = str(r.get("oracle_correct", "")).strip().lower()
    if oc not in ("true", "false"):
        return False, f"oracle-blank ({r.get('oracle_correct','')!r})"
    return True, "ok"


def _dhan_had_trades(target_date: str) -> tuple[bool, int]:
    """Check Dhan tradebook for any NF F&O fills on this date."""
    try:
        raw = dhan_journal.fetch_trade_history(target_date, target_date)
    except Exception as e:
        notify.log(f"weekly_audit: tradebook fetch failed for {target_date}: {e}")
        return False, 0
    nf = dhan_journal.filter_nf_options(raw)
    return len(nf) > 0, len(nf)


def _detect_csv_for_date(target_date: str) -> Path | None:
    """Match the date's Dhan fills to a strategy → returns the right CSV."""
    raw = dhan_journal.fetch_trade_history(target_date, target_date)
    nf  = dhan_journal.filter_nf_options(raw)
    if not nf:
        return None
    grouped = dhan_journal.trades_by_sid(nf)
    n = len(grouped)
    if n >= 4:
        return CSVS["ic"]
    if n == 2:
        opts = set()
        for fills in grouped.values():
            opts.add(str(fills[0].get("drvOptionType", "")).upper())
        if opts == {"CALL", "PUT"} or opts == {"CE", "PE"}:
            return CSVS["straddle"]
        return CSVS["spread"]
    return None


def _run_backfill(target_date: str) -> bool:
    """Invoke backfill_dhan_history.py --date <date> --apply. Returns True on success."""
    if DRY_RUN:
        notify.log(f"weekly_audit: [DRY_RUN] would backfill {target_date}")
        return True
    cmd = ["python3", str(_HERE / "backfill_dhan_history.py"), "--date", target_date, "--apply"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True
        notify.log(f"weekly_audit: backfill failed for {target_date}: {result.stderr[:200]}")
        return False
    except Exception as e:
        notify.log(f"weekly_audit: backfill exception for {target_date}: {e}")
        return False


def main():
    audit_date = datetime.now(_IST).date().isoformat()
    notify.log(f"weekly_audit — start {audit_date}")
    days = _last_week_trading_days()

    missing      = []   # (date, reason) — needed backfill
    recovered    = []   # (date) — backfill succeeded
    failed       = []   # (date, reason) — backfill failed
    clean        = []   # (date) — already correct
    no_trade_day = []   # (date) — Dhan shows nothing, no row needed

    for d in days:
        had_trades, n_fills = _dhan_had_trades(d)
        if not had_trades:
            no_trade_day.append(d)
            continue

        csv_path = _detect_csv_for_date(d)
        if csv_path is None:
            failed.append((d, "could-not-detect-strategy"))
            continue

        clean_ok, reason = _csv_has_clean_row(csv_path, d)
        if clean_ok:
            clean.append(d)
            continue

        missing.append((d, f"{csv_path.name}: {reason}, {n_fills} fills on Dhan"))
        if _run_backfill(d):
            # Re-check after backfill
            re_clean, re_reason = _csv_has_clean_row(csv_path, d)
            if re_clean:
                recovered.append(d)
            else:
                failed.append((d, f"backfill ran but row still {re_reason}"))
        else:
            failed.append((d, "backfill subprocess failed"))

    # ── Telegram report ─────────────────────────────────────────────────────
    icon = "✅" if not missing and not failed else "⚠️"
    lines = [
        f"{icon} <b>Weekly Audit — {audit_date}</b>",
        f"Audited last week ({days[0]} → {days[-1]})",
        "",
    ]
    if clean:
        lines.append(f"✅ Already clean: {len(clean)} day(s)")
        for d in clean:
            lines.append(f"   • {d}")
    if no_trade_day:
        lines.append(f"⏭ No trade day: {len(no_trade_day)} day(s)  (Dhan tradebook empty — holiday or skip-signal)")
    if recovered:
        lines.append(f"🔧 Recovered missing rows: {len(recovered)}")
        for d in recovered:
            lines.append(f"   • {d}  → re-pulled from Dhan tradebook")
    if missing and not recovered:
        lines.append(f"⚠️ Missing rows detected: {len(missing)}")
        for d, why in missing:
            lines.append(f"   • {d}: {why}")
    if failed:
        lines.append(f"❌ Backfill failures: {len(failed)}")
        for d, why in failed:
            lines.append(f"   • {d}: {why}")
        lines.append("\n<b>Manual action needed</b> — investigate Dhan vs CSV.")

    if not missing and not failed:
        lines.append("\nNo gaps detected. Trade ledger consistent with Dhan tradebook.")

    notify.send("\n".join(lines))
    notify.log(f"weekly_audit — done. clean={len(clean)} recovered={len(recovered)} failed={len(failed)}")


if __name__ == "__main__":
    main()
