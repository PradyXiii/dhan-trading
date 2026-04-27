# CLAUDE.md — Nifty50 Iron Condor Auto-Trader Architecture Map

Quick reference for Claude Code. Read this before touching any file.

## 🧠 TRADING WIKI — FULL KNOWLEDGE BASE

**`docs/wiki/index.md`** — master index of all compiled knowledge (Karpathy LLM Wiki pattern).

| Page | What it contains |
|---|---|
| `docs/wiki/strategy/ic_research.md` | 7yr backtest, IC+BullPut verdict, discarded strategies, DOW breakdown |
| `docs/wiki/features/feature_history.md` | All 64 features, kept/discarded log, reserved names |
| `docs/wiki/bugs/known_issues.md` | 15+ session bugs — ML shadows, API format, lot sizing |

Auto-populated by `autoloop_nf.py` after every experiment. Compiled by `wiki_compiler.py`.

```bash
python3 wiki_compiler.py           # compile raw discoveries → wiki articles
python3 wiki_compiler.py --dry-run # preview without API call
python3 wiki_compiler.py --lint    # check broken links + orphan pages
```

**`docs/wiki/raw/`** — gitignored (VM only). Drop discoveries here; wiki_compiler ingests them.

---

## 🗜️ RESPONSE STYLE — CAVEMAN MODE

**All responses in this project use `/caveman full` style** to save tokens.
Drop articles, filler, pleasantries. Keep all technical facts, code, file paths,
numbers. Invoke `/caveman-commit` for commit messages and `/caveman-review`
for PR reviews. Full mode = default; escalate to `/caveman ultra` on request.

---

## 🔍 CODE REVIEW TOOLS

Two review surfaces, different stages:

| Tool | When | Cost | Who triggers |
|---|---|---|---|
| `/review` | Quick local pass while iterating on a change | Normal session usage | Either — fast feedback |
| `/ultrareview` | Pre-merge on substantial PRs — multi-agent cloud review, independently verifies every finding | 3 free runs/account (one-time), then ~$5–$20/run as extra usage | **User only** — Claude cannot auto-invoke |

`/ultrareview` runs 5–10 min in a remote sandbox, returns verified bugs as notifications. Needs Claude.ai login (not API key). Use it before merging — not on every commit.

---

## ✅ LIVE MODE — MULTI-STRATEGY ACTIVE (April 2026)

**`auto_trader.py` is in PAPER_MODE = False.** Real orders placed via Dhan.

**Daily strategy routing (9:30 AM IST):**
- **CALL signal days** → Iron Condor (4 legs: SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE)
- **PUT signal days** → Bull Put Credit (2 legs: SELL ATM PE + BUY ATM-150 PE, 2 lots via Dhan margin API)
- **No skip days** — both strategies profitable all 5 weekdays in Tue-expiry regime
- **Auto-upgrade**: if balance ≥ ₹2.3L → Short Straddle replaces IC on CALL days

**Capital at go-live (22 Apr 2026): ₹1,12,370**
- IC: ₹93,202/lot → 1 lot ✅ (CALL days)
- Bull Put: ≈₹51K/lot → 2 lots ✅ (PUT days)
- Straddle: ₹2,30,000+/lot → 0 lots ❌ (upgrade pending capital growth)

**Why these strategies (7-year backtest, Apr 2026 research):**
- IC on CALL days: market-neutral theta. Wins even if signal is wrong (market sideways). Sep 2025+ regime: 97.5% WR.
- Bull Put on PUT days: 100% WR (51 trades, Sep 2025–Apr 2026). Signal model has CALL bias → market often goes UP on PUT signal days → Bull Put (profits when market doesn't fall) wins easily.
- Bear Call PERMANENTLY DUMPED: -₹24.03L over 7 years, 13.5% WR. Direction conflict: CALL signal = market going UP → short CE gets hit. Never again.
- Straddle (when affordable): ₹3.15Cr over 7yr, but needs ₹2.3L/lot margin. Auto-upgrades when capital reaches threshold.
- **Full research**: see `STRATEGY_RESEARCH.md` — final verdict locked, don't re-run unless regime changes.

**What happens each morning (9:30 AM IST):**
- Data → signal → ML → strategy routing → real MARKET orders via Dhan → Telegram alert
- `spread_monitor.py` watches every minute for SL; `exit_positions.py` squares off at 3:15 PM

**NF lot size: 75 before Jan 6 2026, 65 from Jan 6 2026**

**Go-live checklist (completed April 2026):**
1. ✅ Built `fetch_intraday_options.py --instrument NF --spreads` (NF multi-leg cache)
2. ✅ Built `backtest_spreads.py` with `--instrument NF` + hybrid backtest + cost model
3. ✅ Fetched full NF option cache (5590 files, Aug 2021–Apr 2026)
4. ✅ Confirmed NF IC+BullPut hybrid: 7yr research in `STRATEGY_RESEARCH.md` — final verdict locked
5. ✅ Wired strategy routing: CALL days → IC, PUT days → Bull Put (Dhan margin API lot sizing)
6. ✅ PAPER_MODE=False — live from 22 Apr 2026

---

## ⚠️ REAL-OPTIONS RULE — NEVER USE OHLCV FOR FINAL VALIDATION

**Ground truth for every backtest, experiment, or config change = real 1-min option data.**
OHLCV-formula premium (`PREMIUM_K × BN_open × sqrt(DTE)`) is a **quick sanity check only** — never the basis for promotion, deployment, or any decision that affects live capital.

**Why this rule exists (April 2026 discovery):**
OHLCV backtest showed ₹25M profit over years. Real 1-min option backtest on same period showed **-₹1.22L**. Gap came from 3 things OHLCV can't see: theta decay (premium bleeds every minute regardless of spot), IV compression (premium falls after open even if spot unchanged), real-world bid-ask slippage. Formula tracks spot only — real options don't.

**Mandatory workflow before promoting any change:**

1. Ensure cache is current:
   ```bash
   python3 fetch_intraday_options.py --start 2021-08-01   # Dhan has no data before Aug 2021
   ```
2. Re-run backtest with real option data:
   ```bash
   python3 backtest_engine.py --real-options --ml
   ```
3. If results contradict OHLCV backtest → **real-options wins, always**.
4. Autoloop / autoexperiment must use `--real-options` flag (or equivalent real-premium path) for every evaluation. OHLCV result may be logged for comparison but cannot gate promotion.

**Cache location:** `data/intraday_options_cache/{YYYY-MM-DD}_{CE|PE}.csv` (gitignored, VM-only).

**If cache is partial** (< 90% of trade days covered): annotate any report as "PARTIAL REAL-OPTIONS COVERAGE — results unreliable" and complete the fetch before acting.

---

## 🎯 CONFIRMED STRATEGY: Nifty50 Iron Condor (April 2026)

**NF IC is the live strategy. Backtest confirmed on 4.5 years of real 1-min option data.**

### NF IC Backtest Results (real 1-min data, 2021–2026)

| Year | Trades | WR | P&L |
|---|---|---|---|
| 2021 (Aug–Dec) | 100 | 88% | ₹9.6L |
| 2022 | 238 | 80% | ₹25.2L |
| 2023 | 235 | 91% | ₹24.1L |
| 2024 | 235 | 86% | ₹25.8L |
| 2025 | 237 | 86% | ₹25.4L |
| 2026 (Jan–Apr) | 69 | 67% | ₹7.2L |
| **5yr total** | **1114** | **84.6%** | **₹1.17Cr** |

Max drawdown: **-0.8%**. No year below 67% WR.

**Apr 2026 ML upgrade (commit 5d0d23b):** added `nf_kalman_trend` + 3 HMM regime probs (`hmm_bull_prob`, `hmm_neutral_prob`, `hmm_bear_prob`). Re-ran NF IC backtest with new ensemble: 1116 trades, **84.7% WR, ₹1.38Cr net** (+₹21L over baseline). Max DD improved -0.8% → -0.7%. ML directional composite: 0.5643 → 0.7071 (CatBoost champion). Auto-loaded by `ml_engine.py --predict-today` from next morning. Hurst exponent tested but dropped (0.000 importance — too slow-moving with shift(1) gate).

### NF IC Structure (CALL signal days only)
```
SELL ATM CE  + BUY ATM+150 CE   (Bear Call side: upper wing)
SELL ATM PE  + BUY ATM-150 PE   (Bull Put side: lower wing)
spread_width = 150pts (NF strike spacing = 50pts, ATM±3 strikes)
net_credit   ≈ ₹108/lot average
SL: spread cost exceeds net_credit × 1.5 (50% above credit received)
TP: NONE — IC exits at EOD 3:15 PM only (no TP = ₹21L more over 5yr)
max_lots = 10 (Dhan margin API lot sizing)
Trades CALL signal days only — PUT days routed to Bull Put
```

### NF Bull Put Structure (PUT signal days only)
```
SELL ATM PE  + BUY ATM-150 PE   (2 legs, directional credit)
spread_width = 150pts
net_credit   varies (ATM PE - ATM-150 PE)
SL: spread cost > net_credit × 1.5
TP: spread cost < net_credit × 0.35 (retain 65%)
lots = floor(capital / Dhan_API_margin)  — actual SPAN, not formula
Backtest Sep 2025–Apr 2026: 100% WR, ₹3,794 avg/trade (51 trades)
```

### Why NF IC beats BNF IC

BNF lost weekly expiry (SEBI mandate, Nov 20 2024) → monthly contracts → 15-22 DTE → gamma risk dominates theta → IC WR collapsed 70%→27% in 2025. NF kept weekly Tuesday expiry. Every NF trade is naturally DTE≤7.

### ⚠️ MANDATORY LIVE PLACEMENT ORDER: BUY FIRST, THEN SELL

Dhan hedge margin rules require the LONG leg on the books BEFORE the SHORT leg
gets margin benefit. In `auto_trader.py` for IC:

1. Place BUY ATM+150 CE (long wing) — wait for fill confirmation
2. Then SELL ATM CE (short leg) — margin benefit applies
3. Place BUY ATM-150 PE (long wing) — wait for fill
4. Then SELL ATM PE (short leg) — margin benefit applies
5. Never place short before long — full unhedged margin triggered

### NF IC cache file naming

NF files live in `data/nifty_options_cache/` (separate from BNF `data/intraday_options_cache/`).
Same suffix naming convention as BNF:

| File | Contents |
|---|---|
| `{date}_CE.csv` | ATM CE (CALL signal days) |
| `{date}_PE.csv` | ATM PE (PUT signal days) |
| `{date}_CE_p3.csv` | ATM+150 CE (CALL days) |
| `{date}_PE_m3.csv` | ATM-150 PE (PUT days) |
| `{date}_PE_straddle.csv` | ATM PE on CALL days (IC bull-put side) |
| `{date}_CE_straddle.csv` | ATM CE on PUT days (IC bear-call side) |
| `{date}_PE_m3_straddle.csv` | ATM-150 PE on CALL days (IC long put wing) |
| `{date}_CE_p3_straddle.csv` | ATM+150 CE on PUT days (IC long call wing) |

Fetch: `python3 fetch_intraday_options.py --instrument NF --spreads --start 2021-08-01`

### Common commands

```bash
# NF IC backtest (primary)
python3 backtest_spreads.py --instrument NF --strategy nf_iron_condor --ml
python3 backtest_spreads.py --instrument NF --strategy all --ml    # all 8 NF variants
python3 optimize_params.py --instrument NF                         # VIX+conf grid search

# BNF IC DTE≤7 (fallback if NF data unavailable)
python3 backtest_spreads.py --strategy iron_condor --ml --max-dte 7

# Fetch / refresh NF cache (incremental — skips existing files)
python3 fetch_intraday_options.py --instrument NF --spreads --start 2021-08-01
```

### Strategy verdict (final, Apr 2026 — do not re-backtest without regime change)

| Regime | Strategy | WR | 7yr P&L |
|---|---|---|---|
| Current (< ₹2.3L capital) | IC (CALL days) + Bull Put (PUT days) | 97.5% | ₹18.36L |
| Upgrade (≥ ₹2.3L capital) | Short Straddle | 94.9% | ₹3.15Cr |

No VIX/confidence filter on IC — no filter = max P&L per backtest.
Bear Call permanently discarded — 13.5% WR, -₹24.03L over 7yr.
Full research and reasoning: `STRATEGY_RESEARCH.md`.

---

## ⚠️ DHAN API RULE — READ THIS FIRST, EVERY SESSION

**Before writing, debugging, or modifying ANY Dhan API call — read the docs first:**

```
docs/DHAN_API_V2_REFERENCE.md
```

This file contains the complete, word-for-word Dhan HQ API v2 reference (compiled April 2026).
Endpoint signatures, request payloads, response schemas, error codes — all in there.

**Why this matters:** Dhan's response structures are non-obvious (nested instrument IDs,
float-string strike keys, segment-specific field names). Guessing wastes hours.
The docs have the exact answer. Read them first, always.

---

## ⚠️ BROKER LAYER LOCK-IN RULE — DHAN ONLY, NO RANDOM ALTERNATIVES

**The order placement, position management, and broker integration layer is LOCKED to Dhan.
Never adopt third-party libraries for this layer, regardless of stars, hype, or convenience.**

**Rules:**

1. **Source of truth for any Dhan API call** = `docs/DHAN_API_V2_REFERENCE.md` (local copy of
   official Dhan v2 docs).
2. **The ONLY trusted external Dhan reference** = official Dhan repo
   `https://github.com/dhan-oss/DhanHQ-py`. Use it for code patterns, request/response
   examples, and SDK usage. No other Dhan helper, wrapper, or fork is acceptable.
3. **`tech_scout.py` HARD-REJECTS** these categories regardless of score, stars, or marketing:
   - Order placement / OMS / smart order routing wrappers
   - Broker SDK wrappers, broker adapters, broker UIs (Zerodha / Upstox / Angel / IB / etc.)
   - Position management, P&L trackers, portfolio dashboards
   - Backtest engines (we have our own), trading bots, exchange connectors
   - Algo trading platforms (`openalgo`, `freqtrade`, `vectorbt`, `backtrader`, etc.)
4. **`tech_scout.py` ONLY queues** finds that improve: ML reasoning, feature engineering,
   model architectures, ensembling, regime detection, drift detection, calibration, feature
   selection, hyperparameter tuning, backtest methodology, statistical inference.
5. **Why:** External broker libraries have unproven track records, may break silently on Dhan
   API changes, introduce execution risk, and duplicate work `auto_trader.py` already does
   correctly against the canonical Dhan docs. The trading-execution layer is settled. Only
   the intelligence (ML, signal, risk gating) layer is fair game for new ideas.

---

## ⚠️ DHAN-FIRST DATA RULE — NEVER ASSUME, ALWAYS LOOK UP

**Every data field in this system must come from a verified source. In order:**

1. **Dhan API doc (`docs/DHAN_API_V2_REFERENCE.md`)** — the response schema is
   already in there. Find the exact field name. Use it as-is. Examples:
   - Realized P&L per leg → `realizedProfit` on `GET /v2/positions`
   - Entry/exit averages → `buyAvg`, `sellAvg` on `GET /v2/positions`
   - Historical fills → `tradedPrice`, `tradedQuantity` on `GET /v2/trades/{from}/{to}/{page}`
   - Charges (brokerage, STT, sebiTax, etc.) → explicit fields on `GET /v2/trades/...`
   - Open positions → `netQty > 0` on `GET /v2/positions`
   - Option chain spot → `data.last_price` on `POST /v2/optionchain`

2. **If the field is NOT in the doc** — WebSearch Dhan's official API site, GitHub
   discussions, or check `dhan_journal.py` patterns first. If still unfound,
   **ASK THE USER** before inventing a fallback.

3. **Building proxy / heuristic / formula** is a last resort and must be
   explicitly justified in a comment + commented-on commit. Examples of
   forbidden assumptions: estimating P&L from spot proxies, computing
   margin from contract size formulas, inferring exit_reason from time of day.

4. **Live trade journal** must read from `dhan_journal.py` helpers
   (`get_positions`, `realized_pnl`, `leg_avgs`). `today_trade.json` is
   auxiliary metadata only (signal, score, ML confidence, intent SIDs) —
   never the source of P&L or fill prices.

5. **Backfill any historical row** via `python3 backfill_dhan_history.py
   --date YYYY-MM-DD --apply` — never hand-type P&L numbers. The script
   pulls every fill from `/v2/trades/{from}/{to}/{page}` and reconstructs
   the row from real BUY/SELL prices and Dhan-reported charges.

**Why this rule exists (April 2026 discovery):**
Apr 22–24 trades were originally hand-backfilled with proxy P&L numbers
that diverged from Dhan-booked reality (e.g. Apr 22 hand-typed −₹250 vs
Dhan-actual +₹98 due to lot-sizing bug). Hand-typed numbers can never be
audited, never match the broker statement, and silently poison ML
training labels via `oracle_correct`. Every CSV value in
`live_*_trades.csv` must be traceable to a specific Dhan API response field.

---

## ⚠️ BUG FIX RULE — EVERY DEBUG SESSION

**Before writing any bug fix — search first, code second.**

1. Copy the exact error message (the last line of the traceback)
2. WebSearch it — look at GitHub issues, Stack Overflow, pandas/scikit-learn docs
3. Explain what you found in **plain English** (what caused the bug, why it happened, how the fix works)
4. Only then write the fix

**Plain English means:** no jargon. If the word "dtype", "coerce", "shadow", or "shift" appears in the explanation to the user, explain what it means in brackets. Every number needs a unit and context. Never paste a raw error traceback at the user — one sentence summary is enough.

---

## ⚠️ ML FEATURE RULE — BEFORE EDITING ml_engine.py OR data_fetcher.py

**Checklist — follow in order, every time:**

1. Add computation inside `compute_features()` in `ml_engine.py`, using `.shift(1)` on all price columns (yesterday's values only — today's prices would be cheating)
2. **Reserved variable names — never use as loop variables:** `_c`, `_c_nf`, `_vix`, `_sp`, `_nk` — these hold price series; if overwritten, every downstream calculation silently breaks
3. Add the feature name once to `FEATURE_COLS` — then check: `len(FEATURE_COLS) == len(set(FEATURE_COLS))` (duplicates inflate importance silently)
4. If feature needs a new data file: `python3 data_fetcher.py` then `python3 data_fetcher.py --backfill` (new CSVs start with 1 row — zero importance until backfilled)
5. Run `python3 ml_engine.py --analyze` — feature importance must be > 0
6. Run `python3 autoexperiment_nf.py` — **keep only if composite >= 0.6484** (Apr 2026 baseline after Kalman + HMM features added; autoloop uses live composite dynamically)
7. Commit + push to `main`

---

## ⚠️ PLAIN ENGLISH RULE — ALL USER-FACING OUTPUT

**Every message to the user must pass this test: would a non-programmer understand it?**

- Numbers need context: "composite score 0.572 — that's 5 points above our 0.552 baseline"
- Verdicts not metrics: "the model improved" not "accuracy delta +1.1pp"
- Error summaries in one sentence: "A variable name conflict in the code caused a text string to end up where a price number was expected" — not the raw Python traceback
- Status reports in plain terms: "Today's signal is CALL — model says markets will go up" not "model output: P(CALL)=0.62"

---

## ⚠️ VM PACKAGE RULE — NEVER `rm -rf` SITE-PACKAGES

**Cron jobs run as system `python3` and use packages in `~/.local/lib/python3.11/site-packages/` — NOT the `dhan-env` virtualenv.** Treat `~/.local` as production. Never assume the venv "has its own copy" and that nuking `~/.local` is safe — it isn't, and crons will silently break tomorrow.

**Three rules learned the hard way (Apr 2026 disk-cleanup session):**

1. **`~/.local` ≠ disposable.** It IS the production package store for cron. `dhan-env` only matters when scripts are run inside an activated venv (none of the crons do this). When in doubt, treat `~/.local` as untouchable.

2. **`rm -rf` on a package leaves orphan pip metadata.** Pip will then say "Requirement already satisfied" but `import` fails with `ModuleNotFoundError`. To recover: `pip install --user --break-system-packages --force-reinstall <pkg>`. To avoid the trap: use `pip uninstall <pkg> -y` (which also drops metadata), never `rm -rf`.

3. **CUDA / PyTorch sneak in via transitive deps.** Some package (likely a tabpfn/HF/transformers install) once pulled in `torch`, `triton`, and a full `nvidia/cu13` stack — together ~4 GB on a CPU-only VM. Run quarterly to catch:

   ```bash
   pip list 2>/dev/null | grep -iE "torch|nvidia|cuda|triton|tabpfn|hf-xet"
   ```

   If anything appears, `pip uninstall` it (not `rm -rf`).

**Before any cleanup of `~/.local`, run the full smoke test in the README ("Pre-Live Smoke Test") and confirm every script exits 0.** No cleanup is worth a Monday morning trade failure.

---

## ⚠️ SECURITY RULE — BEFORE EVERY GIT COMMIT

**Always run `git status` before committing. Never commit:**

- Anything inside `data/` — this includes all CSV files with trade history, backtest results, P&L records
- `models/` — trained ML model files
- `logs/` — cron job output
- `.env` — API tokens and credentials
- Any file containing actual trade P&L numbers, win rates from live trading, or account balances

The `.gitignore` already blocks most of these automatically, but **always visually scan `git status` output** before running `git commit`. If a data file appears in the staging area, remove it immediately.

---

## ⚠️ TIME / DATE TRAPS — MANDATORY READING BEFORE TOUCHING ANY DATA-RELATED CODE

**The VM clock is UTC. The market is IST. Trading data is IST. CSVs are keyed by IST trading days.
Every naive `datetime.today()` / `date.today()` / `datetime.now()` returns UTC and silently corrupts data
between midnight UTC (5:30 AM IST) and the next IST midnight.**

**Hard rules — no exceptions:**

1. **Always use IST for any "today's date" comparison.** Pattern:
    ```python
    from datetime import datetime, timezone, timedelta
    _IST_TZ = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(_IST_TZ).date()
    ```
   Files already fixed (April 2026): `validate_all.py`, `lot_expiry_scanner.py`, `data_fetcher.py`,
   `auto_trader.py` (`_IST_TZ`), `notify.py` (`_IST`), `system_health.py`, `morning_brief.py`.

2. **Never use `datetime.today()` for `TO_DATE` in any fetcher.** It returns UTC date on the VM, so
   between 6:30 PM UTC (midnight IST) and 5:30 AM IST the function appears to think yesterday is today.
   Combined with `_last_csv_date` returning "tomorrow's start", `from_date >= TO_DATE` evaluates True
   and the fetch silently no-ops — printing "up to date" while the CSV is one day stale.

3. **`_last_csv_date()` returns the NEXT-FETCH-FROM date, not the actual last CSV row date.** It adds
   `+1 day` (and now skips Sat/Sun → next Monday) so the caller can pass it directly to the API. Never
   display this value as "last row" in user-facing logs — it's misleading.

4. **Dhan `/v2/charts/historical` rejects ENTIRE chunks when `fromDate` is a Sat/Sun** — DH-905 is
   returned for the whole range, dropping every trading day inside. `_last_csv_date` advances Sat/Sun
   to the next Monday before the API call. Don't undo this.

5. **DH-905 is "no data" — NOT a failure.** It's how Dhan signals weekends, holidays, future dates,
   and pre-publish lag. Treat as silent 0-rows; only HTTP errors (4xx/5xx besides DH-905) are real
   chunk failures. `data_fetcher` collects failures and Telegram-warns; only raises when EVERY chunk
   failed (true Dhan outage).

6. **Dhan EOD candle publish lag.** Daily index candles (Nifty50, BankNifty, India VIX via Dhan) are
   typically available ~30 minutes after 3:30 PM IST close, but can lag up to a few hours on volatile
   days. yfinance often has the candle earlier — `data_fetcher` deliberately uses Dhan as the source
   of truth for index OHLCV (per CLAUDE.md DHAN-FIRST DATA RULE), so a fresh-after-close manual run
   may legitimately return 0 rows. The 9:30 AM IST cron is well past the lag window.

7. **`signals_ml.csv` can show "today" even when `nifty50.csv` does not.** `ml_engine --predict-today`
   writes today's prediction using **previous trading day's features** as a fallback when today's row
   isn't in the underlying CSVs yet. This is intentional — auto_trader needs a signal at 9:30 AM
   even if Dhan's candle isn't published yet. validate_all's row-count and staleness checks must
   continue to flag the underlying CSV freshness even when signals_ml.csv looks current.

8. **`max_stale` in validate_all is in CALENDAR days, not trading days.** Mon→Fri gap is 3 days, but
   Fri→Tue gap is also 3 calendar days even though only 1 trading day is missing. If a Mon is a
   holiday, Fri→Wed = 5 calendar days but only 1 trading day missing. Be lenient on stale-checks
   that span weekends.

9. **CSV writes must be atomic.** Every CSV/JSON write that other code reads goes through
   `atomic_io.write_atomic_*` — tmpfile + `os.replace`. Never use `.to_csv(path)` or `open(path, "w")`
   directly on shared state files. A crash mid-write or a concurrent reader sees a half-truncated
   file otherwise.

10. **Shift(1) doesn't fix everything.** A backward-looking smoother (HMM `predict_proba`,
    Kalman `.smooth()`, RTS algorithms) uses observations from t+1..T to estimate state at time t.
    `.shift(1)` only delays the OUTPUT by one row but doesn't unbias the estimator. Use forward-only
    filters (`.filter()`, manual forward-pass) for any per-row feature consumed by walk-forward ML.

---

## Known Gotchas — Session-Discovered Bugs

This table grows every session. Each entry = a bug that was debugged and must never be debugged again.

| Bug | What you'll see | Fix |
|---|---|---|
| `_c` used as loop variable inside `compute_features()` | Error: `could not convert string to float: 'pe_oi_p3'` — a column name ends up where a price number should be | Rename the loop variable to `_oi_col` or `_col` — `_c` is reserved for the BN close price series |
| New yfinance ticker CSV has only 1 row of data | Feature shows 0.000 importance in `--analyze` output | Run `python3 data_fetcher.py --backfill` to fetch full 7-year history |
| Same feature name appears twice in `FEATURE_COLS` | That feature's importance appears doubled; model wastes capacity | After any `FEATURE_COLS` edit: `python3 -c "from ml_engine import FEATURE_COLS; print(len(FEATURE_COLS), len(set(FEATURE_COLS)))"` — both numbers must match |
| `options_iv_skew.csv` doesn't exist yet | Skew features (`call_skew`, `put_skew`, `skew_spread`) all show as zero | Run `python3 data_fetcher.py --fetch-options` once to create the file |
| Opening range breakout (ORB) data is missing before August 2021 | `orb_range_pct` shows 0.000 for 2019–2021 rows | Normal — Dhan's intraday data API only goes back to mid-2021; the feature works correctly from that date forward |
| NF expiry day seen in code as "Thursday" | You read `auto_trader.py` fallback calc → assumed Thursday = current expiry → changed docs to Thursday → WRONG | NSE changed NF expiry Thursday→Tuesday from Sep 1 2025 (NSE circular). The fallback calc was stale. Dhan expirylist API returns the correct Tuesday. **Never infer expiry from code — check the NSE circular timeline: pre-Sep-2025 = Thursday, Sep-2025+ = Tuesday.** |
| Backtest DOW breakdown shows Thursday as best day | Backtest data 2021–Aug 2025 used Thursday expiry; only 7 months (Sep 2025–Apr 2026) use Tuesday expiry. Thursday "expiry day" pattern dominates the stats. | DOW stats are biased toward old Thursday-expiry world. Current (live) DOW profile: Tue = DTE 0 (best), Mon = DTE 1, Wed = DTE 6 (worst same-day). Friday = DTE 4. Don't use pre-Sep-2025 backtest DOW breakdown to judge current regime. |
| IC lot sizing gave 5 lots instead of 1 | Used `RISK_PCT=5% × capital / risk_per_lot` formula — `risk_per_lot` was theoretical P&L not actual SPAN margin | Fixed: call Dhan `/v2/margincalculator/multi` with all 4 legs × 1 lot → get actual margin → `lots = floor(capital / actual_margin)`. Param key = `scripList` not `scripts` (DH-905 error). |
| TP frac: IC has no TP; spreads use 0.65 | IC `spread_monitor.py` path removed TP check (EOD-only). Bear Call / Bull Put TP = `net_credit * 0.35` (spread decayed to 35% = retain 65%). Straddle = SL-only (no TP). | `CREDIT_TP_FRAC = 0.65` applies to spreads only. IC exits at 3:15 PM via `exit_positions.py`. Never add TP logic back to IC path. |
| Bear Call looked profitable in 5yr backtest but destroyed capital in live 7-month run | Bear Call: 17.9% WR, -₹1.25L over Sep 2025–Apr 2026. Signal model has CALL bias → market went up → short CE got hit. Backtest used historical signal accuracy; live accuracy lower. | `IC_SKIP_DAYS = {2,3,4}`. No trade Wed/Thu/Fri. IC Mon+Tue only. Bear Call fully removed Apr 2026. |
| "Wednesday is bad for IC" rule was wrong in new Tue-expiry regime | Old rule came from Thu-expiry backtest. In new Tue-expiry regime, Wed IC = 95.8% WR +₹14K. Thu 100%, Fri 100%. IC profitable ALL 5 days. | `IC_SKIP_DAYS = set()` — no skip days. Run IC every weekday. |
| IC TP was costing ₹21L over 5 years | Standard IC (TP=0.65): ₹1.17Cr. EOD-only IC (no TP): ₹1.38Cr. Same WR, lower drawdown. TP captured 65% of credit but left last 35% of theta. | `spread_monitor.py` IC path no longer checks TP — SL only. IC always exits at EOD 3:15 PM via `exit_positions.py`. |
| Straddle auto-upgrade with 0 lots | `STRADDLE_MARGIN_PER_LOT` set too low (e.g. 217K) but actual Dhan SPAN = 226,492 → capital hits threshold, straddle triggered, but `floor(capital/actual_margin)=0` lots → zero-lot order or crash | Keep `STRADDLE_MARGIN_PER_LOT` ≥ actual Dhan SPAN + 3K buffer. Run `python3 check_margins.py` to verify actual SPAN; update constant if it drifts above threshold. Current: 230,000. |
| Straddle `today_trade.json` schema differs from IC | Straddle uses `ce_sid`/`pe_sid`/`ce_entry`/`pe_entry` (no `short_sid`/`long_sid`). IC uses `ce_short_sid`/`ce_long_sid`/`pe_short_sid`/`pe_long_sid`. 2-leg uses `short_sid`/`long_sid`. | Match the strategy key: `nf_short_straddle` → straddle schema; `nf_iron_condor` → IC schema; `bear_call_credit`/`bull_put_credit` → 2-leg schema. |
| Bull Put lot sizing used wrong formula (over-sized) | `get_spread_legs()` uses `max_loss_per_lot` as margin proxy. With 150pt spread: formula gives 10 lots when actual Dhan SPAN is ~₹51K/lot → only 2 affordable. | `_fetch_spread_margin_per_lot()` queries Dhan `/v2/margincalculator/multi` for actual SPAN. Lot count overridden after `get_spread_legs()` call: `lots = min(MAX_LOTS, int(capital // margin_1lot))`. Never use formula for lot sizing. |
| Bull Put margin from Dhan `multi` API ≈₹220K (not ₹55K) | Dhan `/v2/margincalculator/multi` returns unhedged-equivalent margin for Bull Put spread (~₹220K/lot at current NF levels). Capital of ₹1.12L = 0 affordable lots → `die()` was called, killing the trade. | Fallback added: when Bull Put `lots < 1`, send Telegram alert and route to IC instead (market-neutral, works on any signal day, ~₹93K/lot). `_use_bull_put_today` is set to `False` so the IC guard `not _use_bull_put_today` evaluates True. `BULL_PUT_MARGIN_PER_LOT = 55_000` is only used when the API call fails. |
| IC `trade_journal.py` shows CE/PE credit ₹0 | `_write_today_ic_trade()` in `auto_trader.py` writes keys `ce_credit`/`pe_credit`. `trade_journal.py` was reading `ce_net_credit`/`pe_net_credit` (wrong key) → always 0. | Fixed: lines 287-288 in `trade_journal.py` now use `intent.get("ce_credit")` / `intent.get("pe_credit")`. The CSV column names remain `ce_net_credit`/`pe_net_credit`. |
| IC on PUT signal days (wrong strategy routing) | Before Apr 2026: `_use_bull_put_today = False` always → IC ran on both CALL and PUT days, wasting the 100% WR Bull Put edge. | Fixed: `_use_bull_put_today = (signal == "PUT")`. IC path guarded with `and not _use_bull_put_today`. Do not revert. |
| Wrong exit leg order (IC/spread) | Closing long (wing) before short triggers naked short exposure → Dhan demands full unhedged margin mid-exit, can cause rejection or margin call. | Always close shorts first (BUY back), then longs (SELL). IC: CE short → PE short → CE long → PE long. Spread: short → long. Both `_close_ic()` and `_squareoff_all()` now enforce this order. |
| `pnlExit` value format wrong | `str(round(1500.0, 2))` = `"1500.0"` (one decimal). Dhan API expects `"1500.00"` (two decimals). Wrong format likely causes non-ACTIVE response silently. | Use `f"{value:.2f}"` not `str(round(value, 2))`. Fixed in `_setup_pnl_exit()`. |
| `DELETE /v2/positions` outside market hours | Returns non-SUCCESS if called during AMO window (after 3:30 PM) or on weekends. spread_monitor stops at 3:10 PM, exit_positions runs at 3:15 PM — both within window. | Only an issue if `exit_positions.py` or `spread_monitor.py` is called manually outside 9:15 AM–3:30 PM IST. Backup leg-by-leg path handles it automatically. |
| IC SL exit field names mismatch (`spread_monitor` → `trade_journal`) | IC SL triggered, position closed, but `trade_journal.py` showed P&L = ₹0 for all legs — entire SL path had silent data loss | `spread_monitor.py` was writing `exit_ce_short_ltp`/`exit_pe_short_ltp` etc. `trade_journal.py` expected `ce_short_exit`/`pe_short_exit`. Fixed both to use `ce_short_exit`, `ce_long_exit`, `pe_short_exit`, `pe_long_exit`. |
| Duplicate IC guard inverted default | Two IC trades placed same morning (guard supposed to block 2nd if today already traded) | `today_trade.get("exit_done", True)` → default=True means "already exited" even when field missing → guard never blocked. Fix: `get("exit_done", False)` — absent = not yet exited = proceed to place. |
| UTC date used in IC duplicate guard | Guard compares `date.today()` (UTC) against `today_trade["date"]` (IST). Before ~5:30 AM IST, UTC date = yesterday → guard thinks "different day" → allows duplicate trade | Use `datetime.now(_IST).date().isoformat()` everywhere a trade date comparison is needed. |
| Straddle exit field name mismatch (`spread_monitor` → `trade_journal`) | Straddle SL triggered correctly but `trade_journal.py` always showed exit P&L = ₹0 | `spread_monitor.py` writes `exit_spread`. `trade_journal.py` was reading `exit_cost` (wrong key). Fixed: `intent.get("exit_spread", intent.get("exit_cost", 0))`. |
| EOD exit prices not written for Bull Put / Straddle | `exit_positions.py` captured IC exit LTPs but silently skipped Bull Put and Straddle → journal showed LTP=₹0 for those strategies | Extended `_write_exit_to_today_trade()` to handle all 3 strategies. IC: writes `ce_short_exit`/`ce_long_exit`/`pe_short_exit`/`pe_long_exit`. Spread: writes `exit_short_ltp`/`exit_long_ltp`/`exit_spread`. Straddle: writes `exit_ce_ltp`/`exit_pe_ltp`/`exit_spread`. |
| IC journal showed "position still open / ₹0" for all legs | `trade_journal.py` `_journal_ic()` read only `today_trade.json` for exit prices but that file had no exit fields before the above fix | Now reads per-leg exit prices (`ce_short_exit` etc.) and shows a `<pre>` table: each leg's entry price, exit price, and individual P&L. `has_exits` flag triggers detailed view; falls back to summary-only if field absent. |
| New ML feature accessed `df["close"]` instead of `df["nf_close"]` | `autoexperiment_nf.py` returned `{"error": "compute_features failed: 'close'", "composite": 0.0}` — entire feature pipeline crashed | NF daily close column is named `nf_close` (line 373 in `ml_engine.py`: `_c = pd.to_numeric(d["nf_close"], errors="coerce").shift(1)`). When adding any new feature that needs the daily close, use `d["nf_close"]` not `d["close"]`. Apply `pd.to_numeric(..., errors="coerce").ffill().bfill()` to handle gap days. |
| Lazy-imported lib not installed → feature shows 0.000 importance | All HMM/Kalman/GARCH features showed 0.000 importance even though code looked correct | The `try: import <lib>` block silently fell to `_LIB_OK=False` because the lib was never installed → fallback returned constant value → constant column = zero variance = zero importance. Always run `pip install --break-system-packages <lib>` on the VM BEFORE running `--analyze`. PEP 668 blocks plain pip on Debian-managed systems; use the flag. |
| Bear Call appears profitable in NF backtest_spreads.py output | Output: 61.9% WR, ₹35.9L over 5yr — contradicts the "permanently dumped at 13.5%" line above. | Different routing. CLAUDE.md verdict was Bear Call on **CALL signal days** (direction conflict — model says up, you sell calls). The new `backtest_spreads.py` runs Bear Call on **PUT signal days** (model says down, you sell calls = aligned). Even with correct routing, Bull Put on PUT days still wins (65.7% WR, ₹46.8L over 5yr). Verdict holds — Bear Call stays out of live system. |
| GARCH(1,1) volatility feature added but composite dropped 0.6484 → 0.6449 | `garch_vol` and `garch_vol_z` both showed 0.000 importance after `arch` lib installed and verified working | GARCH conditional vol is highly correlated with `hv20` + `vix_level` already in features → no marginal info. The lib also took 30s+ to fit on full history. Reverted in commit 6a01ca2. Lesson: before adding a vol feature, check it's not redundant with existing realized-vol features. |
| `rm -rf` site-packages folder broke cron next morning | `pip install <pkg>` says "Requirement already satisfied" but `python3 -c "import <pkg>"` fails with `ModuleNotFoundError`. Cron silently dies. | `rm -rf` deletes files but leaves `.dist-info` metadata → pip is fooled. Recovery: `pip install --user --break-system-packages --force-reinstall <pkg>`. Avoidance: always use `pip uninstall <pkg> -y`, never `rm -rf` on site-packages. |
| Cleanup of `~/.local` packages assuming `dhan-env` virtualenv had a duplicate | After deletion, every cron-invoked script (`auto_trader.py`, `ml_engine.py`, etc.) crashed with `ModuleNotFoundError: numpy` | Cron runs as system `python3` and resolves imports from `~/.local/lib/python3.11/site-packages/`, NOT from `dhan-env/`. The venv copy is irrelevant unless the cron entry activates the venv (none do). Treat `~/.local` as production. |
| 4 GB of unused PyTorch + CUDA libraries on a CPU-only VM | `df -h /` showed 63% disk used (12 GB / 20 GB). Largest files: `torch/libtorch_cuda.so` (380 MB), `nvidia/cu13/*` (~2 GB total), `triton` (397 MB). Never imported by any trading script. | Pulled in transitively by some past `pip install` (likely `tabpfn` or `transformers`). Audit quarterly: `pip list 2>/dev/null \| grep -iE "torch\|nvidia\|cuda\|triton\|tabpfn\|hf-xet"`. Uninstall any hits with `pip uninstall <pkg> -y`. |
| `data_fetcher` "up to date" but CSV is days stale | After-midnight-IST run on UTC-clocked VM logs `Nifty50: up to date (last row = 2026-04-27)` while `nifty50.csv` actually ends Fri 24 Apr | `TO_DATE = datetime.today()` returns UTC date — yesterday's date until 5:30 AM IST. Combined with `_last_csv_date` advancing Fri+1 → next Mon, `from_date >= TO_DATE` evaluates True and the fetch silently skips. Fix: `TO_DATE = datetime.now(_IST_TZ).strftime("%Y-%m-%d")`. Commit `271e5c1`. |
| `data_fetcher` drops Mon trading day after Fri last row | `nifty50.csv` stuck on Friday for days even though Mon was a real trading day; "no new trading data (2026-04-25 — weekend/holiday)" log line | Dhan `/v2/charts/historical` returns DH-905 for the ENTIRE chunk if `fromDate` is Sat/Sun, even when trading days exist inside the range. `_last_csv_date` returned `last+1` blindly → Fri+1 = Sat → whole chunk dropped. Fix: advance Sat/Sun → next Monday in `_last_csv_date`. Commit `c278a96`. |
| `RuntimeError` on first transient Dhan blip aborted entire morning trade | One yfinance/Dhan chunk fails → all-or-nothing raise → `auto_trader.py` morning fetch dies → no trade placed | Original tightening was too aggressive. Now: collect failures, Telegram-warn, only raise when EVERY chunk failed (true outage). Successful chunks are merged; next run's `_last_csv_date` will retry the gap naturally. Commit `d35ad14`. |
| HMM `predict_proba` returned smoothed posteriors → walk-forward leakage | All nightly HMM features looked correct but composite dropped 0.064 after switching to "forward-only" with `_do_forward_pass` because hmmlearn 0.3.3 removed that private API → exception path → constant 0.33 fallback → zero-variance feature | hmmlearn `predict_proba(X)` is the forward-BACKWARD smoother, not just forward — uses observations from t+1..T even with `.shift(1)`. The `_do_forward_pass` API was removed; use a manual numpy log-alpha forward pass. Commit `f19e493`. |
| Kalman `.smooth()` (RTS) leaks future into walk-forward | `nf_kalman_trend` computed via `_kf.smooth(X)` then `.shift(1)` — looked safe but RTS smoother conditions on x_{t+1}..x_T at every t, so the shifted value still saw the future | Replace `.smooth()` with `.filter()` (forward-only Kalman). For ANY temporal feature consumed by walk-forward ML: confirm the algorithm uses only x_0..x_t, not future obs. Commit `c997084`. |
| `champion.pkl` overwritten unconditionally each evolver run → quality random walk | Score went up then down then up nightly; champion was whatever last run produced regardless of quality | Add a score ratchet to `save_champion()`: only overwrite when new score ≥ existing. Cross-day scores aren't apples-to-apples but still prevents random-walk degradation. Backed by `champion_meta.json["score"]`. Commit `f9fc31b`. |
| Stale leaked-score baseline blocked good champion promotion | After fixing HMM/Kalman/holdout leaks, new clean composite ~0.54 < leaked baseline 0.736; ratchet correctly REFUSED to overwrite. Production stuck on the leaked champion forever. | When fixing leakage that materially drops the score, manually wipe `models/champion_meta.json` (and `champion.pkl`) so the next evolver run sets a fresh apples-to-apples baseline. Don't fight the ratchet. |
| Live-feedback rows leaked into model_evolver holdout | `_load_live_outcomes` appended live rows at the END of `X_aug`; `_temporal_split` then took the last 252 rows as holdout — which were the freshly-injected, 10×-weighted live rows | Insert injected rows at index `len(X_all) - HOLDOUT_DAYS`, so the last HOLDOUT_DAYS rows of X_aug remain pristine historical data. Commit `c997084`. |
| `or` fallback in `trade_journal` flipped real Dhan zeros to stale intent | Dhan legitimately reported `buy_avg = 0.0` (one wing didn't fill) → `leg.get("buy_avg") or float(intent.get(...))` evaluated the LHS as falsy and returned the stale intent value → CSV row had a synthetic price that didn't match Dhan's ledger | Replace `value or fallback` with explicit `_pick(value, fallback)` that uses `is None` checks. Genuine 0.0 from Dhan is preserved. Commit `c997084`. |
| `notify.send` returned True silently when Telegram creds were missing | All alerts looked successful in code, including critical SL-fired alerts. health_ping showed green. Operator never knew alerts weren't being delivered. | Return `False` + write `[CREDS MISSING — alert not sent]` stub to `data/critical_alerts.log` so health_ping can detect Telegram outage. Commit `c997084`. |
| `system_health` Telegram silently dropped on `<>&` in interpolated content | Daily report message sent with `parse_mode="HTML"`; commit subjects, exception strings, Claude API descriptions can include `<` / `>` / `&` → Telegram returns 400 → message dropped. Daily report doesn't carry critical-marker so the fallback log doesn't even capture it. | `html.escape()` every interpolated value via `_esc()` helper. Commit `c997084`. |
| `lstrip("json")` strips ANY leading char in {j,s,o,n} | `morning_brief.py` parsed Claude API response with ```` ```json ```` fences using `text.lstrip("json")` — which strips characters individually, not the prefix. JSON like `{"sentiment":"...} ` got mauled into `{"entiment":"..."}` because `s` was stripped. | Use prefix-aware removal: `if inner.startswith("json"): inner = inner[4:].lstrip()`. Commit `c997084`. |

---

## What This System Does

Fully automated Nifty50 options trading. Cron fires at 9:30 AM IST on trading days:
data → rule signal → ML override → strategy router → orders via Dhan → Telegram alert.

Strategy by signal: CALL days → IC (4-leg, all weekdays). PUT days → Bull Put (2-leg, all weekdays).
Auto-upgrades to Short Straddle when capital ≥ ₹2.3L. No human input needed.

---

## File Index (one line each)

| File | Purpose |
|---|---|
| `auto_trader.py` | Morning runner (9:30 AM) — orchestrates all steps, picks credit spread legs, places both orders via Dhan |
| `spread_monitor.py` | Intraday cron (every 1 min, 9:30 AM–3:10 PM) — watches spread cost; primary exit via DELETE /v2/positions, backup leg-by-leg |
| `signal_engine.py` | Rule-based signal scorer (4 active indicators) → `data/signals.csv` |
| `ml_engine.py` | Walk-forward training → `data/signals_ml.csv`; fast predict via champion.pkl + ensemble |
| `model_evolver.py` | Nightly 11 PM — Optuna HPO (RF/XGB/LGB/CAT) → `models/champion.pkl` + ensemble |
| `backtest_engine.py` | Naked-option historical P&L sim with cost model + lot-size timeline (legacy validation) |
| `backtest_spreads.py` | Multi-leg credit-spread backtest — Bear Call / Bull Put / Long Straddle / regime router |
| `fetch_intraday_options.py` | Fetches 1-min option premiums into `data/intraday_options_cache/` for real-options backtests |
| `backtest_live_context.py` | Research tool — tests intraday live-context override rules |
| `data_fetcher.py` | Downloads OHLCV + global market data → `data/*.csv` |
| `health_ping.py` | Pre-market heartbeat (8:50 AM) — token/capital/freshness checks |
| `midday_conviction.py` | Midday thesis reassessment (11 AM) — branches on spread vs naked schema → Telegram summary |
| `exit_positions.py` | EOD 3:15 PM — primary: DELETE /v2/positions (one call); backup: leg-by-leg shorts-first; writes EOD fallback to today_trade.json on early-exit path |
| `trade_journal.py` | EOD 3:30 PM — reads realized P&L from Dhan `/v2/positions` (single source of truth), idempotent upsert by date |
| `dhan_journal.py` | Dhan API helper module — `get_positions`, `realized_pnl`, `leg_avgs`, `fetch_trade_history`, `leg_pnl_from_fills`, `trade_pnl_for_date` |
| `backfill_dhan_history.py` | Reconstruct CSV rows from `/v2/trades/{from}/{to}/{page}` historical fills + charges. Auto-detects strategy. Dry-run by default; `--apply` to write |
| `weekly_audit.py` | Saturday 7:30 AM safety net — walks last week, cross-checks Dhan tradebook vs CSV, auto-runs backfill on gaps. EXCLUDED_DATES skips known system-bug days |
| `system_health.py` | Daily 7:00 AM evolution report — composite trend, champion accuracy, live WR + P&L, research velocity |
| `wiki_compiler.py` | Compiles `docs/wiki/raw/*.txt` discoveries into knowledge-base articles via Claude API (Karpathy LLM Wiki pattern) |
| `tech_scout.py` | Weekly autonomous scanner — GitHub/arXiv/HN → Claude scores relevance 1-10 → queues high-scorers for autoloop experiments → Telegram digest |
| `lot_expiry_scanner.py` | Monthly cron — detects Nifty50 lot size / expiry day changes |
| `regime_watcher.py` | Monthly cron (2nd of month) — detects regime changes, runs backtest, auto-patches LOT_SIZE, sends Telegram strategy verdict |
| `backtest_hold_periods.py` | Strategy research tool — multi-strategy BS-model backtest with regime-report, DOW-breakdown, hold-period analysis |
| `STRATEGY_RESEARCH.md` | Final strategy research — 7yr backtest results, IC+BullPut verdict, permanently discarded strategies |
| `replay_today.py` | Post-mortem tool — ensemble replay of today after evolver |
| `renew_token.py` | Twice-daily token renewer (7:55 AM + 11 PM IST + @reboot) |
| `notify.py` | Telegram send/log helper (2 functions) |
| `autoloop_nf.py` | Daily midnight autoresearch — paper-trades ML changes, auto-promotes after 3 nights of outperformance |
| `ml_engine_paper.py` | Paper copy of ml_engine.py — autoresearcher tests here first before promoting to live |
| `autoexperiment_nf.py` | Fast 252-day holdout evaluator; `--module ml_engine_paper` to eval paper model |
| `autoexperiment_backtest.py` | Backtest evaluator for auto_trader.py constant changes |
| `analyze_confidence.py` | Diagnostic tool — confidence buckets, VIX regime accuracy, feature importances; `--write-threshold` updates dynamic VIX filter |
| `morning_brief.py` | 9:15 AM news sentiment — fetches Nifty50 headlines, calls Claude API, writes `data/news_sentiment.json` for auto_trader vote |
| `setup_automation.sh` | One-shot VM setup: pip deps, cron install, dry-run verification |
| `check_margins.py` | Live margin checker — queries Dhan /margincalculator/multi for all 5 strategies vs account balance; shows lots affordable, daily income |

---

## Key Constants (auto_trader.py)

```python
# Mode flags
PAPER_MODE         = False       # LIVE — real NF IC orders via Dhan from 22 Apr 2026
CREDIT_SPREAD_MODE = True        # primary strategy; False = legacy naked-option fallback path

# Sizing
LOT_SIZE         = 65            # Nifty50 lot size (Jan 6 2026+ — was 75 before Jan 6 2026)
MAX_LOTS         = 10            # NF IC max lots (margin tied on both sides)
IC_MARGIN_PER_LOT        = 100_000  # FALLBACK only; live call goes to /margincalculator/multi
BULL_PUT_MARGIN_PER_LOT  =  55_000  # FALLBACK only (API error path). Live Dhan /margincalculator/multi
                                     # returns ~₹220K/lot for Bull Put (unhedged-equivalent SPAN).
                                     # At capital < ₹220K: Bull Put skipped → IC fallback (auto, via Telegram alert)
                                     # Actual sizing for both: lots = floor(capital / Dhan_API_margin)

# Credit-spread params (active path)
SPREAD_WIDTH     = 150           # NF: 50pt strike spacing × 3 = ATM±150 (BNF was 300)
CREDIT_SL_FRAC   = 0.5           # SL when spread cost grows to net_credit × 1.5
CREDIT_TP_FRAC   = 0.65          # TP when spread cost falls to net_credit × 0.35

# Filters
ML_CONF_THRESHOLD = 0.55         # skip trade when ML ensemble confidence below this
VIX_MIN_TRADE     = 13.0         # dynamic — analyze_confidence.py --write-threshold updates nightly
VIX_MAX_TRADE     = 20.0         # ceiling — panic regime above this
IC_SKIP_DAYS         = set()      # no skip — IC profitable ALL 5 days (Tue-expiry regime)
                                  # DOW backtest Sep 2025+: Mon 88.5%, Tue 89.5%, Wed 95.8%,
                                  # Thu 100%, Fri 100%. "Wed bad" was stale Thu-expiry data.
STRADDLE_MARGIN_PER_LOT = 230_000 # auto-upgrade threshold; actual Dhan SPAN ≈₹2,26,492 + ₹3,508 buffer
MAX_LOTS_STRADDLE    = 5         # straddle uses ~2.5× IC margin (~₹2.3L vs ₹93K)

# Naked-option legacy params (only used when CREDIT_SPREAD_MODE=False)
SL_PCT       = 0.15              # 15% stop-loss on premium
PREMIUM_K    = 0.004             # approx premium factor: BN_open × PREMIUM_K × sqrt(DTE)
ITM_WALK_MAX = 2                 # max 200pt ITM probe when capital is flush
RR           = 2.5               # reward:risk (SL=15% → TP=37.5%) — grid-optimised
```

---

## 9:30 AM Flow (auto_trader.py main)

```
0. _acquire_lock()                      — fcntl prevents double cron execution
1. check_credentials()                  — Dhan token valid? API reachable?
2. _check_lot_size()                    — alert if LOT_SIZE doesn't match expected
3. _check_exit_marker()                 — yesterday's exit ran? (skipped in PAPER mode)
4. refresh_data_and_signal()            — data_fetcher → signal_engine → ml_engine --predict-today
5. _is_trading_day()                    — NSE holiday check
6. IC_SKIP_DAYS check (set()) — no skip days; all 5 weekdays reach trade path
7. get_todays_signal()                  — reads signals_ml.csv (falls back to signals.csv)
   ├── NONE / score=0 / event_day → Telegram "No Trade" → exit
   └── CALL/PUT → continue
8. _use_bull_put_today = (signal == "PUT")  — routes PUT days to Bull Put
9. VIX + ML filters (bypassed for IC — no filter = max P&L per backtest)
10. get_capital()                       — Dhan fundlimit API (availabelBalance field)
    Straddle auto-upgrade check:
    _use_straddle_today = (capital >= STRADDLE_MARGIN_PER_LOT)
    If True AND signal==CALL → IC replaced by Short Straddle
11. get_expiry()                        — Dhan expirylist API (falls back to last-Tuesday calc)

── STRADDLE PATH (_use_straddle_today = True, capital ≥ ₹2.3L, CALL days) ──
12s. get_straddle_legs(expiry, capital) — ATM CE + ATM PE SIDs from OC
                                          _fetch_straddle_margin_per_lot() → actual SPAN margin
                                          lots = min(MAX_LOTS_STRADDLE, capital // margin_1lot)
13s. Telegram: straddle details         — both SELLs, net credit, SL trigger
14s. _check_no_existing_position()
15s. place_straddle()                   — SELL CE, wait 2s, SELL PE
     └── 🚨 PARTIAL_STRADDLE if PE SELL fails after CE placed
16s. _write_today_straddle_trade()      — today_trade.json: strategy=nf_short_straddle
                                          fields: ce_sid/pe_sid/ce_entry/pe_entry/net_credit

── BULL PUT PATH (_use_bull_put_today = True, all PUT signal days) ──────────
12p. get_spread_legs("PUT", expiry, capital) — SELL ATM PE + BUY ATM-150 PE (leg SIDs + LTPs)
     _fetch_spread_margin_per_lot() → actual Dhan SPAN margin
     lots = min(MAX_LOTS, capital // margin_1lot)
13p. Telegram: bull put details         — 2 legs, net credit, SL, TP, lot count
14p. _check_no_existing_position()      — no duplicate guard via today_trade.json
15p. place_credit_spread()              — BUY long (hedge) first, SELL short
16p. _write_today_spread_trade()        — today_trade.json: strategy=bull_put_credit
                                          fields: short_sid/long_sid, net_credit, lots
17p. _setup_pnl_exit(net_credit, lots)  — POST /v2/pnlExit: account-level safety net
                                          lossValue = net_credit × 0.5 × qty (mirrors SL)
                                          fires only if spread_monitor misses SL (VM crash etc)

── IC PATH (IRON_CONDOR_MODE, all CALL signal days) ─────────────────────────
12i. get_ic_legs(expiry, capital)       — all 4 legs from OC; _fetch_ic_margin_per_lot()
                                          lots = min(MAX_LOTS, capital // api_margin)
13i. compute_chain_signals()            — max-pain + GEX (informational)
14i. Telegram: IC details               — 4 legs, net credit, SL trigger (NO TP — EOD only)
15i. _check_no_existing_position() via today_trade.json (IC duplicate guard)
16i. place_iron_condor(ic, expiry)      — BUY CE long → SELL CE short → BUY PE long → SELL PE short
     └── 🚨 PARTIAL_IC* alerts if any leg fails mid-sequence
17i. _write_today_ic_trade()            — today_trade.json: strategy=nf_iron_condor
                                          fields: ce/pe_short_sid/long_sid, ce/pe_credit, net_credit
18i. _setup_pnl_exit(net_credit, lots)  — same P&L safety net as Bull Put path above
```

---

## 11 PM Evolver Flow (model_evolver.py)

```
1. Fetch all data sources (Dhan + yfinance + NSE FII + PCR)
2. compute_features() from ml_engine — 64 features across technicals, macro, flow, options, IV skew, OI surface, ORB, breadth
3. Feature selection via RF importance (keep > 1%)
4. Optuna HPO: 30 trials × RF + XGB + LGB + CAT = 120 trials (~8-12 min)
5. Champion = best on 252-day temporal holdout (accuracy + recall blend)
6. Retrain champion on full data; train full 4-model ensemble
7. Save: models/champion.pkl + models/champion_meta.json + models/ensemble/*.pkl
8. Predict tomorrow using ensemble vote (falls back to most recent trading day if today's row missing)
9. Telegram: evolver report (plain-language summary)
```

Live feedback: the evolver reads `data/live_ic_trades.csv` and injects real outcomes with 10× weight; historical rows matching miss-day patterns get 3× weight boost.

---

## ML Fast Path (ml_engine.py --predict-today)

```python
# Fast path (< 5 sec): loads models/ensemble/*.pkl if trained within 2 days;
#                      falls back to models/champion.pkl if no ensemble
# Slow fallback (30 sec): retrains RF from scratch (only if no saved models exist)
```

---

## Option Chain Structure (Dhan v2)

```python
chain = POST /v2/optionchain {UnderlyingScrip: 13, UnderlyingSeg: "IDX_I", Expiry: "YYYY-MM-DD"}
inner = chain["data"]                 # dict with last_price + oc (no intermediate key)
spot  = inner["last_price"]           # spot index price
oc    = inner["oc"]                   # strike → {ce: {...}, pe: {...}}
sid   = oc["55900.000000"]["ce"]["security_id"]   # float-string keys
iv    = oc["55900.000000"]["ce"]["implied_volatility"]  # ATM IV (%)

# Always fetch expirylist first:
expiries = POST /v2/optionchain/expirylist {UnderlyingScrip: 13, UnderlyingSeg: "IDX_I"}
expiry_str = expiries["data"][0]      # nearest valid expiry
```

---

## Nifty50 Lot Size Timeline (backtest_engine.py get_lot_size)

| Period | Lot size |
|---|---|
| Before Jan 6 2026 | 75 |
| Jan 6 2026+ | 65 |

Live overrides stored in `data/lot_size_overrides.json` (written by `lot_expiry_scanner.py`).

---

## Nifty50 Expiry

Nifty50 has **weekly Tuesday expiry** (confirmed via Dhan expirylist API — never changed to monthly).
Every NF IC trade is naturally DTE ≤ 7. All 5 weekdays are valid entry days.

---

## Data Files (all gitignored — GCP VM only)

| File | Contents |
|---|---|
| `data/nifty50.csv` | Daily OHLCV from Dhan |
| `data/india_vix.csv` | ^INDIAVIX from yfinance |
| `data/sp500.csv` | ^GSPC from yfinance |
| `data/nikkei.csv` | ^N225 from yfinance |
| `data/sp500_futures.csv` | ES=F from yfinance |
| `data/gold.csv`, `crude.csv`, `usdinr.csv`, `dxy.csv`, `us10y.csv` | Macro series from yfinance |
| `data/pcr.csv`, `data/pcr_live.csv` | Historical + live Put/Call Ratio from Dhan |
| `data/fii_dii.csv` | FII/DII net activity |
| `data/signals.csv` | Rule-based signals (signal_engine.py output) |
| `data/signals_ml.csv` | ML-overridden signals (ml_engine.py output) |
| `data/options_atm_daily.csv` | Real ATM option opens from Dhan rollingoption (date, call_premium, put_premium) |
| `data/options_iv_skew.csv` | Daily ATM + OTM±3 implied volatilities (date, call_iv_atm, put_iv_atm, call_iv_otm, put_iv_otm) |
| `data/options_oi_surface.csv` | Daily OI at ATM±3 strikes × CE/PE (date, atm_strike, ce_oi_m3..p3, pe_oi_m3..p3) |
| `data/nifty50_15m_orb.csv` | NF 9:15-9:30 opening range candles (date, orb_open/high/low/close) for ORB features |
| `data/bankbees.csv` + `hdfcbank.csv` + `icicibank.csv` + `kotakbank.csv` + `sbin.csv` + `axisbank.csv` | Bank ETF + top-5 NF constituents (yfinance daily OHLCV) for breadth + flow features |
| `data/live_ic_trades.csv` | Daily IC trade outcomes (written by trade_journal.py) |
| `data/today_trade.json` | What auto_trader placed today (read by trade_journal) |
| `data/midday_checkpoints.csv` | Midday conviction snapshots — reversal detection, fed to model_evolver + autoloop |
| `data/paper_performance.csv` | Daily live vs paper model scores — combined_advantage drives promotion streak |
| `data/paper_changes.json` | Accumulated plain-English log of paper model improvements (reset on promotion) |
| `data/vix_threshold.json` | Dynamic VIX trade filter — written nightly by analyze_confidence.py, read by auto_trader.py at startup |
| `data/news_sentiment.json` | Today's pre-market news sentiment — written by morning_brief.py at 9:15 AM, consumed by auto_trader.py |
| `models/champion.pkl` | Best HPO model from last evolver run |
| `models/champion_meta.json` | Model type, accuracy, feature list, trained_at |
| `models/ensemble/*.pkl` | 4-model ensemble (rf/xgb/lgb/cat) for live voting |
| `models/ensemble_meta.json` | Per-model meta for each ensemble member |

---

## Cron Schedule (GCP VM)

Installed by `setup_automation.sh`:

```
25 2   * * *    renew_token.py          # 7:55 AM IST (pre-trade renewal)
30 17  * * *    renew_token.py          # 11:00 PM IST (overnight renewal)
# @reboot renew_token.py               # safety net on VM restart
35 3   * * 1-5  health_ping.py          # 9:05 AM IST
45 3   * * 1-5  morning_brief.py        # 9:15 AM IST (news sentiment → data/news_sentiment.json)
0  4   * * 1-5  auto_trader.py          # 9:30 AM IST
30 5   * * 1-5  midday_conviction.py    # 11:00 AM IST
45 9   * * 1-5  exit_positions.py       # 3:15 PM IST
0  10  * * 1-5  trade_journal.py        # 3:30 PM IST
30 17  * * 1-5  model_evolver.py        # 11:00 PM IST
30 18  * * 1-5  autoloop_nf.py          # Mon–Fri midnight IST (autoresearch, after evolver)
30 4   1 * *    lot_expiry_scanner.py   # 1st of month, 10:00 AM IST
45 4   2 * *    regime_watcher.py       # 2nd of month, 10:15 AM IST (after scanner)
0  2   * * 6    weekly_audit.py         # Sat 7:30 AM IST (gap detection + auto-recovery)
30 18  * * 6    tech_scout.py           # Sunday midnight IST (weekly innovation scan)
0  1,13 * * *   git pull (autopull.log) # 06:30 + 18:30 IST (latest code on VM)
30 1   * * *    system_health.py        # 7:00 AM IST (daily evolution report)
30 20  * * 0    log rotation            # Sunday 2:00 AM IST (trim logs > 10 MB)
```

(Times in UTC cron; comments show the IST equivalent.)

---

## Resilience Matrix — every cron + every component, what fails, what recovers

| Cron / Function | Failure mode | Backup / Recovery |
|---|---|---|
| `renew_token.py` (7:55 AM, 11 PM) | Dhan auth API down → token expires next day | `@reboot` retry; 1st morning trade fails fast on 401 → manual `.env` edit |
| `health_ping.py` (9:05 AM) | Data CSVs stale or token bad | Telegram warns; auto_trader.py also re-checks `data_fetcher.py` freshness |
| `morning_brief.py` (9:15 AM) | Claude API timeout / rate limit | `news_sentiment.json` may be stale; auto_trader.py treats stale sentiment as neutral (no veto) |
| `auto_trader.py` (9:30 AM) | Order rejection / partial fill / API outage | Telegram alert with `🚨 PARTIAL_IC` / `PARTIAL_STRADDLE`; `today_trade.json` records intent so spread_monitor can pick up; pnlExit safety net at account level |
| `spread_monitor.py` (every 1 min, 9:30 AM–3:10 PM) | VM crash, rate limit, IC SL miss | `pnlExit` (account-level loss trigger via Dhan `/v2/pnlExit`) fires automatically if losses breach threshold; exit_positions.py at 3:15 PM is hard backstop |
| `exit_positions.py` (3:15 PM) | DELETE /v2/positions returns ERROR | leg-by-leg fallback path (close shorts first); deadline-breach Telegram if 3:20 PM passes still open; pnlExit covers worst case |
| `trade_journal.py` (3:30 PM) | Process crash, race condition, today_trade.json incomplete | Idempotent _upsert_csv_row; reads `/v2/positions` directly (Dhan-truth); manual re-run safe; **weekly_audit.py Sat catches any miss** |
| `model_evolver.py` (11 PM) | Library / data fetch fail; Optuna hang | Next night retry; `champion.pkl` + ensemble from previous night still load fine; `--no-data` flag for offline retry |
| `autoloop_nf.py` (midnight) | Claude API timeout, paper score regression | Promotion streak resets (3-of-5 wins gate); paper model never auto-deploys without 3 nights confirmed |
| `lot_expiry_scanner.py` (monthly 1st) | NSE API change | Telegram alert if format unrecognized; LOT_SIZE override file untouched; manual `--show` then edit |
| `regime_watcher.py` (monthly 2nd) | Backtest crash | No auto-patch; Telegram error; live system continues with current strategy |
| `weekly_audit.py` (Sat 7:30 AM) | Itself fails | Telegram silence is the warning — if no Saturday "all clean" message, manually run |
| `system_health.py` (daily 7 AM) | CSV / champion_meta read error | Renders `—` for missing fields; non-blocking |
| `git pull` cron (twice daily) | Network blip | Autostash preserves local data/; manual pull fixes |

### Function-level fallbacks

| Function | Primary path | Fallback path |
|---|---|---|
| `get_capital()` | Dhan `/v2/fundlimit` (`availabelBalance`) | env var `INITIAL_CAPITAL`; if both fail → die with Telegram |
| `get_expiry()` | Dhan `/v2/optionchain/expirylist` | last-Tuesday calc (warns: "API failed, using fallback expiry") |
| `_fetch_ic_margin_per_lot()` | Dhan `/v2/margincalculator/multi` | `IC_MARGIN_PER_LOT = 100_000` constant (FALLBACK only) |
| `_fetch_spread_margin_per_lot()` | Dhan `/v2/margincalculator/multi` | `BULL_PUT_MARGIN_PER_LOT = 55_000`; if Dhan returns ₹220K (unhedged-equivalent) → routes to IC fallback automatically |
| `ml_engine.py --predict-today` | Load champion.pkl + ensemble, vote | If pickle missing → retrain RF from scratch (slow path, 30s) |
| Live trade journal P&L | `dhan_journal.realized_pnl()` from `/v2/positions` | `today_trade.json` `pnl_inr` field (auxiliary) |
| Historical row recovery | `backfill_dhan_history.py --date X --apply` | Manual edit (only if Dhan API unreachable; document in commit) |

### Self-healing flow on any 3:30 PM journal miss

```
3:30 PM trade_journal.py — fails (crash / race / network)
   ↓
[no recovery same day — this is OK]
   ↓
3:30 PM next day — runs successfully for new trade
   ↓
Saturday 7:30 AM — weekly_audit.py:
   - Walks last week Mon–Fri
   - For each day: Dhan tradebook says trades happened?
     - Yes + CSV row OK → ✅ clean
     - Yes + CSV row missing/OPEN → run backfill_dhan_history.py
     - No + no row → ⏭ no-trade day
   - Telegram report
```

### What you cannot recover automatically

- **Pre-2 weeks ago trades** — Dhan `/v2/trades/{from}/{to}/{page}` works on any range but pagination and rate-limits make bulk historical recovery slow (>30 days). Use `python3 backfill_dhan_history.py --range FROM:TO` once and let it churn.
- **Trades placed manually outside the system** — `weekly_audit.py` will see the fills and try to detect strategy from leg fingerprint, but if the legs don't match IC/spread/straddle templates, it flags as `could-not-detect-strategy` and asks for manual classification.
- **Token expiry mid-day** — `renew_token.py` runs twice daily but if it's down for >24 h, all crons silently fail. `system_health.py` 7 AM will show stale data → manual investigation.

---

## Common Commands

```bash
# VM setup (first time)
bash setup_automation.sh

# Data refresh + signal
python3 data_fetcher.py
python3 signal_engine.py

# ML
python3 ml_engine.py                  # full walk-forward (~2 min)
python3 ml_engine.py --predict-today  # fast single prediction (<10 sec)
python3 ml_engine.py --analyze        # feature importance report

# Backtest
python3 backtest_engine.py                   # rule-based backtest (uses real premiums if available)
python3 backtest_engine.py --real-premium    # explicitly real-premium backtest (rule signals)
python3 backtest_engine.py --real-premium-ml # real-premium ML backtest
python3 backtest_engine.py --ml              # ML backtest (formula premium)
python3 backtest_live_context.py             # research: intraday live-context rules

# Fetch historical ATM option premiums + IV skew + OI surface (one-time, then incremental)
python3 data_fetcher.py --fetch-options   # options_atm_daily.csv + options_iv_skew.csv + options_oi_surface.csv

# Fetch historical NF 9:15-9:30 opening range candles (one-time, ~2 min for 5y)
python3 data_fetcher.py --fetch-intraday  # nifty50_15m_orb.csv (90-day chunks via Dhan intraday)

# Live test
python3 auto_trader.py --dry-run

# Nightly evolver (manually)
python3 model_evolver.py                 # full run (data + HPO + train + telegram)
python3 model_evolver.py --no-data       # skip data refresh
python3 model_evolver.py --trials 30     # override trial count per model

# Post-mortem replay
python3 replay_today.py                  # rerun today's prediction with current ensemble

# Pre-market / mid-day
python3 health_ping.py                   # manual 8:50 AM checks
python3 midday_conviction.py --dry-run   # midday thesis check, no Telegram

# Lot/expiry scanner
python3 lot_expiry_scanner.py --show   # print current override state
python3 lot_expiry_scanner.py          # run scan + Telegram alert if change

# Regime watcher (autonomous — runs on 2nd of month via cron)
python3 regime_watcher.py --show       # print current regime state
python3 regime_watcher.py --dry-run    # run analysis, no writes, no Telegram
python3 regime_watcher.py --force      # run full backtest even if no change detected

# Autoresearch
python3 autoexperiment_nf.py                 # baseline composite score (JSON output)
python3 autoexperiment_nf.py --module ml_engine_paper  # evaluate paper model
python3 autoloop_nf.py --dry-run             # test loop without calling Claude API
python3 autoloop_nf.py --experiments 3       # run 3 live experiments
python3 autoloop_nf.py                       # full 5-experiment nightly run

# VIX trade filter
python3 analyze_confidence.py                # diagnostic: confidence + VIX regime + importances
python3 analyze_confidence.py --write-threshold  # recompute + save dynamic VIX threshold
```

---

## ML Feature Set (ml_engine.py FEATURE_COLS — 64 features as of Apr 2026)

Live count grows over time (autoresearch may add features). Verify any time:
```python
python3 -c "from ml_engine import FEATURE_COLS; print(len(FEATURE_COLS))"
```

Major groups (representative, not exhaustive — read FEATURE_COLS in ml_engine.py for complete list):

| Group | Sample features | What they capture |
|---|---|---|
| Rule signals | `s_ema20`, `s_trend5`, `s_vix`, `s_nf_gap` | Discrete ±1 rule outputs |
| Continuous signals | `ema20_pct`, `trend5`, `vix_dir` | Raw magnitudes behind the rules |
| Technical | `rsi14`, `hv20`, `nf_gap`, `adx14` | Momentum, volatility, opening gap, trend strength |
| Global markets | `sp500_chg`, `nikkei_chg`, `spf_gap` | Overnight global risk sentiment |
| Macro / FII drivers | `crude_ret`, `dxy_ret`, `us10y_chg`, `usdinr_ret` | Inflation, dollar, yield, rupee |
| Volatility regime | `vix_level`, `vix_pct_chg`, `vix_hv_ratio`, `vix_pct_rank_252` | Fear level, realized vol ratio, percentile rank |
| Momentum & drawdown | `nf_ret1`, `nf_ret20`, `nf_dist_high20`, `nf_dist_high52` | Short/medium trend, drawdowns, 52-week high distance |
| Calendar | `dow`, `dte` | Day-of-week, days to expiry |
| Options sentiment | `pcr_ma5`, `put_call_skew`, `iv_proxy` | Put/call ratio, premium bias, IV level |
| IV skew | `call_skew`, `put_skew`, `skew_spread`, `skew_chg` | OTM skew dynamics, fear momentum |
| OI surface | `oi_pcr_wide`, `oi_imbalance_atm`, `call_wall_offset`, `put_wall_offset` | OI across ATM±3 strikes, max-pain distance |
| Opening range | `orb_range_pct`, `orb_break_side` | 9:15-9:30 candle range + breakout direction |
| Breadth / flow | `fii_net_cash_z`, `bankbees_ret1`, `bank_breadth_d1` | FII activity, bank ETF, top-5 constituent breadth |
| Opening signal | `vix_open_chg` | VIX gap at 9:15 AM (risk-on/off at entry) |
| Interaction | `gap_mom_align`, `iv_spf_interaction`, `adx_gap_interact` + others | Conditional combinations (autoresearch-added) |

The autoresearch loop (`autoloop_nf.py`) proposes additions/removals to this list and validates each on the 252-day holdout before committing.

---

## What to Read for Common Tasks

| Task | Files to read |
|---|---|
| Debug a morning trade failure | `auto_trader.py` + `logs/auto_trader.log` |
| Change SL % or RR | `auto_trader.py` constants (top of file) |
| Add a new indicator | `signal_engine.py` `score_row()` + `compute_indicators()` |
| Understand ML features | `ml_engine.py` `FEATURE_COLS` + `compute_features()` — see ML Feature Set below |
| Run/debug autoresearch | `autoloop_nf.py` + `autoexperiment_nf.py` + `ml_engine_paper.py` |
| Change lot size | `backtest_engine.py` `get_lot_size()` + `auto_trader.py` `LOT_SIZE` |
| Run new backtest | `backtest_engine.py` — standalone, reads `data/signals.csv` |
| Add data source | `data_fetcher.py` + `model_evolver.py` feature list |
| Change HPO trials | `model_evolver.py` top — `N_TRIALS = 30` |
| Add a new model | `model_evolver.py` `_build_model()` + competition loop |
| Check live P&L | Dhan app → Positions, or read `data/live_ic_trades.csv` |

---

## Dhan API Notes

- **Token**: expires every 24h; auto-renewed by `renew_token.py` twice daily (7:55 AM + 11 PM IST) + @reboot safety net. `.env` is rewritten in place.
- **DH-906**: "Market closed" OR "weekend AMO block". Not an account issue.
- **AMO window**: Mon–Fri after 3:30 PM IST. Weekends reject all `afterMarketOrder: true`.
- **Super Order**: `/v2/super/orders` — single call for entry + SL + TP.
- **Nifty50 scrip**: `UnderlyingScrip: 13`, `UnderlyingSeg: "IDX_I"`.
- **Rate limit**: Data API = 10 req/s. Long fetches (historical PCR) pace at 2 req/s for 5× headroom.
