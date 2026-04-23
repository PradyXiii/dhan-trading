# NF IC ML Research Program

**System:** Nifty50 Iron Condor + Bull Put hybrid auto-trader  
**Live since:** 22 April 2026  
**Optimization target:** `composite score > current baseline` on 252-day holdout  
**Current baseline:** ≥ 0.5358 (post BN→NF migration, April 2026)  
**Promotion gate:** paper beats live by ≥1.5% for 3 consecutive nights  

---

## What you are optimizing

The composite score is a blend of:
- Directional accuracy (did the model call CALL/PUT correctly?)
- Precision on CALL signals (IC days — false positives are costly)
- Recall on PUT signals (Bull Put days — missing a PUT day = IC loss)

A 1-point gain in composite = roughly 1–2 extra winning trades per quarter.

---

## Core philosophy (from Karpathy autoresearch)

1. **Never pause** — propose, run, measure, keep or discard, repeat
2. **Simpler is better** — if two ideas tie in composite, prefer the one with fewer lines of code. Prefer deleting a feature that adds noise over adding a complex new one.
3. **No lookahead, ever** — every price feature must use `.shift(1)`. Today's close is unknown at 9:30 AM.
4. **Strict improvement** — new composite must be strictly > old. Equal = discard.

---

## What the model already has (60 features — DO NOT duplicate)

### Group 1: Rule signals (discrete ±1 outputs)
`s_ema20`, `s_trend5`, `s_vix`, `s_nf_gap` — rule-based CALL/PUT votes

### Group 2: Continuous signals
`ema20_pct`, `trend5`, `vix_dir` — magnitudes behind the rules

### Group 3: Technical momentum
`rsi14`, `hv20`, `nf_gap`, `adx14` — RSI, realized vol, overnight gap, trend strength

### Group 4: Global markets
`sp500_chg`, `nikkei_chg`, `spf_gap` — previous-day S&P, Nikkei, S&P futures gap

### Group 5: Macro
`crude_ret`, `dxy_ret`, `us10y_chg`, `usdinr_ret` — inflation/dollar/yield/rupee

### Group 6: VIX regime
`vix_level`, `vix_pct_chg`, `vix_hv_ratio`, `vix_pct_rank_252`

### Group 7: NF momentum and drawdown
`nf_ret1`, `nf_ret20`, `nf_dist_high20`, `nf_dist_high52`

### Group 8: Calendar
`dow`, `dte` — day of week, days to expiry

### Group 9: Options sentiment
`pcr_ma5`, `put_call_skew`, `iv_proxy`

### Group 10: IV skew
`call_skew`, `put_skew`, `skew_spread`, `skew_chg`

### Group 11: OI surface
`oi_pcr_wide`, `oi_imbalance_atm`, `call_wall_offset`, `put_wall_offset`

### Group 12: Opening range (ORB)
`orb_range_pct`, `orb_break_side` — 9:15–9:30 AM range + breakout side

### Group 13: Breadth and flow
`fii_net_cash_z`, `bankbees_ret1`, `bank_breadth_d1`

### Group 14: Opening signal
`vix_open_chg` — VIX gap at 9:15 AM

### Group 15: Interaction terms (autoresearch-added)
`gap_mom_align`, `iv_spf_interaction`, `adx_gap_interact` + any others already in history

---

## Priority experiment areas — ranked by expected lift

### P1: Feature removal / noise reduction (highest impact, often positive)
- Remove features with importance < 0.5% that have been in FEATURE_COLS for > 30 experiments
- Remove interaction terms that were added but show diminishing importance
- "Simpler is better" — a 55-feature model sometimes beats a 60-feature model

### P2: VIX regime transitions (not yet captured)
- `vix_roc3` — 3-day rate of change of VIX (is fear accelerating or decelerating?)
- `vix_rolling_zscore` — VIX z-score over 20-day window (where in fear cycle?)
- `vix_trend_5d` — sign of 5-day VIX slope (rising = bad for CALL days)
- `vix_regime_binary` — 1 if VIX in top quartile (high-fear regime = different model behavior)

### P3: Cross-asset momentum alignment (consensus check)
- `macro_alignment_score` — count of how many macro signals agree (crude, DXY, S&P, VIX all pointing same direction = high conviction)
- `sp_vix_diverge` — S&P up but VIX also up = divergence flag (unusual, often precedes reversal)
- `global_risk_score` — simple composite: (+1 if sp500>0) + (+1 if vix<0) + (+1 if dxy<0) + (+1 if crude>0 for PUT) ranges -4 to +4

### P4: Trend quality (not just direction)
- `nf_trend_consistency` — rolling count of same-direction days in last 5 (e.g. 4/5 up days = strong trend)
- `ema_separation` — distance between EMA20 and EMA50 as % (trend maturity)
- `nf_vol_trend_ratio` — realized vol divided by trend: strong trend + low vol = best IC entry

### P5: Options flow quality
- `iv_rank_20` — where current ATM IV sits in its 20-day range (0=low, 1=high) — sell premium when IV rank high
- `pcr_momentum` — 3-day change in PCR (rising PCR = put buying = fear building)
- `oi_build_ratio` — call OI change vs put OI change at ATM (market makers positioning)

### P6: Calendar effects (weekly cycle)
- `days_since_expiry` — inverse of DTE: how many days SINCE last Tuesday expiry (0=expiry day, 6=following Monday)
- `week_of_month` — 1–4: first expiry week, second, third, fourth (different theta profiles)
- `expiry_week_flag` — binary: 1 if this Tuesday is month's last expiry (monthly option expiry, different dynamics)

### P7: Regime-conditional interaction terms
- `vix_dte_interact` — VIX × DTE: high VIX + low DTE = dangerous for IC shorts
- `trend_pcr_align` — trend signal × PCR direction (both pointing same way = higher conviction)
- `gap_vix_interact` — gap size × VIX level: large gap on high-VIX day means different thing than large gap on low-VIX day

### P8: Bank constituent signals (existing data available)
- `bank_dispersion` — standard deviation of HDFC/ICICI/Kotak/SBI/Axis returns on prior day (high = sector rotation, bad for IC)
- `bank_volume_ratio` — bank ETF volume vs 20-day average (volume surge = directional move coming)
- `bank_return_zscore` — bankbees return z-score over 20 days (extreme bank move = vol spike likely)

---

## What NOT to try

- Any feature using `nf_close`, `nf_high`, or `nf_low` for TODAY's row without `.shift(1)` — **leakage**
- `rsi14` variants — already have RSI; adding RSI(7) or RSI(21) rarely helps
- Very long windows (>100 days rolling) — NaN chains drop too many training rows
- Complex deep features with >10 lines of computation — fragile, usually overfit
- Any feature that perfectly predicts one direction (|corr| > 0.85) — auto-rejected by pipeline
- Re-trying experiments already in the lifetime history (see history above)
- Features requiring NEW data files not already in `data/` — unless data_fetcher.py already fetches them

---

## Experiment structure checklist

Before proposing any experiment:
1. Check lifetime history — is this exact idea already tried?
2. Check FEATURE_COLS — will adding this duplicate an existing feature?
3. Verify data column exists — run `_build_column_inventory()` in context
4. Ensure `.shift(1)` on all price/return series
5. Keep it simple — if the idea needs >8 lines of code, it probably won't improve composite

---

## Domain knowledge — why the NF IC signal matters

**IC on CALL days:** Market-neutral theta strategy. Wins even when CALL signal is wrong (market sideways). The ML model's job is NOT to predict direction — it's to predict when the market will be VIOLENT (big move = IC loss). Features that detect high-volatility days are most valuable.

**Bull Put on PUT days:** Directional credit spread. Model has CALL bias so on PUT signal days, market often goes up anyway → Bull Put profits. ML job here: detect the rare PUT days when market actually collapses → flag those as SKIP.

**What causes IC losses:**
- VIX spike intraday (fear surge → premiums expand → short legs hit SL)
- Gap + trend continuation (market gaps up AND keeps running → CE short breached)
- Global panic (S&P futures collapse overnight → NF opens far from ATM)
- Expiry-day oscillation is LOWER risk (DTE=0 = max theta)

**The best IC entry conditions:**
- VIX between 13-20 (not too low = IV collapse, not too high = gap risk)
- S&P futures stable (±0.3% overnight)
- DTE ≤ 3 (max theta)
- No gap > 1.5% expected
- PCR near neutral (0.9–1.1)


---

## Recently tried (auto-updated 2026-04-24, last 19 experiments)

### Kept (11 experiments improved composite):
  ✅ 2026-04-17: Remove noisy features (crude_ret, dxy_ret, us10y_chg, usdinr_ret, pcr, pcr_ma5, pcr_chg, fii_net_cash_z) that likely add noise for RandomForest with limited data
  ✅ 2026-04-18: Add gap-momentum alignment, IV-direction interaction, and 52w-high regime interaction features
  ✅ 2026-04-18: Add VIX-trend interaction, prev_body_momentum, and PCR/FII features with proper computation
  ✅ 2026-04-18: Add ADX14 computation before FEATURE_COLS reference and add adx_trend_interact feature combining ADX with trend direction
  ✅ 2026-04-23: Add mean-reversion signal: distance from 10-day VWAP proxy (volume-weighted), plus RSI-regime interaction (oversold/overbought × trend direction) and a momentum acceleration feature (ret5 - ret20 normalized)
  ✅ 2026-04-23: Add OI-weighted directional bias and straddle change rate features: oi_dir_bias combines ATM OI imbalance with put/call skew for a stronger directional signal; straddle_velocity measures rate of change of straddle premium (IV acceleration); plus an ADX-momentum interaction that amplifies momentum signals when trend is strong
  ✅ 2026-04-23: Add mean-reversion features: nf_ret5_zscore (z-scored 5-day return using 60d window) captures overextended short-term moves, and pcr_oi_combined (PCR from OI surface × straddle expansion) captures options market positioning. Also add vix_gap_regime (VIX percentile rank × gap direction) to weight gaps by volatility regime.
  ✅ 2026-04-23: Add USDINR z-scored level (currency stress indicator), crude oil momentum signal, and a DXY-VIX interaction feature. USDINR weakening signals risk-off for Indian equities; crude momentum impacts inflation expectations; DXY strength × VIX captures global risk-off episodes.
  ✅ 2026-04-24: Add VIX rate-of-change (3-day) and trend consistency (count of same-direction days in last 5) features. VIX ROC captures fear acceleration/deceleration which is critical for IC loss prediction. Trend consistency measures how clean the recent trend is - choppy markets (2-3/5) are better for IC than clean trends (5/5).
  ✅ 2026-04-24: Add EMA separation (EMA20 vs EMA50 distance as %) and IV rank 20-day features. EMA separation captures trend maturity - wide separation means established trend (good for directional plays), narrow means consolidation (good for IC). IV rank shows where current IV sits in its recent range - sell premium when IV rank is high.
  ✅ 2026-04-24: Add nf_vol_trend_ratio (realized vol divided by absolute trend magnitude - low ratio = clean trend ideal for directional, high ratio = choppy ideal for IC) and vix_ema_separation interaction (VIX regime × trend maturity). These capture the key domain insight: strong trend + low vol = best IC entry, while weak trend + high vol = dangerous.

### Discarded (8 experiments didn't help):
  ❌ 2026-04-18: Add gap-momentum alignment and IV-direction interaction features
  ❌ 2026-04-18: Add ADX-weighted trend signal and gap-ema alignment interaction features [CRASHED/FAILED]
  ❌ 2026-04-18: Add ADX14 computation (was missing, caused exp3 crash) and interaction features: gap_ema_align and iv_x_vix_dir
  ❌ 2026-04-18: Add rule_score_lag1 (yesterday's conviction momentum) and remove prev_body_pct and bn_ret60 which may add noise [CRASHED/FAILED]
  ❌ 2026-04-23: Add USDINR momentum and crude oil regime features as macro risk signals, plus a VIX-term-structure proxy (vix_open vs vix_close lagged) and a volume spike detector
  ❌ 2026-04-23: Add overnight_range feature (gap vs prev range ratio) and vix_mean_revert (VIX distance from 20d mean normalized) plus a combined vol_regime_signal (HV20 rank × VIX percentile interaction). These capture volatility regime persistence and mean-reversion dynamics.
  ❌ 2026-04-24: Add macro_alignment_score (count of agreeing macro signals) and sp_vix_diverge (S&P up but VIX also up divergence flag). macro_alignment captures cross-asset consensus which increases conviction. sp_vix_diverge flags unusual divergences that often precede reversals - critical for IC loss avoidance.
  ❌ 2026-04-24: Add PCR momentum (3-day change in PCR) and VIX-DTE interaction features. PCR momentum captures building fear/complacency trend - rising PCR means put buying accelerating which is critical for detecting emerging panic days that kill IC positions. VIX-DTE interaction captures the key domain insight that high VIX + low DTE is dangerous for IC shorts while low VIX + high DTE is ideal theta harvesting.
