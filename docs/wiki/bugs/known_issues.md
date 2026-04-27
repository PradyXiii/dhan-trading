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

## Time / date traps (April 2026 session — UTC vs IST + Dhan publish lag)

### `data_fetcher` "up to date" but CSV is days stale
- **Symptom:** Manual run after midnight IST logs `Nifty50: up to date (last row = 2026-04-27)` but `nifty50.csv` actually ends Friday 24 Apr.
- **Cause:** `TO_DATE = datetime.today()` returns UTC date — yesterday's date until 5:30 AM IST. Combined with `_last_csv_date()` advancing Sat/Sun → next Monday, `from_date >= TO_DATE` evaluates True and the fetch silently skips.
- **Fix:** `TO_DATE = datetime.now(_IST_TZ).strftime("%Y-%m-%d")` — IST-anchored. Commit `271e5c1`.
- **Lesson:** Every `datetime.today()` / `date.today()` / `datetime.now()` without explicit tz is a bug on a UTC-clocked VM. Search-and-destroy.

### `data_fetcher` drops Mon trading day after Friday last row
- **Symptom:** `nifty50.csv` stuck on Friday for days; `no new trading data (2026-04-25 — weekend/holiday)` logs.
- **Cause:** Dhan `/v2/charts/historical` returns DH-905 for the ENTIRE chunk if `fromDate` is Sat/Sun, even when trading days exist inside the range. `_last_csv_date` returned `last+1` blindly → Fri+1 = Sat → whole chunk dropped including the Mon trading day.
- **Fix:** Advance Sat/Sun → next Monday in `_last_csv_date()` before passing to API. Commit `c278a96`.

### Aggressive RuntimeError aborted morning trade on transient blip
- **Symptom:** One yfinance/Dhan chunk fails → all-or-nothing raise → `auto_trader.py` morning fetch dies → no trade.
- **Fix:** Collect failures, Telegram-warn, only raise when EVERY chunk failed (true outage). Successful chunks merged. Commit `d35ad14`.

### DH-905 ≠ failure
- **Rule:** Dhan returns DH-905 ("no data") for weekends, holidays, future dates, and pre-publish lag. NOT an error response. Treat as silent 0-rows. Bad scrip ID + DH-905 looks identical to "weekend, no data" — `data_fetcher` cannot distinguish. The all-fail abort only triggers on auth/server errors.

### Dhan EOD publish lag
- **Rule:** Daily index candles (Nifty50, BankNifty, India VIX via Dhan) are typically available ~30 minutes after 3:30 PM IST close, but can lag up to a few hours on volatile days. Manual fetches between 3:30 PM and the publish moment will return 0 rows for that day. Wait or schedule the fetch later. The 9:30 AM IST cron is well past the publish window.

### `signals_ml.csv` ahead of `nifty50.csv`
- **Cause:** `ml_engine --predict-today` writes today's prediction using the previous trading day's features as a fallback when today's row isn't in the underlying CSVs yet. Intentional — auto_trader needs a signal at 9:30 AM even if Dhan's candle isn't published.
- **Implication:** Don't use signals_ml.csv freshness as a proxy for raw-data freshness.

### Calendar days vs trading days in staleness checks
- **Rule:** `max_stale` in `validate_all.py` is in CALENDAR days. Mon→Fri = 3; Fri→Tue = 3 (1 missing trading day); Fri→Wed (Tue holiday) = 5. Setting `max_stale=2` thinking it's strict will false-fail every Tuesday morning.

---

## Atomicity / concurrency

### Non-atomic CSV/JSON writes corrupt state
- **Symptom:** Crash mid-write or concurrent reader sees half-truncated file. Affects `signals.csv`, `nifty50.csv`, `live_*_trades.csv`, `champion_meta.json`, `today_trade.json`, etc.
- **Fix:** All shared-state writes now go through `atomic_io.write_atomic_*` (tmpfile + `os.replace`). Never `df.to_csv(path)` or `open(path, "w")` directly. Commit `c997084`.

### `or` fallback masking real Dhan zeros
- **Symptom:** Dhan reports `buy_avg = 0.0` for an unfilled wing; `leg.get("buy_avg") or float(intent.get(...))` evaluated LHS as falsy and returned stale intent value → CSV row had a synthetic price not matching Dhan's ledger.
- **Fix:** Explicit `_pick(value, fallback)` helper with `is None` check. Genuine 0.0 from Dhan is preserved. Commit `c997084`.

---

## ML temporal-leakage traps

### HMM `predict_proba` is forward-BACKWARD smoother
- **Cause:** hmmlearn's `predict_proba(X)` computes smoothed posteriors using observations from t+1..T. `.shift(1)` only delays OUTPUT — doesn't unbias the estimator. Every walk-forward fold leaked future macro/VIX data.
- **Fix:** Manual numpy log-alpha forward pass (since hmmlearn 0.3.3 removed `_do_forward_pass`). Forward-only posteriors `P(s_t | x_0..x_t)`. Commit `f19e493`.

### Kalman `.smooth()` (RTS) leaks future
- **Cause:** RTS smoother conditions on x_{t+1}..x_T at every t. Shifted output still sees the future.
- **Fix:** Replace `.smooth()` with `.filter()` (forward-only Kalman). Commit `c997084`.

### Live-feedback rows leaked into model_evolver holdout
- **Cause:** `_load_live_outcomes` appended live rows at the END of `X_aug`; `_temporal_split` then took the last 252 rows as holdout — which were the freshly-injected, 10×-weighted live rows.
- **Fix:** Insert injected rows at index `len(X_all) - HOLDOUT_DAYS` so the last HOLDOUT_DAYS rows remain pristine historical data. Commit `c997084`.

### Champion ratchet trap after fixing leakage
- **Symptom:** After fixing HMM/Kalman/holdout leaks, new clean composite ~0.54 < leaked baseline 0.736 → ratchet refused to overwrite. Production stuck on the leaked champion.
- **Rule:** When fixing temporal-leakage that drops the score, manually wipe `models/champion_meta.json` (and `champion.pkl`) so the next evolver run sets a fresh apples-to-apples baseline. Don't fight the ratchet.

---

## Alerting / observability

### `notify.send` silently succeeded on missing creds
- **Symptom:** All alerts looked successful in code; health_ping reported green. Operator never knew alerts weren't being delivered.
- **Fix:** Return `False` + write `[CREDS MISSING — alert not sent]` stub to `data/critical_alerts.log` so health_ping can detect Telegram outage. Commit `c997084`.

### Telegram silently dropped on `<>&` in interpolated content
- **Symptom:** Daily `system_health` report disappeared when commit subjects, Claude API descriptions, or exception strings contained `<` / `>` / `&` → Telegram parse_mode=HTML 400 → message dropped silently. Critical-marker fallback log doesn't catch daily reports (📊 not 🚨).
- **Fix:** `html.escape()` every interpolated value via `_esc()` helper. Commit `c997084`.

### Non-critical 5xx/429 also vanish silently
- **Fix:** `notify.py` now writes a `[non-critical send failed HTTP <code>]` stub on every send failure, not just critical ones. Commit `c997084`.

---

## Parsing pitfalls

### `lstrip("json")` strips ANY leading char in {j,s,o,n}
- **Symptom:** `morning_brief.py` mangled valid JSON whose first key began with j/s/o/n into corrupted strings.
- **Fix:** Prefix-aware removal: `if inner.startswith("json"): inner = inner[4:].lstrip()`. Commit `c997084`.

### Regex with `re.MULTILINE` matches commented-out lines
- **Symptom:** `autoexperiment_backtest.py` parsed `# SL_PCT = 0.05` (commented-out experiment) as a real assignment.
- **Fix:** Strip comment-only lines before regex match. Commit `c997084`.

---

## Related pages
- [[strategy/ic_research]] — strategy-level bugs and routing decisions
- [[features/feature_history]] — feature-level bugs and column name gotchas
- [[live/data_integrity]] — Dhan journal, audit cron, and P&L sourcing rules
