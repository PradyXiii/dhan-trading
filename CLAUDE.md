# CLAUDE.md — BankNifty Auto-Trader Architecture Map

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

## 🛑 PAPER MODE — LIVE TRADING DISABLED (April 2026)

**`auto_trader.py` is currently in PAPER_MODE = True.** No real orders will be placed.
`MAX_LOTS = 1` (was 20) during rebuild.

**Why:** Real 1-min option backtest + 4-trade live sample (Apr 13–17, 2026) proved:
- Naked long-options buying is structurally losing — theta decay + IV crush
- 2025–26 real-data backtest: WR 17%, -₹1,006/trade per lot
- Core problem is the strategy (buy naked ATM options), not the signal

**What happens now each morning (9:30 AM IST):**
- Full signal flow runs (data → rule → ML → option pick)
- "Would have placed" trade logged to `data/paper_trades.csv`
- Telegram message fires with `[PAPER]` prefix + clear plain-English "no real order"
- No Dhan API call for order placement
- No exit-marker check, no duplicate-position guard (not needed — nothing live)

**Next steps before flipping back to LIVE:**
1. ✅ Built `fetch_intraday_options.py --spreads` (fetches ATM+3 CE / ATM-3 PE / straddle legs)
2. ✅ Built `backtest_spreads.py` — multi-leg spread simulator with regime router
3. Fetch full OTM cache on VM: `python3 fetch_intraday_options.py --spreads --start 2021-08-01` (~30 min)
4. Run comparative backtest across 3 strategies + adaptive router
5. Paper-trade the winning strategy for 30+ days — only flip PAPER_MODE when
   `data/paper_trades.csv` shows >50 days net-positive P&L

**To re-enable LIVE trading** (only after strategy is fixed):
```python
# In auto_trader.py — near top of file:
PAPER_MODE = False   # WAS True during Apr 2026 rebuild
MAX_LOTS   = 20      # WAS 1 during rebuild
```

Then: re-read this section, read `data/paper_trades.csv` last 30 days, confirm
positive P&L, commit with clear message, restart cron.

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

## 🎯 ADAPTIVE MULTI-STRATEGY SYSTEM (April 2026 — design phase)

**The system must dynamically choose strategy per day based on market regime.**
No single strategy works across all years — directional spreads fail in neutral
regimes, straddles fail in low-vol regimes, etc.

### Strategy registry (`backtest_spreads.py` → `STRATEGIES` dict)

| Strategy | Direction | Regime (signal + VIX) | Legs | Max loss | Max profit |
|---|---|---|---|---|---|
| Bull Call Spread | BULLISH | CALL + VIX∈[10,20] | BUY ATM CE + SELL ATM+3 CE | net debit | (300 − debit) |
| Bear Put Spread  | BEARISH | PUT  + VIX∈[10,20] | BUY ATM PE + SELL ATM-3 PE | net debit | (300 − debit) |
| Long Straddle    | VOLATILE | any signal + VIX≥20 | BUY ATM CE + BUY ATM PE | sum of debits | unlimited |
| Iron Condor (TBD) | NEUTRAL | no signal + VIX∈[12,18] | 4-leg credit spread | spread width − credit | net credit |

### Adaptive router (`_route_strategy()` in `backtest_spreads.py`)
```
VIX ≥ 20 + directional signal  →  Long Straddle
CALL signal + VIX∈[10,20)       →  Bull Call Spread
PUT  signal + VIX∈[10,20)       →  Bear Put Spread
else                            →  skip day (no valid regime match)
```

### ⚠️ MANDATORY LIVE PLACEMENT ORDER: BUY FIRST, THEN SELL

Dhan hedge margin rules require the LONG leg on the books BEFORE the SHORT leg
gets margin benefit. In `auto_trader.py` when spreads go live:

1. Place BUY order (long leg) — wait for fill confirmation
2. Then place SELL order (short leg) — margin benefit applies
3. Never reverse the order — short-first triggers full margin for the sell, defeating spread economics

### Cache file naming (for multi-leg backtests)

| File | Contents |
|---|---|
| `{date}_CE.csv` | ATM CE (backward-compat, exists from Apr 2026 fetch) |
| `{date}_PE.csv` | ATM PE (backward-compat) |
| `{date}_CE_p3.csv` | ATM+3 CE (Bull Call Spread short leg, CALL days only) |
| `{date}_PE_m3.csv` | ATM-3 PE (Bear Put Spread short leg, PUT days only) |
| `{date}_PE_straddle.csv` | ATM PE on CALL signal days (Long Straddle) |
| `{date}_CE_straddle.csv` | ATM CE on PUT signal days (Long Straddle) |

Fetch them all (one-time, ~30 min for full 5y): `python3 fetch_intraday_options.py --spreads --start 2021-08-01`

### Common commands

```bash
python3 backtest_spreads.py                              # run all 3 strategies
python3 backtest_spreads.py --strategy bull_call_spread  # single strategy
python3 backtest_spreads.py --adaptive                   # regime router
python3 backtest_spreads.py --ml                         # use signals_ml.csv
python3 backtest_spreads.py --save data/spread_trades.csv
```

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
6. Run `python3 autoexperiment_bn.py` — **keep only if composite >= 0.6175** (current best)
7. Commit + push to `claude/banknifty-options-backtest-JoxCW`

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

---

## What This System Does

Fully automated BankNifty options trading. Cron fires at 9:30 AM IST on trading days:
data → rule signal → ML override → Dhan Super Order + SL/TP → Telegram alert.
No human input needed during market hours.

---

## File Index (one line each)

| File | Purpose |
|---|---|
| `auto_trader.py` | Morning runner — orchestrates all steps, places Dhan order |
| `signal_engine.py` | Rule-based signal scorer (4 active indicators) → `data/signals.csv` |
| `ml_engine.py` | Walk-forward training → `data/signals_ml.csv`; fast predict via champion.pkl + ensemble |
| `model_evolver.py` | Nightly 11 PM — Optuna HPO (RF/XGB/LGB/CAT) → `models/champion.pkl` + ensemble |
| `backtest_engine.py` | Historical P&L simulation with cost model + lot-size timeline |
| `backtest_live_context.py` | Research tool — tests intraday live-context override rules |
| `data_fetcher.py` | Downloads OHLCV + global market data → `data/*.csv` |
| `health_ping.py` | Pre-market heartbeat (8:50 AM) — token/capital/freshness checks |
| `midday_conviction.py` | Midday thesis reassessment (11 AM) → Telegram summary |
| `exit_positions.py` | EOD 3:15 PM — closes open NRML positions |
| `trade_journal.py` | EOD 3:30 PM — logs actual fills vs oracle to `live_trades.csv` |
| `lot_expiry_scanner.py` | Monthly cron — detects BankNifty lot size / expiry day changes |
| `replay_today.py` | Post-mortem tool — ensemble replay of today after evolver |
| `renew_token.py` | Every-5-min token renewer (23h50m interval) |
| `notify.py` | Telegram send/log helper (2 functions) |
| `autoloop_bn.py` | Daily midnight autoresearch — paper-trades ML changes, auto-promotes after 3 nights of outperformance |
| `ml_engine_paper.py` | Paper copy of ml_engine.py — autoresearcher tests here first before promoting to live |
| `autoexperiment_bn.py` | Fast 252-day holdout evaluator; `--module ml_engine_paper` to eval paper model |
| `autoexperiment_backtest.py` | Backtest evaluator for auto_trader.py constant changes |
| `backfill_live_trades.py` | One-time / periodic utility — imports Dhan trade history into live_trades.csv |
| `analyze_confidence.py` | Diagnostic tool — confidence buckets, VIX regime accuracy, feature importances; `--write-threshold` updates dynamic VIX filter |
| `morning_brief.py` | 9:15 AM news sentiment — fetches BankNifty headlines, calls Claude API, writes `data/news_sentiment.json` for auto_trader vote |
| `research_program_bn.md` | Autoresearch brief — defines what the AI agent may and may not change |
| `setup_automation.sh` | One-shot VM setup: pip deps, cron install, dry-run verification |

---

## Key Constants (auto_trader.py)

```python
LOT_SIZE     = 30        # BankNifty lot size (Jan 2026+ — was 35 Jun–Dec 2025, was 15 pre-Nov 2024)
SL_PCT       = 0.15      # 15% stop-loss on premium
RISK_PCT     = 0.05      # 5% of capital at risk per trade
MAX_LOTS     = 20        # hard cap on position size
PREMIUM_K    = 0.004     # approx premium factor: BN_open × PREMIUM_K × sqrt(DTE)
ITM_WALK_MAX = 2         # max 200pt ITM probe when capital is flush
RR           = 2.5       # reward:risk (SL=15% → TP=37.5%) — grid-optimised
```

---

## 9:30 AM Flow (auto_trader.py main)

```
0. _acquire_lock()  — fcntl prevents double cron execution
1. check_credentials()  — Dhan token valid? API reachable?
2. refresh_data_and_signal()  — subprocess: data_fetcher → signal_engine → ml_engine --predict-today
3. get_todays_signal()  — reads signals_ml.csv (falls back to signals.csv)
   ├── NONE → Telegram "No Trade Today" → exit
   └── CALL/PUT → continue
4. get_capital()  — Dhan fundlimit API
5. get_expiry()  — Dhan expirylist API (falls back to last-Tuesday calc)
6. get_affordable_option()  — live option chain, walks ATM→OTM→ITM, finds best strike
7. Telegram: trade details message
8. place_super_order()  — Super Order (entry+SL+TP in one call)
   ├── DH-906 market closed → AMO fallback (afterMarketOrder+amoTime=OPEN)
   ├── Super Order fails → manual BUY + SL-M
   │   ├── SL fails → 🚨 CRITICAL Telegram (FALLBACK_NO_SL mode)
   │   └── BUY fails → ❌ FAILED mode
   └── Success → Telegram: order confirmed
```

---

## 11 PM Evolver Flow (model_evolver.py)

```
1. Fetch all data sources (Dhan + yfinance + NSE FII + PCR)
2. compute_features() from ml_engine — 31 features across technicals, macro, flow, options
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
chain = POST /v2/optionchain {UnderlyingScrip: 25, UnderlyingSeg: "IDX_I", Expiry: "YYYY-MM-DD"}
inner = chain["data"]                 # dict with last_price + oc (no intermediate key)
spot  = inner["last_price"]           # spot index price
oc    = inner["oc"]                   # strike → {ce: {...}, pe: {...}}
sid   = oc["55900.000000"]["ce"]["security_id"]   # float-string keys
iv    = oc["55900.000000"]["ce"]["implied_volatility"]  # ATM IV (%)

# Always fetch expirylist first:
expiries = POST /v2/optionchain/expirylist {UnderlyingScrip: 25, UnderlyingSeg: "IDX_I"}
expiry_str = expiries["data"][0]      # nearest valid expiry
```

---

## Lot Size Timeline (backtest_engine.py get_lot_size)

| Period | Lot size |
|---|---|
| Before Nov 2024 | 15 |
| Nov 2024 – May 2025 | 30 |
| Jun 2025 – Dec 2025 | 35 |
| Jan 2026+ | 30 |

Live overrides stored in `data/lot_size_overrides.json` (written by `lot_expiry_scanner.py`).

---

## BankNifty Phases (expiry schedule)

| Phase | Period | Expiry day |
|---|---|---|
| Phase 1–3 | Before Sep 2025 | Weekly Thursday |
| Phase 4 | Sep 2025+ | Monthly last Tuesday |

Phase 4 means all 5 weekdays are valid trade days (no weekly expiry on Wednesday).

---

## Data Files (all gitignored — GCP VM only)

| File | Contents |
|---|---|
| `data/banknifty.csv` | Daily OHLCV from Dhan |
| `data/nifty50.csv` | Daily close from Dhan |
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
| `data/banknifty_15m_orb.csv` | BN 9:15-9:30 opening range candles (date, orb_open/high/low/close) for ORB features |
| `data/bankbees.csv` + `hdfcbank.csv` + `icicibank.csv` + `kotakbank.csv` + `sbin.csv` + `axisbank.csv` | Bank ETF + top-5 BN constituents (yfinance daily OHLCV) for breadth + flow features |
| `data/live_trades.csv` | Daily live-trade outcomes (written by trade_journal.py + backfill_live_trades.py) |
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
30 18  * * 1-5  autoloop_bn.py          # Mon–Fri midnight IST (autoresearch, after evolver)
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

# Fetch historical BN 9:15-9:30 opening range candles (one-time, ~2 min for 5y)
python3 data_fetcher.py --fetch-intraday  # banknifty_15m_orb.csv (90-day chunks via Dhan intraday)

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
python3 autoexperiment_bn.py                 # baseline composite score (JSON output)
python3 autoexperiment_bn.py --module ml_engine_paper  # evaluate paper model
python3 autoloop_bn.py --dry-run             # test loop without calling Claude API
python3 autoloop_bn.py --experiments 3       # run 3 live experiments
python3 autoloop_bn.py                       # full 5-experiment nightly run

# VIX trade filter
python3 analyze_confidence.py                # diagnostic: confidence + VIX regime + importances
python3 analyze_confidence.py --write-threshold  # recompute + save dynamic VIX threshold
```

---

## ML Feature Set (ml_engine.py FEATURE_COLS — 31 features)

| Group | Features | What they capture |
|---|---|---|
| Rule signals | `s_ema20`, `s_trend5`, `s_vix`, `s_bn_nf_div` | Discrete ±1 rule outputs |
| Continuous signals | `ema20_pct`, `trend5`, `vix_dir`, `bn_nf_div` | Raw magnitudes behind the rules |
| Technical | `rsi14`, `hv20`, `bn_gap` | Momentum, volatility, opening gap |
| Global markets | `sp500_chg`, `nikkei_chg`, `spf_gap` | Overnight global risk sentiment |
| Macro / FII drivers | `crude_ret`, `dxy_ret`, `us10y_chg`, `usdinr_ret` | Inflation, dollar strength, yield, rupee |
| Volatility regime | `vix_level`, `vix_pct_chg`, `vix_hv_ratio` | Fear level and realized vol ratio |
| Momentum & drawdown | `bn_ret1`, `bn_ret20`, `bn_dist_high20` | Short/medium trend + distance from recent high |
| Calendar | `dow`, `dte` | Day-of-week, days to expiry |
| Options sentiment | `pcr`, `pcr_ma5`, `pcr_chg` | Put/call ratio and its trend |
| Opening signal | `vix_open_chg` | VIX gap at 9:15 AM (risk-on/off at entry) |
| Institutional flow | `fii_net_cash_z` | Z-scored FII cash market activity (prev day) |

The autoresearch loop (`autoloop_bn.py`) proposes additions/removals to this list and validates each on the 252-day holdout before committing.

---

## What to Read for Common Tasks

| Task | Files to read |
|---|---|
| Debug a morning trade failure | `auto_trader.py` + `logs/auto_trader.log` |
| Change SL % or RR | `auto_trader.py` constants (top of file) |
| Add a new indicator | `signal_engine.py` `score_row()` + `compute_indicators()` |
| Understand ML features | `ml_engine.py` `FEATURE_COLS` + `compute_features()` — see ML Feature Set below |
| Run/debug autoresearch | `autoloop_bn.py` + `autoexperiment_bn.py` + `ml_engine_paper.py` + `research_program_bn.md` |
| Change lot size | `backtest_engine.py` `get_lot_size()` + `auto_trader.py` `LOT_SIZE` |
| Run new backtest | `backtest_engine.py` — standalone, reads `data/signals.csv` |
| Add data source | `data_fetcher.py` + `model_evolver.py` feature list |
| Change HPO trials | `model_evolver.py` top — `N_TRIALS = 30` |
| Add a new model | `model_evolver.py` `_build_model()` + competition loop |
| Check live P&L | Dhan app → Positions, or `python3 -c "from backfill_live_trades import ..." ` |

---

## Dhan API Notes

- **Token**: expires every 24h; auto-renewed by `renew_token.py` every 5 min at T+23h50m. `.env` is rewritten in place.
- **DH-906**: "Market closed" OR "weekend AMO block". Not an account issue.
- **AMO window**: Mon–Fri after 3:30 PM IST. Weekends reject all `afterMarketOrder: true`.
- **Super Order**: `/v2/super/orders` — single call for entry + SL + TP.
- **BankNifty scrip**: `UnderlyingScrip: 25`, `UnderlyingSeg: "IDX_I"`.
- **Rate limit**: Data API = 10 req/s. Long fetches (historical PCR) pace at 2 req/s for 5× headroom.
