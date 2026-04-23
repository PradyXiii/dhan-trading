# Feature History — ML Feature Experiments

Auto-populated by autoloop_nf.py after each experiment.
Also see: `data/experiment_history.json` for full machine-readable log.
**Last updated:** 2026-04-24

---

## Current feature set (60 features as of April 2026)

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

---

## Discarded features log

*(populated by autoloop_nf.py — each DISCARDED experiment appended here)*

| Date | Score delta | Features attempted | Reason discarded |
|---|---|---|---|
| 2026-04-24 | 0.5890 → 0.5771 (-0.0119) | `macro_alignment_score`, `sp_vix_diverge` | macro_alignment = count of agreeing macro signals. sp_vix_diverge = S&P up but VIX also up flag. Both hurt model despite strong domain rationale — cross-asset signals likely too noisy or already captured by existing features. |
| 2026-04-24 | 0.5890 → 0.5705 (-0.0185) | `pcr_momentum_3d`, `vix_dte_interaction` | PCR momentum = 3-day change in PCR. VIX-DTE interaction = high VIX + low DTE danger flag. Largest single-session drop seen (-0.0185). Despite strong domain logic, these features hurt generalization — possibly overfitting to specific panic day patterns or multicollinearity with existing VIX/DTE features. |

---

## Reserved variable names — NEVER use as loop vars

| Name | What it holds | Where defined |
|---|---|---|
| `_c` | NF close price series (shifted) | compute_features() ~line 339 |
| `_c_nf` | NF close (alternate reference) | compute_features() |
| `_vix` | India VIX series | compute_features() |
| `_sp` | S&P500 series | compute_features() |
| `_nk` | Nikkei series | compute_features() |

---

## Related pages
- [[strategy/ic_research]] — strategy context for IC and Bull Put
- [[bugs/known_issues]] — ML feature bugs (reserved names, duplicate cols, etc.)