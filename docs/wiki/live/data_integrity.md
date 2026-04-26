# Live Data Integrity — Dhan Journal, Audit, and P&L Sourcing

**Last updated:** 2026-04-26

---

## Golden rule

Every CSV value in `live_*_trades.csv` must come from a Dhan API field.  
Hand-typed / proxy P&L is forbidden. Example failure: Apr 22 hand=-₹250 vs Dhan=+₹98.

---

## dhan_journal.py

| Purpose | Dhan endpoint |
|---|---|
| Live P&L (same-day open positions) | `/v2/positions` |
| Historical backfill | `/v2/trades/{from}/{to}/{page}` |

**backfill_dhan_history.py** replaces the old `backfill_open_trades.py`.

---

## weekly_audit.py — Saturday safety net

- **Cron:** `0 2 * * 6` (UTC) = Saturday 7:30 AM IST
- **Action:** Walks Mon–Fri of last week, fetches Dhan tradebook per date, cross-checks `live_*_trades.csv`
- **Auto-runs backfill** if any row is missing, still OPEN, or oracle-blank
- **`EXCLUDED_DATES = {2026-04-22}`** — Apr 22 excluded due to lot-sizing bug (qty=325, P&L +₹98 not a strategy signal)

---

## filter_nf_options — instrument filtering

- **Bug (fixed):** BANKNIFTY matched NIFTY substring → BNF trades ingested
- **Fix:** Explicit reject list: BANKNIFTY, FINNIFTY, MIDCPNIFTY checked before NIFTY containment
- **Symptom observed:** Apr 13–17 weekly_audit saw BNF trades and 4× "could-not-detect-strategy" failures

---

## model_evolver live outcome loading

- `LIVE_TRADES_PATHS` must include all 3 CSVs: IC, Bull Put/Bear Call, Straddle
- Old bug: only `live_ic_trades.csv` was read → evolver blind to other strategies

---

## trade_journal oracle correctness

- `oracle_correct` computed only for CLOSED rows (`pnl_inr != 0`)
- OPEN rows skipped — avoids False written for undecided trades
- `_upsert_csv_row` replaces same-date rows: re-running journal upgrades OPEN→closed without duplicates

---

## exit_positions EOD state

- Early-exit path must write minimal EOD fallback to `today_trade.json` before writing marker
- Failure mode: trade_journal at 3:30 reads stale OPEN state

---

## Related pages
- [[bugs/known_issues]] — full bug list including journal and audit bugs
- [[strategy/ic_research]] — Apr 22 lot-sizing incident, excluded dates
