# BankNifty Autoresearch Program

## ⚠️ REAL-OPTIONS RULE (April 2026)

Composite score measures **directional signal quality** — not real option P&L.
OHLCV-formula premium backtest is misleading: April 2026 test showed OHLCV
predicting ₹25M profit vs real 1-min options showing -₹1.22L on the same
period. Theta decay + IV compression + slippage are invisible to OHLCV.

**Before any feature / config change gets promoted to live:**
1. Composite ≥ previous best (this file's existing gate)
2. AND `python3 backtest_engine.py --real-options --ml` result is non-worse
   than the current live config's real-options result

The autoresearcher may propose changes using autoexperiment composite, but
the 3-night paper-model promotion streak in `autoloop_bn.py` must also pass
real-options validation before auto-promotion. Rule of thumb: composite is a
cheap filter, real-options is the judge.

See "REAL-OPTIONS RULE" in CLAUDE.md for the full discovery story and
workflow commands.

---

## Goal

Maximise composite score on the 252-day temporal holdout.

Formula (matches model_evolver.py exactly):
  composite = 0.50 × accuracy + 0.25 × recall_CALL + 0.25 × recall_PUT

Evaluated by: `python3 autoexperiment_bn.py`
Current honest baseline: ~0.515 (no leakage, 1511 training rows, 7 years of data)
Target: push composite toward 0.60+ through genuine feature signal

A change is KEPT only if:
  - composite >= previous best, AND
  - pnl_proxy >= baseline_pnl × 0.90  (guard against direction bias collapse), AND
  - real-options backtest P&L (once 5-yr cache is complete) is non-worse than baseline

---

## Context — what the label is

The label is NOT simply "did BN close higher or lower today."
It is: **which side (CALL or PUT) hits its take-profit before stop-loss?**

  - CALL wins if BN moves UP by TP_pts before DOWN by SL_pts from open
  - PUT wins if BN moves DOWN by TP_pts before UP by SL_pts from open
  - TP_pts is derived from real ATM call/put premiums (options_atm_daily.csv)

This means the most predictive features are those that predict:
  1. **Which direction** the day will trend intraday
  2. **How wide** the intraday range will be (wide range = one side will definitely win)

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
call_premium (sometimes, may be NaN)    — Real ATM call open price (9:30 AM)
put_premium  (sometimes, may be NaN)    — Real ATM put open price (9:30 AM)
```

Already computed in compute_features() — just add to FEATURE_COLS to activate:

```
ema20, rsi14, trend5, vix_dir, sp500_chg, nikkei_chg, spf_gap, bn_nf_div
hv20, bn_gap
s_ema20, s_trend5, s_vix, s_bn_nf_div   (discrete ±1 rule signals)
rule_score, rule_signal
rule_score_lag1                          (yesterday's rule score — conviction momentum)
adx14                                    (Average Directional Index — trending vs ranging)
ema20_pct, vix_level, vix_pct_chg, vix_hv_ratio
bn_ret1, bn_ret20, bn_ret60, dow, dte
vix_open_chg, pcr_ma5, pcr_chg, fii_net_cash_z
crude_ret, dxy_ret, us10y_chg, usdinr_ret
bn_dist_high20, bn_dist_high52          (% below rolling high)
prev_range_pct, prev_body_pct           (prev-day candle structure)
put_call_skew, iv_proxy                 (real options market signals)
straddle_expansion                       (today's ATM straddle vs 20d mean — IV expansion)
call_skew, put_skew, skew_spread, skew_chg  (IV skew dynamics — requires options_iv_skew.csv)
skew_trend_interact, skew_vix_regime        (skew interactions with trend and vol regime)
oi_pcr_wide, oi_imbalance_atm               (OI surface — requires options_oi_surface.csv)
call_wall_offset, put_wall_offset           (max OI strike position vs ATM)
max_pain_dist_prev, gex_flag_prev           (max pain distance + dealer gamma regime)
vix_pct_rank_252                            (VIX 252-day percentile — regime-normalized)
bn_nifty_rs, bn_nifty_rs_slope5             (BN/Nifty leadership ratio + momentum)
bankbees_ret1, bankbees_vol_z               (bank ETF flow + unusual volume — yfinance)
bank_breadth_d1, bank_breadth_z             (top-5 BN constituent breadth + regime)
orb_range_pct, orb_break_side               (prior-day 9:15 opening-range breakout)
```

**Note: GIFT Nifty pre-open not implemented** — no stable public API. Would need
investing.com scraping or paid feed. S&P 500 futures gap (`spf_gap`) serves as
the closest overnight proxy for now.

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

### auto_trader.py — trading constants only

You may change ONLY these two constants:

```python
SL_PCT = 0.15   # stop-loss percentage on premium (e.g. try 0.12 or 0.18)
RR     = 2.5    # reward:risk — TP = SL × RR (e.g. try 2.0 or 3.0)
```

**Evaluation**: `python3 autoexperiment_backtest.py`
Output: `{"composite": 0.534, "pnl_proxy": 0.534, "n_val": 430, ...}`

Composite = 0.70 × win_rate + 0.30 × drawdown_score (full backtest history, ML signals).
A change is KEPT only if composite >= baseline_bt AND pnl_proxy >= baseline_bt_pnl × 0.90.

Constraints:
- SL_PCT must stay in range [0.08, 0.25]
- RR must stay in range [1.5, 4.0]
- Do NOT touch LOT_SIZE, RISK_PCT, MAX_LOTS, PREMIUM_K, ITM_WALK_MAX, or any API-related code

---

## What you MUST NOT change

- Any Dhan API call, URL, endpoint, request payload, or response parsing
- Files: model_evolver.py, exit_positions.py, backtest_engine.py,
  data_fetcher.py, trade_journal.py, notify.py, renew_token.py, health_ping.py,
  midday_conviction.py, autoexperiment_bn.py, autoexperiment_backtest.py, autoloop_bn.py
- Constants in auto_trader.py: LOT_SIZE, RISK_PCT, MAX_LOTS, PREMIUM_K, ITM_WALK_MAX,
  ML_CONF_THRESHOLD, ENTRY_SPOT_GAP_THRESHOLD, ENTRY_WAIT_MAX_MINS
- Data source tickers (^INDIAVIX, ^GSPC, ES=F, etc.) or CSV filenames

---

## High-priority research directions

These have the strongest theoretical basis for predicting intraday BN direction:

1. **IV skew dynamics**: `call_skew` / `put_skew` measure how much more expensive OTM
   options are vs ATM. `skew_spread` = put_skew − call_skew captures net downside fear —
   elevated put skew on a CALL day = market pricing in crash risk, historically associated
   with reversals. `skew_chg` captures momentum: a sudden put skew expansion is a bearish
   leading signal even if spot hasn't moved yet.
   Try: `skew_spread * s_ema20` (skew aligned with trend = stronger signal),
        `skew_chg * vix_dir` (skew expanding + VIX rising = high-conviction PUT),
        `skew_spread * pcr` (skew and flow agree → higher accuracy).
   NOTE: populate `data/options_iv_skew.csv` first: `python3 data_fetcher.py --fetch-options`

2. **Volatility regime interactions**: When iv_proxy is high (IV elevated), intraday
   range is wide — both SL and TP are more likely to be hit. On these days, direction
   signals (s_ema20, spf_gap, vix_dir) are more decisive. Try: `iv_proxy * s_ema20`,
   `iv_proxy * spf_gap`, `iv_proxy * vix_dir`.

2. **Prev-day candle conviction + range**: `prev_body_pct` near ±1 means yesterday
   was a strong trending candle → today may continue OR mean-revert. Combined with
   `prev_range_pct` (was yesterday wide or narrow?), this paints the volatility regime.
   Try: `prev_body_pct * bn_ret5` (does yesterday's conviction align with short momentum?),
   `prev_range_pct * iv_proxy` (do options and price agree on range expansion?).

3. **Options market direction signal**: `put_call_skew > 1` = puts cost more = market
   fears downside = potential PUT signal OR mean-reversion CALL signal (contrarian).
   Try using `put_call_skew` in combination with trend features to identify which regime
   applies (trending market = follow skew; ranging market = fade skew).

4. **Gap + momentum alignment**: `bn_gap * bn_ret5` or `bn_gap * s_ema20` — if today's
   opening gap aligns with recent short-term momentum, continuation is more likely.

5. **52-week high regime**: `bn_dist_high52` near 0 = BN at all-time highs = bull trend
   intact. Far negative = deep correction. This regime determines whether CALL or PUT
   signals are more reliable. Try it as a standalone feature or in interaction with
   s_ema20.

6. **Medium-term trend**: `bn_ret60` (3-month return) separates sustained bull phases
   from bear phases better than `bn_ret20` (4-week, too noisy) or `bn_ret1` (1-day,
   too short). On positive `bn_ret60` days, CALL accuracy historically higher.

---

## Leakage warning — CRITICAL

The leakage guard rejects any feature with |correlation| > 0.85 with the label,
AND any holdout composite > 0.90.

**Never use same-day values of bn_close, bn_high, bn_low, vix_close, sp_close,
nk_close in any calculation**. The label is determined by today's close — using
today's close in a feature is circular and will be caught.

Always shift by 1: `_c = d["bn_close"].shift(1)` etc. The current code already does
this correctly for all base series. If you add new features, use `d["bn_high"].shift(1)`,
`d["bn_low"].shift(1)`, `d["bn_open"].shift(1)` etc., NOT `d["bn_high"]` directly.

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

`"file"` must be one of: `"ml_engine.py"`, `"signal_engine.py"`, `"auto_trader.py"`.
