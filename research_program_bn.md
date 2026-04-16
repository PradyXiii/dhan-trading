# BankNifty Autoresearch Program

## Goal

Maximise composite score on the 252-day temporal holdout.

Formula (matches model_evolver.py exactly):
  composite = 0.50 × accuracy + 0.25 × recall_CALL + 0.25 × recall_PUT

Evaluated by: `python3 autoexperiment_bn.py`
Output: `{"composite": 0.734, "pnl_proxy": 0.68, "n_val": 252, "n_train": 1423}`

A change is KEPT only if:
  - composite >= previous best, AND
  - pnl_proxy >= baseline_pnl × 0.90  (guard against direction bias collapse)

---

## What you MAY change

### ml_engine.py — FEATURE_COLS and compute_features()

Add or remove feature names from `FEATURE_COLS`.

Add new derived columns in `compute_features()` using these raw columns
(available after `load_all_data()`):

```
bn_open, bn_high, bn_low, bn_close     — BankNifty OHLCV
nf_close                                — Nifty 50 close
vix_close, vix_open (sometimes)         — India VIX
sp_close                                — S&P 500 close
nk_close                                — Nikkei 225 close
spf_open, spf_close                     — S&P 500 futures OHLCV
crude_close (sometimes, may be NaN)     — Crude oil futures (CL=F)
dxy_close (sometimes, may be NaN)       — US Dollar Index
us10y_close (sometimes, may be NaN)     — US 10-Year Treasury yield
usdinr_close (sometimes, may be NaN)    — USD/INR exchange rate
fii_net_cash (sometimes, may be NaN)    — FII net cash market flow
pcr (sometimes, may be NaN)             — Put-Call ratio from option chain
```

Already computed in compute_features() — just add to FEATURE_COLS to activate:

```
ema20, rsi14, trend5, vix_dir, sp500_chg, nikkei_chg, spf_gap, bn_nf_div
hv20, bn_gap
s_ema20, s_trend5, s_vix, s_bn_nf_div   (discrete ±1 rule signals)
rule_score, rule_signal
ema20_pct, vix_level, vix_pct_chg, vix_hv_ratio
bn_ret1, bn_ret20, dow, dte
vix_open_chg, pcr_ma5, pcr_chg, fii_net_cash_z
crude_ret, dxy_ret, us10y_chg, usdinr_ret
bn_dist_high20                          (% below 20-day rolling high; negative = in correction)
```

### signal_engine.py — score_row()

Change thresholds for existing active signals or activate inactive ones.

ACTIVE indicators (4, summed to compute CALL/PUT/NONE score):
```
s_ema20     — 1 if bn_close > ema20,  else -1
s_trend5    — 1 if trend5 > +1.0,     -1 if < -1.0,  else 0
s_vix       — 1 if vix_dir < 0,       -1 if > 0,     else 0
s_bn_nf_div — 1 if bn_nf_div > +0.5,  -1 if < -0.5,  else 0
```

INACTIVE (computed but not added to `total` — can promote to active):
```
s_rsi14    — 1 if rsi14 > 55,    -1 if < 45,   else 0
s_sp500    — 1 if sp500_chg > 0, -1 if <= 0
s_nikkei   — 1 if nikkei_chg > 0, -1 if <= 0
s_spf_gap  — 1 if spf_gap > 0.2, -1 if < -0.2, else 0
s_hv20     — 1 if hv20 < 12,     -1 if > 20,   else 0
s_bn_gap   — 1 if bn_gap > 0.3,  -1 if < -0.3, else 0
```

---

## What you MUST NOT change

- Any Dhan API call, URL, endpoint, request payload, or response parsing
- Files: auto_trader.py, model_evolver.py, exit_positions.py, backtest_engine.py,
  data_fetcher.py, trade_journal.py, notify.py, renew_token.py, health_ping.py,
  midday_conviction.py, autoexperiment_bn.py, autoloop_bn.py
- Constants: SL_PCT, RR, LIVE_TRADING_ENABLED, ML_THRESHOLD, SCORE_THRESHOLD
- Data source tickers (^INDIAVIX, ^GSPC, ES=F, etc.) or CSV filenames

---

## Experiment discipline

- ONE change per experiment. Keep it small and targeted.
- No refactors, renames, or formatting changes — they waste experiments.
- If adding a new feature to FEATURE_COLS: also add its computation to compute_features().
- If removing a feature from FEATURE_COLS: just remove the name from the list
  (leave the computation in compute_features — it does no harm).
- Changing a signal threshold in score_row() also shifts what the model learns from
  the s_* columns — consider this downstream effect.

---

## Response format (CRITICAL)

Return ONLY a valid JSON object. No markdown fences. No explanation before or after.
The `old_code` field must be a unique substring of the file (include 2–3 lines of
context so it cannot match two places).

```
{
  "file": "ml_engine.py",
  "description": "Short one-line description of the change",
  "old_code": "exact unique substring to find in the file",
  "new_code": "replacement string"
}
```
