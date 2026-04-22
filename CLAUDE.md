# CLAUDE.md — Nifty50 Iron Condor Auto-Trader Architecture Map

Quick reference for Claude Code. Read this before touching any file.

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
- **Mon + Tue** → Nifty Iron Condor (4 legs, BUY wings first)
- **Thu + Fri, CALL signal** → Bear Call Credit Spread (2 legs)
- **Thu + Fri, PUT signal** → Bull Put Credit Spread (2 legs)
- **Wed** → No trade (DTE 6, all strategies lose after costs)
- **Auto-upgrade**: if balance ≥ ₹2.3L → Short Straddle replaces all above (except Wed)

**Capital at go-live (22 Apr 2026): ₹1,12,370**
- IC: ₹93,202/lot → 1 lot ✅  Bear Call/Bull Put: ₹51K/lot → 2 lots ✅
- Straddle: ₹2,16-26L/lot → 0 lots ❌ (insufficient — upgrade pending capital growth)

**Why these strategies:**
- IC (Mon/Tue): WR 84.6% (2021–2026), ₹25L/yr, max DD -0.8%. Theta dominates DTE 0-1.
- Bear Call (Thu/Fri CALL): +₹65-81/lot net — IC loses on DTE 4-5 but directional credit wins.
- Bull Put (Thu/Fri PUT): small positive P&L — mirrors Bear Call on PUT signal days.
- Straddle (when affordable): both sides collected, no wings, higher margin but more credit.

**What happens each morning (9:30 AM IST):**
- Data → signal → ML → strategy routing → real MARKET orders via Dhan → Telegram alert
- `spread_monitor.py` watches every minute for SL; `exit_positions.py` squares off at 3:15 PM

**NF lot size: 75 before Jan 6 2026, 65 from Jan 6 2026**

**Go-live checklist (completed April 2026):**
1. ✅ Built `fetch_intraday_options.py --instrument NF --spreads` (NF multi-leg cache)
2. ✅ Built `backtest_spreads.py` with `--instrument NF` + hybrid backtest + cost model
3. ✅ Fetched full NF option cache (5590 files, Aug 2021–Apr 2026)
4. ✅ Confirmed NF IC: WR 84.6%, ₹1.17Cr (5yr), ₹25L/yr, max DD -0.8%
5. ✅ Wired multi-strategy into `auto_trader.py` (IC + Bear Call + Bull Put + straddle upgrade)
6. ✅ PAPER_MODE=False, MAX_LOTS=10 — live from 22 Apr 2026

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

### NF IC Structure
```
SELL ATM CE  + BUY ATM+150 CE   (Bear Call side: upper wing)
SELL ATM PE  + BUY ATM-150 PE   (Bull Put side: lower wing)
spread_width = 150pts (NF strike spacing = 50pts, ATM±3 strikes)
net_credit   ≈ ₹108/lot average
SL: loss = 50% of credit received
TP: retain 65% of credit (spread decays to 35%)
max_lots = 10 (IC ties up margin on both sides)
Trades CALL and PUT signal days — 235 trades/year (weekly expiry)
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

### Optimizer verdict (NF, Bear Call + Bull Put credit, combined)

No VIX/confidence filter = highest P&L (₹17.55L/yr, 1114 trades) but NF IC standalone
is better (₹25L/yr, 1114 trades) because IC collects both sides simultaneously.
**Do not filter NF IC by VIX or confidence — no filter gives maximum P&L.**

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
6. Run `python3 autoexperiment_nf.py` — **keep only if composite >= 0.5358** (current NF baseline, post BN→NF migration)
7. Commit + push to `main`

---

## ⚠️ PLAIN ENGLISH RULE — ALL USER-FACING OUTPUT

**Every message to the user must pass this test: would a non-programmer understand it?**

- Numbers need context: "composite score 0.617 — that's 10 points above our 0.515 starting point and above the 0.60 target"
- Verdicts not metrics: "the model improved" not "accuracy delta +1.1pp"
- Error summaries in one sentence: "A variable name conflict in the code caused a text string to end up where a price number was expected" — not the raw Python traceback
- Status reports in plain terms: "Today's signal is CALL — model says markets will go up" not "model output: P(CALL)=0.62"

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
| TP frac mismatch: live 0.90 vs backtest 0.65 | `CREDIT_TP_FRAC = 0.90` in live code while backtest used 0.65 | Always verify TP/SL fracs match between `backtest_spreads.py` strategy dict and `auto_trader.py` / `spread_monitor.py` constants. Current correct value: 0.65 (retain 65% of credit). |
| Wed/Thu/Fri IC all net-negative; only Bear Call/Bull Put profitable on Thu/Fri | IC: Wed -₹233, Thu -₹258, Fri -₹39 per lot after costs. Bear Call: Thu +₹65/lot, Fri +₹81/lot (CALL days). Bull Put: small positive P&L on Thu/Fri PUT days. | `IC_SKIP_DAYS = {2}` (Wed only). `BEAR_CALL_DAYS = {3,4}`. Thu/Fri CALL → Bear Call. Thu/Fri PUT → Bull Put. |
| IC TP was costing ₹21L over 5 years | Standard IC (TP=0.65): ₹1.17Cr. EOD-only IC (no TP): ₹1.38Cr. Same WR, lower drawdown. TP captured 65% of credit but left last 35% of theta. | `spread_monitor.py` IC path no longer checks TP — SL only. IC always exits at EOD 3:15 PM via `exit_positions.py`. |
| Straddle auto-upgrade not firing | `capital >= STRADDLE_MARGIN_PER_LOT` check uses constant ₹2.3L; actual Dhan margin may differ slightly | Verify with `python3 check_margins.py` — if live margin is higher than constant, update `STRADDLE_MARGIN_PER_LOT` in `auto_trader.py`. |
| Straddle `today_trade.json` schema differs from IC | Straddle uses `ce_sid`/`pe_sid`/`ce_entry`/`pe_entry` (no `short_sid`/`long_sid`). IC uses `ce_short_sid`/`ce_long_sid`/`pe_short_sid`/`pe_long_sid`. 2-leg uses `short_sid`/`long_sid`. | Match the strategy key: `nf_short_straddle` → straddle schema; `nf_iron_condor` → IC schema; `bear_call_credit`/`bull_put_credit` → 2-leg schema. |

---

## What This System Does

Fully automated Nifty50 multi-strategy options trading. Cron fires at 9:30 AM IST on trading days:
data → rule signal → ML override → strategy router → orders via Dhan → Telegram alert.

Strategy by day: Mon/Tue = IC (4-leg), Thu/Fri = Bear Call or Bull Put (2-leg, signal-dependent),
Wed = skip. Auto-upgrades to Short Straddle when capital ≥ ₹2.3L. No human input needed.

---

## File Index (one line each)

| File | Purpose |
|---|---|
| `auto_trader.py` | Morning runner (9:30 AM) — orchestrates all steps, picks credit spread legs, places both orders via Dhan |
| `spread_monitor.py` | Intraday cron (every 1 min, 9:30 AM–3:29 PM) — watches spread cost, exits both legs on SL/TP |
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
| `exit_positions.py` | EOD 3:15 PM — squares off open Nifty F&O positions (long AND short legs for spreads) |
| `trade_journal.py` | EOD 3:30 PM — logs actual fills; spreads → `live_ic_trades.csv` |
| `lot_expiry_scanner.py` | Monthly cron — detects Nifty50 lot size / expiry day changes |
| `replay_today.py` | Post-mortem tool — ensemble replay of today after evolver |
| `renew_token.py` | Every-5-min token renewer (23h50m interval) |
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
IC_MARGIN_PER_LOT = 100_000      # FALLBACK only; live call goes to /margincalculator/multi
                                  # Actual sizing: lots = floor(capital / Dhan_API_margin), max MAX_LOTS
                                  # All 4 legs placed at same lot count (equal quantity each)

# Credit-spread params (active path)
SPREAD_WIDTH     = 150           # NF: 50pt strike spacing × 3 = ATM±150 (BNF was 300)
CREDIT_SL_FRAC   = 0.5           # SL when spread cost grows to net_credit × 1.5
CREDIT_TP_FRAC   = 0.65          # TP when spread cost falls to net_credit × 0.35

# Filters
ML_CONF_THRESHOLD = 0.55         # skip trade when ML ensemble confidence below this
VIX_MIN_TRADE     = 13.0         # dynamic — analyze_confidence.py --write-threshold updates nightly
VIX_MAX_TRADE     = 20.0         # ceiling — panic regime above this
IC_SKIP_DAYS         = {2}       # Wed only — all strategies net-negative on DTE 6
BEAR_CALL_DAYS       = {3, 4}    # Thu (DTE=5) + Fri (DTE=4)
                                  # CALL signal → Bear Call (+₹65/lot Thu, +₹81/lot Fri)
                                  # PUT signal → Bull Put (small positive P&L on Thu/Fri)
                                  # IC days: Mon (DTE=1, 97%WR) + Tue (DTE=0, 100%WR)
STRADDLE_MARGIN_PER_LOT = 217_000 # auto-upgrade threshold; actual Dhan SPAN ≈₹2,16,492 + ₹508 buffer
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
6. IC_SKIP_DAYS check (Wed=2)           — no trade Wednesday; all strategies lose on DTE 6
7. get_todays_signal()                  — reads signals_ml.csv (falls back to signals.csv)
   ├── NONE / score=0 / event_day → Telegram "No Trade" → exit
   └── CALL/PUT → continue
8. Day-of-week routing (IC mode only):
   ├── BEAR_CALL_DAYS {3,4} + CALL → _use_bear_call_today = True
   ├── BEAR_CALL_DAYS {3,4} + PUT  → _use_bull_put_today  = True
   └── Mon/Tue                     → IC path (neither flag set)
9. VIX + ML filters (bypassed for IC/spreads — no filter = max P&L per backtest)
10. get_capital()                       — Dhan fundlimit API (availabelBalance field)
    Straddle auto-upgrade check:
    _use_straddle_today = (capital >= STRADDLE_MARGIN_PER_LOT)
    If True → overrides bear_call/bull_put/IC for all days (logs upgrade notice)
11. get_expiry()                        — Dhan expirylist API (falls back to last-Tuesday calc)

── STRADDLE PATH (_use_straddle_today = True, capital ≥ ₹2.3L) ─────────────
12s. get_straddle_legs(expiry, capital) — ATM CE + ATM PE SIDs from OC
                                          _fetch_straddle_margin_per_lot() → actual SPAN margin
                                          lots = min(MAX_LOTS_STRADDLE, capital // margin_1lot)
13s. Telegram: straddle details         — both SELLs, net credit, SL trigger
14s. _check_no_existing_position()
15s. place_straddle()                   — SELL CE, wait 2s, SELL PE
     └── 🚨 PARTIAL_STRADDLE if PE SELL fails after CE placed
16s. _write_today_straddle_trade()      — today_trade.json: strategy=nf_short_straddle
                                          fields: ce_sid/pe_sid/ce_entry/pe_entry/net_credit

── BEAR CALL PATH (_use_bear_call_today = True, Thu/Fri CALL) ───────────────
12b. get_spread_legs("CALL", expiry, capital) — SELL ATM CE + BUY ATM+150 CE
13b. Telegram: bear call details
14b. _check_no_existing_position()
15b. place_credit_spread()              — BUY long (hedge) first, SELL short
16b. _write_today_spread_trade()        — strategy=bear_call_credit

── BULL PUT PATH (_use_bull_put_today = True, Thu/Fri PUT) ──────────────────
12p. get_spread_legs("PUT", expiry, capital)  — SELL ATM PE + BUY ATM-150 PE
13p. Telegram: bull put details
14p. _check_no_existing_position()
15p. place_credit_spread()              — BUY long (hedge) first, SELL short
16p. _write_today_spread_trade()        — strategy=bull_put_credit

── IC PATH (IRON_CONDOR_MODE, Mon/Tue) ──────────────────────────────────────
12i. get_ic_legs(expiry, capital)       — all 4 legs from OC; _fetch_ic_margin_per_lot()
                                          lots = min(MAX_LOTS, capital // api_margin)
13i. compute_chain_signals()            — max-pain + GEX (informational)
14i. Telegram: IC details               — 4 legs, net credit, SL trigger
15i. _check_no_existing_position() via today_trade.json (IC duplicate guard)
16i. place_iron_condor(ic, expiry)      — BUY CE long → SELL CE short → BUY PE long → SELL PE short
     └── 🚨 PARTIAL_IC* alerts if any leg fails mid-sequence
17i. _write_today_ic_trade()            — today_trade.json: strategy=nf_iron_condor
                                          fields: ce/pe_short_sid/long_sid, ce/pe_credit, net_credit

── LEGACY FALLBACK (CREDIT_SPREAD_MODE = False) ─────────────────────────────
  Naked option BUY via place_super_order() — kept for reference, never used in IC mode
```

---

## 11 PM Evolver Flow (model_evolver.py)

```
1. Fetch all data sources (Dhan + yfinance + NSE FII + PCR)
2. compute_features() from ml_engine — 63 features across technicals, macro, flow, options, IV skew, OI surface, ORB, breadth
3. Feature selection via RF importance (keep > 1%)
4. Optuna HPO: 30 trials × RF + XGB + LGB + CAT = 120 trials (~8-12 min)
5. Champion = best on 252-day temporal holdout (accuracy + recall blend)
6. Retrain champion on full data; train full 4-model ensemble
7. Save: models/champion.pkl + models/champion_meta.json + models/ensemble/*.pkl
8. Predict tomorrow using ensemble vote (falls back to most recent trading day if today's row missing)
9. Telegram: evolver report (plain-language summary)
```

Live feedback: the evolver reads `data/live_trades.csv` and injects real outcomes with 10× weight; historical rows matching miss-day patterns get 3× weight boost.

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
*/5 *  * * *    renew_token.py          # every 5 min, all 7 days
35 3   * * 1-5  health_ping.py          # 9:05 AM IST
45 3   * * 1-5  morning_brief.py        # 9:15 AM IST (news sentiment → data/news_sentiment.json)
0  4   * * 1-5  auto_trader.py          # 9:30 AM IST
30 5   * * 1-5  midday_conviction.py    # 11:00 AM IST
45 9   * * 1-5  exit_positions.py       # 3:15 PM IST
0  10  * * 1-5  trade_journal.py        # 3:30 PM IST
30 17  * * 1-5  model_evolver.py        # 11:00 PM IST
30 18  * * 1-5  autoloop_nf.py          # Mon–Fri midnight IST (autoresearch, after evolver)
30 4   1 * *    lot_expiry_scanner.py   # 1st of month, 10:00 AM IST
30 20  * * 0    log rotation            # Sunday 2:00 AM IST (trim logs > 10 MB)
```

(Times in UTC cron; comments show the IST equivalent.)

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

## ML Feature Set (ml_engine.py FEATURE_COLS — 63 features)

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

- **Token**: expires every 24h; auto-renewed by `renew_token.py` every 5 min at T+23h50m. `.env` is rewritten in place.
- **DH-906**: "Market closed" OR "weekend AMO block". Not an account issue.
- **AMO window**: Mon–Fri after 3:30 PM IST. Weekends reject all `afterMarketOrder: true`.
- **Super Order**: `/v2/super/orders` — single call for entry + SL + TP.
- **Nifty50 scrip**: `UnderlyingScrip: 13`, `UnderlyingSeg: "IDX_I"`.
- **Rate limit**: Data API = 10 req/s. Long fetches (historical PCR) pace at 2 req/s for 5× headroom.
