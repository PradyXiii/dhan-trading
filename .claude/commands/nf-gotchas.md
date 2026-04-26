# NF Known Gotchas — Living Bug Library

When invoked, read these entries and apply them to the current task before writing any code.

---

## Reserved variable names — NEVER use in compute_features()

| Name | What it holds | If overwritten |
|---|---|---|
| `_c` | NF close price series (shifted) | Downstream features silently use column name strings as numbers |
| `_c_nf` | NF close (alternate reference) | Same |
| `_vix` | India VIX series | VIX features all NaN |
| `_sp` | S&P500 series | Global market features silent failure |
| `_nk` | Nikkei series | Same |

Symptom: `could not convert string to float: 'pe_oi_p3'` — a column name where a number was expected.
Fix: rename loop variable to `_oi_col`, `_col`, `_k`, etc.

---

## NF column naming — use `nf_close` not `close`

NF daily close column = `d["nf_close"]` (line ~373 in ml_engine.py).
Symptom: `{"error": "compute_features failed: 'close'", "composite": 0.0}` — entire pipeline crashes.
Fix: `pd.to_numeric(d["nf_close"], errors="coerce").ffill().bfill().values.astype(float)`

---

## Lazy-imported lib not installed → feature shows 0.000 importance

Symptom: HMM/Kalman/GARCH/Hurst features all 0.000 even though code is correct.
Cause: `try: import <lib>` block silently falls to `_LIB_OK = False` because lib never installed.
Fix: `pip install --break-system-packages <lib>` (PEP 668 blocks plain pip on Debian-managed Python).
Then re-run `python3 ml_engine.py --analyze` to confirm importance > 0.

---

## New yfinance ticker = 1 row until backfilled

Symptom: feature shows 0.000 importance in `--analyze` output right after adding new data source.
Fix: `python3 data_fetcher.py --backfill` after adding any new ticker.

---

## FEATURE_COLS duplicate silently inflates importance

Symptom: one feature's importance looks doubled; model wastes capacity on redundant column.
Fix after every FEATURE_COLS edit: `python3 -c "from ml_engine import FEATURE_COLS; print(len(FEATURE_COLS), len(set(FEATURE_COLS)))"`
Both numbers must match.

---

## GARCH conditional vol = redundant

`garch_vol` / `garch_vol_z` both 0.000 importance — fully covered by `hv20` + `vix_level`.
Also adds 30s+ fit time. Do not re-add.

---

## Hurst exponent = too slow-moving

`hurst_exp` (63-day rolling) = 0.000 importance with shift(1) gate. Not useful for DTE≤7 IC trades.

---

## New yfinance ticker CSV missing `options_iv_skew.csv`

Symptom: skew features (`call_skew`, `put_skew`, `skew_spread`) all 0.
Fix: `python3 data_fetcher.py --fetch-options`

---

## ORB data empty before Aug 2021

Expected — Dhan intraday API has no data before mid-2021. `orb_range_pct` = 0 for 2019–2021 rows is correct.

---

## NF expiry is Tuesday (since Sep 1 2025)

NSE changed NF expiry Thu → Tue from Sep 1 2025. Never infer expiry from code fallback calc.
Use Dhan expirylist API. Pre-Sep-2025 backtest DOW stats reflect old Thursday-expiry world — don't use them to judge current regime.

---

## Bull Put margin from Dhan API ≈₹220K/lot (not ₹55K)

Dhan `/v2/margincalculator/multi` returns unhedged-equivalent SPAN (~₹220K/lot) for Bull Put spread.
At capital < ₹220K: Bull Put skipped, IC fallback auto-triggered via Telegram alert.
`BULL_PUT_MARGIN_PER_LOT = 55_000` in auto_trader.py is the error-path fallback only.

---

## IC lot sizing: use actual Dhan SPAN, not formula

`_fetch_ic_margin_per_lot()` queries Dhan `/v2/margincalculator/multi` with all 4 legs.
`lots = floor(capital / actual_margin)`. The `risk_per_lot` formula gives wrong answer (5 lots instead of 1).

---

## IC has NO take-profit — exits at EOD only

`CREDIT_TP_FRAC = 0.65` applies to Bull Put and Bear Call spreads only.
IC: SL-only via spread_monitor.py, then EOD 3:15 PM via exit_positions.py.
Removing TP from IC added ₹21L over 5yr in backtest.

---

## Place BUY legs before SELL legs (Dhan hedge margin rule)

Long wing must be on-books before short leg gets margin benefit.
IC order: BUY CE long → SELL CE short → BUY PE long → SELL PE short.
Spread order: BUY long → SELL short.
Reversing triggers full unhedged margin.

---

## Bear Call routing reconciliation

- Old backtest (−₹24L verdict): Bear Call ran on **CALL signal days** (direction conflict — model said up, short CE got hit).
- New backtest_spreads.py: Bear Call runs on **PUT signal days** (aligned). Shows 61.9% WR, ₹35.9L.
- But Bull Put on same PUT days = 65.7% WR, ₹46.8L → Bull Put dominates.
- Verdict: **Bear Call stays out of live system.** Reasoning updated but conclusion unchanged.
