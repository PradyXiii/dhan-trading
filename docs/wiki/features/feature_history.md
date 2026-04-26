# Feature History — ML Feature Experiments

Auto-populated by autoloop_nf.py after each experiment.
Also see: `data/experiment_history.json` for full machine-readable log.
**Last updated:** 2026-04-26

---

## Current feature set (64 features as of April 2026)

Groups: rule signals, continuous signals, technical, global markets, macro,
VIX regime, NF momentum/drawdown, calendar, options sentiment, IV skew,
OI surface, ORB, breadth/flow, opening signal, interaction terms.

Full list: `python3 -c "from ml_engine import FEATURE_COLS; print(FEATURE_COLS)"`

---

## Kept features log

*(populated by autoloop_nf.py — each KEPT experiment appended here)*

| Date | Score delta | Features added | Rationale |
|---|---|---|---|
| 2026-04-24 | 0.5764 → 0.5804 (+0.0040) | `vix_roc_3d`, `vix_trend_consistency` | VIX ROC captures fear acceleration/deceleration critical for IC loss prediction. Trend consistency (count of same-direction days in last 5) — choppy markets (2-3/5) better for IC than clean trends (5/5). |
| 2026-04-24 | 0.5804 → 0.5890 (+0.0086) | `ema_separation_pct`, `iv_rank_20d` | EMA separation (EMA20 vs EMA50 distance as %) captures trend maturity — wide = established trend (directional), narrow = consolidation (IC). IV rank shows where current IV sits in 20-day range — sell premium when IV rank is high. |
| 2026-04-24 | 0.5890 → 0.5968 (+0.0078) | `nf_vol_trend_ratio`, `vix_ema_separation_interaction` | nf_vol_trend_ratio = realized vol / abs(trend magnitude); low ratio = clean trend ideal for directional, high ratio = choppy ideal for IC. vix_ema_separation_interaction = VIX regime × trend maturity. Domain insight: strong trend + low vol = best IC entry, weak trend + high vol = dangerous. |
| 2026-04-25 | 0.5643 → 0.7071 (+0.1428) | `nf_kalman_trend`, `hmm_bull_prob`, `hmm_neutral_prob`, `hmm_bear_prob` | Kalman filter on daily NF returns — noise-filtered signal now #1 feature (0.0957 importance, beats put_call_skew 0.0556). 3-state Gaussian HMM on [nf_ret1, vix_pct_chg] — states sorted by mean return → consistent bear/neutral/bull labeling. All use shift(1). Composite 0.5643 → 0.7071 after Optuna HPO. IC 5yr P&L: ₹1.17Cr → ₹1.38Cr (+18%). Requires: hmmlearn, pykalman (pip install --break-system-packages on Debian). |
| 2026-04-25 | baseline | `garch_vol`, `garch_vol_z` | GARCH(1,1) 1-day volatility forecast. arch lib lazy-imported. garch_vol_z = 60d z-score of garch_vol. Requires: arch lib. |

---

## Discarded features log

*(populated by autoloop_nf.py — each DISCARDED experiment appended here)*

| Date | Score delta | Features attempted | Reason discarded |
|---|---|---|---|
| 2026-04-24 | 0.5890 → 0.5771 (-0.0119) | `macro_alignment_score`, `sp_vix_diverge` | macro_alignment = count of agreeing macro signals. sp_vix_diverge = S&P up but VIX also up flag. Both hurt model despite strong domain rationale — cross-asset signals likely too noisy or already captured by existing features. |
| 2026-04-24 | 0.5890 → 0.5705 (-0.0185) | `pcr_momentum_3d`, `vix_dte_interaction` | PCR momentum = 3-day change in PCR. VIX-DTE interaction = high VIX + low DTE danger flag. Largest single-session drop seen (-0.0185). Despite strong domain logic, these features hurt generalization. |
| 2026-04-25 | 0.5968 → 0.5968 (0.0000) | Hurst exponent | 0.000 importance — too slow with shift(1). Discarded. |

---

## Reserved / watch list

*(features not yet tried but flagged for future experiments)*

| Feature | Domain rationale | Notes |
|---|---|---|
| garch_vol, garch_vol_z | 1-day realized vol forecast from GARCH(1,1) | Added 2026-04-25; pending importance confirmation in next kept run |

---

## Gotchas

- **Lazy-imported libs show 0.000 importance** if not installed. hmmlearn, pykalman, arch blocked by PEP 668 on Debian VM. Fix: `pip install --break-system-packages <lib>`.
- **NF close column is `d["nf_close"]`**, not `d["close"]`. Using `d["close"]` crashes autoexperiment with composite=0.0. Always use `d["nf_close"]`.

---

## Related pages
- [[bugs/known_issues]] — feature bugs and column name gotchas
- [[strategy/ic_research]] — how feature improvements affect IC P&L
