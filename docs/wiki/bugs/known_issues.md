# Known Issues — Session-Discovered Bugs

Every entry = a bug debugged once, never again.  
Source: CLAUDE.md Known Gotchas table (synchronized at each wiki compile).  
**Last updated:** 2026-04-26

---

## ML / Feature bugs

### `_c` loop variable shadow
- **Symptom:** `could not convert string to float: 'pe_oi_p3'` — column name where price number expected
- **Cause:** `_c` is reserved for NF close price series in `compute_features()`. Using it as a loop variable overwrites it.
- **Fix:** Rename loop var to `_oi_col`, `_col`, or `_k`

### New yfinance CSV has only 1 row
- **Symptom:** Feature shows `0.000` importance in `--analyze` output
- **Cause:** New CSV created with only today's row — no history
- **Fix:** `python3 data_fetcher.py --backfill`

### Duplicate name in FEATURE_COLS
- **Symptom:** One feature's importance doubled; model wastes capacity
- **Fix:** After any FEATURE_COLS edit: `python3 -c "from ml_engine import FEATURE_COLS; print(len(FEATURE_COLS), len(set(FEATURE_COLS)))"` — both numbers must match

### `options_iv_skew.csv` missing
- **Symptom:** `call_skew`, `put_skew`, `skew_spread` all zero
- **Fix:** `python3 data_fetcher.py --fetch-options`

### ORB data empty before Aug 2021
- **Symptom:** `orb_range_pct` shows 0.000 for 2019–2021
- **Cause:** Dhan intraday API has no data before mid-2021 — expected behavior

### ML feature reads `d["close"]` instead of `d["nf_close"]`
- **Symptom:** autoexperiment crashes with composite=0.0
- **Cause:** NF close column is `d["nf_close"]`, not `d["close"]`
- **Fix:** Always use `d["nf_close"]` in any feature that references NF close price

### Lazy-imported lib shows 0.000 importance
- **Symptom:** hmmlearn / pykalman / arch feature importance = 0.000
- **Cause:** Library not pip-installed; import silently skipped
- **Fix:** `pip install --break-system-packages <lib>` on Debian VM (PEP 668 blocks plain pip)

---

## Strategy / routing bugs

### NF expiry day assumed Thursday
- **Symptom:** Code used Thursday as expiry day in calculations
- **Cause:** Pre-Sep-2025 fallback code was stale
- **Fix:** NSE changed NF expiry **Thursday→Tuesday from Sep 1 2025** (NSE circular). Always use Dhan expirylist API, never infer from code.

### Backtest DOW breakdown shows Thursday as best day
- **Symptom:** Historical DOW stats show Thursday peak — used to justify skipping Wednesday
- **Cause:** 2021–Aug 2025 data = Thursday expiry (6 years). Sep 2025–present = Tuesday (7 months). Thu pattern dominates stats.
- **Fix:** Use only Sep 2025+ data for current regime. `IC_SKIP_DAYS = set()` — no skip days.

### IC lot sizing gave 5 lots instead of 1
- **Cause:** Used formula `RISK_PCT × capital / risk_per_lot` instead of actual Dhan SPAN margin
- **Fix:** Always call `/v2/margincalculator/multi` for actual margin. `lots = floor(capital / actual_margin)`. Guard with `min(MAX_LOTS, capital // margin_1lot)`.
- **Incident:** Apr 22 2026 — qty=325 on 2 of 6 IC legs. Trade excluded from ledger. `EXCLUDED_DATES = {2026-04-22}`.

### Bull Put lot sizing over-sized
- **Cause:** `max_loss_per_lot` used as margin proxy. 150pt spread → formula gave 10 lots, actual ≈2 affordable
- **Fix:** `_fetch_spread_margin_per_lot()` queries Dhan API for actual SPAN

### IC on PUT signal days (wrong routing)
- **Symptom:** IC ran on both CALL and PUT days — wasted Bull Put edge
- **Fix:** `_use_bull_put_today = (signal == "PUT")`. Do not revert.

### Bear Call ran live despite being permanently dumped
- **Symptom:** Bear Call placed on Apr 23 2026 — strategy was supposed to be excluded
- **Cause:** Pre-Apr-2026-finalization code path still present in auto_trader.py
- **Fix:** Routing hardcoded to IC (CALL days) + Bull Put (PUT days). Audit auto_trader.py to confirm no Bear Call placement path remains.

### Wrong exit leg order
- **Symptom:** Closing long (wing) before short → naked short exposure → Dhan margin call
- **Fix:** Always close shorts first. IC: CE short → PE short → CE long → PE long

### filter_nf_options matched BANKNIFTY
- **Symptom:** weekly_audit on Apr 13–17 saw BNF trades and "could-not-detect-strategy" 4× failures
- **Cause:** BANKNIFTY contains "NIFTY" string — substring match was too broad
- **Fix:** Explicit reject list for BANKNIFTY/FINNIFTY/MIDCPNIFTY before NIFTY containment check

---

## Trade journal / data integrity bugs

### oracle_correct=False written for OPEN rows
- **Symptom:** trade_journal wrote oracle_correct=False for trades still open (pnl_inr=0 at OPEN time)
- **Cause:** oracle_correct computed as `pnl_inr > 0` — zero triggers False for undecided trades
- **Fix:** Drop OPEN rows from oracle compute branch. `_upsert_csv_row` replaces same-date rows so re-running journal upgrades OPEN→closed without duplicates.

### model_evolver blind to Bull Put / Bear Call / Straddle CSVs
- **Symptom:** `_load_live_outcomes` only read live_ic_trades.csv — other strategy trades invisible to evolver
- **Fix:** `LIVE_TRADES_PATHS` list reads all 3 CSVs (IC, Bull Put/Bear Call, Straddle)

### exit_positions early-exit did not update today_trade.json
- **Symptom:** trade_journal at 3:30 read stale OPEN state after early exit
- **Cause:** Early-exit path wrote marker file but skipped today_trade.json update
- **Fix:** Write minimal EOD fallback to today_trade.json before writing marker

### Hand-typed proxy P&L diverges from Dhan-booked
- **Symptom:** Apr 22: hand=-₹250 vs Dhan=+₹98
- **Rule:** Every CSV value in live_*_trades.csv must come from a Dhan API field
- **Fix:** dhan_journal.py reads `/v2/positions` for live P&L, `/v2/trades/{from}/{to}/{page}` for backfill. backfill_dhan_history.py replaces backfill_open_trades.py.

---

## API / format bugs

### `pnlExit` value format wrong
- **Symptom:** Non-ACTIVE response parsing failed
- *(details from prior session — see CLAUDE.md for full trace)*

---

## Related pages
- [[strategy/ic_research]] — strategy-level bugs and routing decisions
- [[features/feature_history]] — feature-level bugs and column name gotchas
- [[live/data_integrity]] — Dhan journal, audit cron, and P&L sourcing rules
