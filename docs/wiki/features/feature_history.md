# Feature History — ML Feature Experiments

Auto-populated by autoloop_nf.py after each experiment.  
Also see: `data/experiment_history.json` for full machine-readable log.  
**Last updated:** 2026-04-23 (initial creation)

---

## Current feature set (60 features as of April 2026)

Groups: rule signals, continuous signals, technical, global markets, macro,
VIX regime, NF momentum/drawdown, calendar, options sentiment, IV skew,
OI surface, ORB, breadth/flow, opening signal, interaction terms.

Full list: `python3 -c "from ml_engine import FEATURE_COLS; print(FEATURE_COLS)"`

---

## Kept features log

*(populated by autoloop_nf.py — each KEPT experiment appended here)*

---

## Discarded features log

*(populated by autoloop_nf.py — each DISCARDED experiment appended here)*

---

## Reserved variable names — NEVER use as loop vars

| Name | What it holds | Where defined |
|---|---|---|
| `_c` | NF close price series (shifted) | compute_features() ~line 339 |
| `_c_nf` | NF close (alternate reference) | compute_features() |
| `_vix` | India VIX series | compute_features() |
| `_sp` | S&P500 series | compute_features() |
| `_nk` | Nikkei series | compute_features() |

If used as loop variable → `could not convert string to float` error downstream.

---

## Feature addition checklist

1. Insert computation at AUTOLOOP APPEND ZONE anchor
2. Add name to FEATURE_COLS (check for duplicates)
3. All price series: `.shift(1)` before any rolling/ewm
4. `python3 ml_engine.py --analyze` → importance must be > 0
5. `python3 autoexperiment_nf.py` → composite must be > baseline
6. If new CSV needed: `python3 data_fetcher.py --backfill`

---

## Related pages
- [[strategy/ic_research]] — what the model is optimizing
- [[bugs/known_issues]] — bugs to avoid when adding features
