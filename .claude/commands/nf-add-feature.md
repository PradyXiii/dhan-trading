# NF Add Feature — Step-by-Step Checklist

Run this checklist every time a new ML feature is added to the NF system.

## Step 1 — Read CLAUDE.md rules first

Before writing any code, re-read:
- "ML FEATURE RULE" section in CLAUDE.md
- "Known Gotchas" table in CLAUDE.md
- Or invoke `/nf-gotchas` for the full gotcha library

## Step 2 — Add computation to compute_features()

File: `ml_engine.py`, inside `compute_features()`, in the AUTOLOOP APPEND ZONE.

Rules:
- Use `.shift(1)` on ALL price/return inputs (yesterday's values only — today = lookahead)
- Never use `_c`, `_c_nf`, `_vix`, `_sp`, `_nk` as loop variable names
- NF daily close = `d["nf_close"]` (not `d["close"]`)
- Wrap with `pd.to_numeric(..., errors="coerce").ffill().bfill()` to handle gap days
- If new library needed: add lazy import block with `_LIB_OK` guard at top of file

## Step 3 — Add name to FEATURE_COLS

One name only. Then immediately verify no duplicate:
```bash
python3 -c "from ml_engine import FEATURE_COLS; print(len(FEATURE_COLS), len(set(FEATURE_COLS)))"
```
Both numbers must match.

## Step 4 — Install any new library

```bash
pip install --break-system-packages <lib>
```
(Plain `pip` blocked on Debian-managed Python — PEP 668.)

## Step 5 — Backfill if new CSV needed

```bash
python3 data_fetcher.py --backfill
```
New yfinance tickers start with 1 row → 0.000 importance until backfilled.

## Step 6 — Verify importance > 0

```bash
python3 ml_engine.py --analyze
```
Feature must appear with importance > 0.000. If zero: check lazy import flag, check column name, check shift(1) applied.

## Step 7 — Run autoexperiment gate

```bash
python3 autoexperiment_nf.py
```
**Keep only if composite >= 0.6484** (Apr 2026 NF baseline after Kalman + HMM).
If composite drops: revert the feature, add entry to "Discarded features log" in `docs/wiki/features/feature_history.md`.

## Step 8 — Update wiki

Add entry to `docs/wiki/features/feature_history.md`:
- "Kept features log" if passed gate
- "Discarded features log" if dropped
- Update feature count in the "Current feature set" header

## Step 9 — Commit and push

```bash
git add ml_engine.py docs/wiki/features/feature_history.md
git commit -m "feat: add <feature_name> — composite <old> → <new>"
git push -u origin nifty-strategies
```

## Quick reference — composite baselines

| Date | Baseline | Gate |
|---|---|---|
| Pre-session Apr 25 2026 | 0.5643 | 0.5521 |
| Apr 25 2026 (after Kalman+HMM) | 0.6484 | **0.6484** (current) |
