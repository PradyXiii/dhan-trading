# Known Issues — Session-Discovered Bugs

Every entry = a bug debugged once, never again.  
Source: CLAUDE.md Known Gotchas table (synchronized at each wiki compile).  
**Last updated:** 2026-04-23

---

## ML / Feature bugs

### `_c` loop variable shadow
- **Symptom:** `could not convert string to float: 'pe_oi_p3'` — column name where price number expected
- **Cause:** `_c` is reserved for BN close price series in `compute_features()`. Using it as a loop variable overwrites it.
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
- **Fix:** Always call `/v2/margincalculator/multi` for actual margin. `lots = floor(capital / actual_margin)`

### Bull Put lot sizing over-sized
- **Cause:** `max_loss_per_lot` used as margin proxy. 150pt spread → formula gave 10 lots, actual ≈2 affordable
- **Fix:** `_fetch_spread_margin_per_lot()` queries Dhan API for actual SPAN

### IC on PUT signal days (wrong routing)
- **Symptom:** IC ran on both CALL and PUT days — wasted Bull Put edge
- **Fix:** `_use_bull_put_today = (signal == "PUT")`. Do not revert.

### Wrong exit leg order
- **Symptom:** Closing long (wing) before short → naked short exposure → Dhan margin call
- **Fix:** Always close shorts first. IC: CE short → PE short → CE long → PE long

---

## API / format bugs

### `pnlExit` value format wrong
- **Symptom:** Non-ACTIVE response from Dhan pnlExit API silently
- **Cause:** `str(round(1500.0, 2))` = `"1500.0"` (one decimal). API needs `"1500.00"` (two decimals)
- **Fix:** `f"{value:.2f}"` everywhere — not `str(round(value, 2))`

### `DELETE /v2/positions` outside market hours
- **Symptom:** Returns non-SUCCESS if called after 3:30 PM or weekends
- **Fix:** Only call during 9:15 AM–3:30 PM IST. Backup leg-by-leg path handles edge cases automatically.

### regime_watcher.py lot size returns 120 instead of 65
- **Cause:** Substring filter `"NIFTY" in sym` matched "MIDCAP NIFTY" (lot=120) before Nifty50
- **Fix:** Use exact set matching `sym in {"NIFTY", "NIFTY50", "NIFTY 50"}` (fixed Apr 2026)

### `BULL_PUT_MARGIN_PER_LOT = 55_000` parsed as 55
- **Cause:** Regex `\d+` stops at underscore in Python integer literal `55_000`
- **Fix:** Use `[\d_]+` and parse with `.replace('_', '')` (fixed Apr 2026)

### IC TP removed: no TP = +₹21L over 5yr
- IC with TP=0.65: ₹1.17Cr. EOD-only (no TP): ₹1.38Cr. Same WR, less drawdown.
- **Fix:** `spread_monitor.py` IC path has SL only. IC always exits at 3:15 PM. Never add TP back.

---

## Related pages
- [[features/feature_history]] — feature-specific bugs
- [[strategy/ic_research]] — strategy decisions and their reasoning
