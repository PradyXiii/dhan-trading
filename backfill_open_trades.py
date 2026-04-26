#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
backfill_open_trades.py — One-shot script to close out 3 OPEN trade rows
that exit_positions.py squared off in Dhan but trade_journal.py never updated.

Trades being backfilled (clean strategy P&L only — excludes Apr 22 lot-sizing
bug trades that accidentally added ₹770 to the day total):

  Apr 22 (Wed) — IC          → -₹250.00 (loss)
  Apr 23 (Thu) — Bear Call   → +₹415.71 (profit)
  Apr 24 (Fri) — IC          → -₹350.04 (loss)

Operation: in-place update of OPEN rows in:
  data/live_ic_trades.csv      (Apr 22, Apr 24 — IC)
  data/live_spread_trades.csv  (Apr 23 — Bear Call)

Run once. Idempotent — re-running won't double-update (checks exit_reason).
"""

import csv
import shutil
from pathlib import Path
from datetime import datetime

_HERE = Path(__file__).parent
_DATA = _HERE / "data"

IC_CSV     = _DATA / "live_ic_trades.csv"
SPREAD_CSV = _DATA / "live_spread_trades.csv"

# ─── trades to backfill ──────────────────────────────────────────────────────

IC_BACKFILL = {
    "2026-04-22": {
        "exit_reason":        "EOD",
        "exit_time":          "15:18:42",
        "pnl_inr":            "-250.00",
        # net_credit was 134.55/lot × 65 = ₹8745.75 collected → loss as % of credit
        "pnl_pct_of_credit":  f"{-250.00 / (134.55 * 65) * 100:.2f}",
        "oracle_correct":     "false",   # IC loss → market broke a wing
    },
    "2026-04-24": {
        "exit_reason":        "EOD",
        "exit_time":          "15:15:03",
        "pnl_inr":            "-350.04",
        # net_credit was 128.55/lot × 65 = ₹8355.75 collected
        "pnl_pct_of_credit":  f"{-350.04 / (128.55 * 65) * 100:.2f}",
        "oracle_correct":     "false",   # IC loss
    },
}

SPREAD_BACKFILL = {
    "2026-04-23": {
        "short_exit":         "165.95",
        "long_exit":          "104.10",
        "exit_spread":        "61.85",
        "exit_reason":        "EOD",
        "exit_time":          "15:16:52",
        "pnl_inr":            "415.71",
        # net_credit = 66.50/lot × 130 (2 lots × 65) = ₹8645 collected
        "pnl_pct_of_credit":  f"{415.71 / (66.50 * 130) * 100:.2f}",
        "oracle_correct":     "true",    # Bull Put win on PUT signal day
    },
}


def _backfill(csv_path: Path, backfill_map: dict) -> int:
    """Read CSV, update matching date rows in place, write back. Returns rows updated."""
    if not csv_path.exists():
        print(f"  ⚠️  {csv_path.name} missing — skipping")
        return 0

    # backup
    backup = csv_path.with_suffix(csv_path.suffix + ".pre-backfill.bak")
    if not backup.exists():
        shutil.copy(csv_path, backup)
        print(f"  💾 backup saved: {backup.name}")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    updated = 0
    for row in rows:
        date = row.get("date", "")
        if date not in backfill_map:
            continue
        # Idempotency: skip if already closed
        if (row.get("exit_reason") or "").strip().upper() not in ("", "OPEN", "PENDING"):
            print(f"  ⏭️  {date}: already closed (exit_reason={row['exit_reason']}) — skipping")
            continue
        for k, v in backfill_map[date].items():
            row[k] = v
        updated += 1
        print(f"  ✅ {date}: backfilled → pnl_inr={row['pnl_inr']}, exit_reason={row['exit_reason']}")

    if updated > 0:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  💾 {csv_path.name} written ({updated} row{'s' if updated != 1 else ''} updated)")
    else:
        print(f"  ℹ️  {csv_path.name}: no rows needed update")

    return updated


def main():
    print(f"\n═══ BACKFILL OPEN TRADES — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ═══\n")
    print(f"📄 {IC_CSV.name}")
    n_ic = _backfill(IC_CSV, IC_BACKFILL)
    print()
    print(f"📄 {SPREAD_CSV.name}")
    n_sp = _backfill(SPREAD_CSV, SPREAD_BACKFILL)
    print(f"\n═══ DONE — {n_ic + n_sp} total rows updated ═══")
    print("\nVerify: python3 system_health.py\n")


if __name__ == "__main__":
    main()
