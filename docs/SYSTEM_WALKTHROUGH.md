# Nifty50 Multi-Strategy Auto-Trader — Full System Walkthrough

Plain-English guide: what happens every trading day, pre-market to midnight.

---

## The One-Line Version

Every weekday the system wakes up, reads the market, routes to IC (CALL signal) or Bull Put (PUT signal), places orders via Dhan, monitors every minute for SL/TP, closes automatically, and retrains the ML brain at night.

---

## Why This Hybrid Strategy?

**Iron Condor (CALL signal days):**
```
SELL ATM CE  + BUY ATM+150 CE    ← upper wing
SELL ATM PE  + BUY ATM-150 PE    ← lower wing
```
Market-neutral theta. Wins if Nifty stays inside the wings — even if the CALL signal is wrong and markets go sideways. Weekly Tuesday expiry (DTE ≤ 7) = fast theta decay. Backtest 2021–2026: 84.7% WR, 1,116 trades, max drawdown −0.7% (Apr 2026 ML upgrade: Kalman + HMM features lifted net P&L from ₹1.17Cr → ₹1.38Cr).

**Bull Put Credit Spread (PUT signal days):**
```
SELL ATM PE  + BUY ATM-150 PE    ← 2 legs
```
Directional credit — profits when market stays flat or goes UP. The ML model has a CALL bias, so markets often go UP even on PUT signal days. Bull Put profits in exactly that scenario. Backtest Sep 2025–Apr 2026: 100% WR, 51 trades.

**Why not Bear Call on CALL days?** Bear Call = short CE = needs market to go DOWN. CALL signal days = model says market going UP. Direction conflict. 7-year backtest: 13.5% WR, −₹24.03L. Permanently discarded.

**Why NF and not BNF?** BankNifty lost weekly expiry Nov 2024 (SEBI). Monthly = DTE 15-22 = gamma dominates theta = IC WR collapsed to 27%. NF kept weekly Tuesday. Every trade naturally DTE ≤ 7.

---

## Daily Timeline

### 8:50 AM IST — `health_ping.py`

Pre-market check:
- Dhan token valid (not expired)
- Capital above minimum
- `data/signals_ml.csv` updated today (ML ran last night)
- No stale lock file from crashed previous run

Sends Telegram: "✅ All clear" or "🚨 ALERT: [specific problem]". If no message → don't trust auto_trader.

---

### 9:15 AM IST — `morning_brief.py`

Fetches Nifty50 RSS headlines (ET, Google News: RBI/CPI/FII/SEBI) from last 20 hours. Calls Claude API → returns:
```json
{"direction": "BULLISH|BEARISH|NEUTRAL", "confidence": "HIGH|MEDIUM|LOW", "reason": "one sentence"}
```
Writes `data/news_sentiment.json`. auto_trader reads it as one extra vote at 9:30 AM.

---

### 9:30 AM IST — `auto_trader.py`

**Step 1: Acquire lock**
`fcntl` advisory lock — prevents two cron runs overlapping.

**Step 2: Check credentials**
Verifies Dhan token live. Dead → Telegram alert, exit.

**Step 3: Check lot size**
Reads `data/lot_size_overrides.json` (written by lot_expiry_scanner). Warns if `LOT_SIZE` in code doesn't match expected for today's date.

**Step 4: Check exit marker**
Verifies yesterday's `exit_positions.py` ran clean. Skipped in paper mode.

**Step 5: Refresh data + signal**
Three subprocesses in sequence:
1. `data_fetcher.py` — pulls latest OHLCV + macro + options
2. `signal_engine.py` — rule-based signal → `data/signals.csv`
3. `ml_engine.py --predict-today` — fast prediction using saved ensemble → `data/signals_ml.csv`

**Step 6: Holiday check**
If NSE holiday → Telegram "No trade today (holiday)", exit.

**Step 7: Get today's signal**
Reads `data/signals_ml.csv`. NONE → exit. CALL or PUT → continue.

Signal vote: 4 rule-based indicators + ML ensemble (RF + XGB + LGB + CAT) + news sentiment (morning_brief).

**Step 8: Routing decision**
```python
_use_bull_put_today = (signal == "PUT")   # PUT days → Bull Put
# CALL days → IC (or Straddle if capital ≥ ₹2.3L)
```
VIX and ML confidence filters are **bypassed in IRON_CONDOR_MODE** — no filter = max P&L per 7-year backtest.

**Step 9: Get capital**
Dhan `/fundlimit` API → available margin for sizing.

**Step 10: Check straddle auto-upgrade**
If capital ≥ ₹2,30,000 AND signal == CALL → Short Straddle replaces IC.

**Step 11: Get expiry**
Dhan `/optionchain/expirylist` (scrip=13). First date ≥ today (IST). Always nearest Tuesday.

---

**━━ BULL PUT PATH (signal == PUT) ━━**

**Step 12p: Get Bull Put legs**
Option chain → ATM PE (short) + ATM-150 PE (long). Queries Dhan `/margincalculator/multi` for actual SPAN margin. `lots = min(10, floor(capital / actual_span))`.

**Step 13p: Telegram alert**
Shows: signal, both strikes, net credit, SL/TP triggers, lot count.

**Step 14p: Duplicate guard**
Dhan positions API — if any open NF position → abort.

**Step 15p: Place 2 orders (MANDATORY ORDER)**
```
1. BUY ATM-150 PE   ← long wing first (Dhan margin rule)
   wait 2s
2. SELL ATM PE      ← short leg (margin benefit now applies)
```

**Step 16p: Write today_trade.json + P&L safety net**
Writes `strategy=bull_put_credit`, both SIDs, net_credit, lots.
Calls `POST /v2/pnlExit` — sets account-level loss threshold mirroring SL level as safety net if spread_monitor misses SL.

---

**━━ IC PATH (signal == CALL) ━━**

**Step 12i: Get IC legs**
Option chain → all 4 legs. `net_credit = ce_short_ltp + pe_short_ltp - ce_long_ltp - pe_long_ltp`. SPAN margin from Dhan API. `lots = min(10, floor(capital / actual_span))`.

**Step 13i: Chain signals (informational)**
Max pain, GEX, straddle cost — Telegram context only, doesn't affect trade decision.

**Step 14i: Telegram alert**
Signal, VIX, all 4 strikes, net credit, SL trigger (NO TP — IC holds to EOD), lot count.

**Step 15i: Duplicate guard**
today_trade.json + Dhan positions API.

**Step 16i: Place 4 orders (MANDATORY ORDER)**
```
1. BUY ATM+150 CE   ← long CE wing first (Dhan margin rule)
   wait 2s
2. SELL ATM CE      ← short CE
   wait 2s
3. BUY ATM-150 PE   ← long PE wing first
   wait 2s
4. SELL ATM PE      ← short PE
```
Failure: BUY fails → FAILED + Telegram + stop. SELL fails after BUY → 🚨 PARTIAL_IC — naked exposure, manual close needed.

**Step 17i: Write today_trade.json + P&L safety net**
Writes `strategy=nf_iron_condor`, all 4 SIDs, net_credit, lots.
Calls `POST /v2/pnlExit` same as Bull Put path.

---

### 9:30 AM–3:10 PM (every 1 min) — `spread_monitor.py`

Each minute:
1. Reads `today_trade.json` — strategy, SIDs, net_credit
2. Fetches live LTPs via Dhan `/marketfeed/ltp`
3. Computes current spread cost

**IC path (SL only — no TP):**
- `current_cost = (ce_short_ltp - ce_long_ltp) + (pe_short_ltp - pe_long_ltp)`
- SL: `current_cost >= net_credit × 1.5` → close all 4 legs
- No TP — holding to EOD adds ₹21L over 5 years vs early TP

**Bull Put path (SL + TP):**
- `current_cost = short_ltp - long_ltp`
- SL: `current_cost >= net_credit × 1.5`
- TP: `current_cost <= net_credit × 0.35` (retain 65% of credit)

**Straddle path (SL only):**
- `current_cost = ce_ltp + pe_ltp`
- SL: `current_cost >= net_credit × 1.5`

**Exit sequence (all strategies):**
Primary: `DELETE /v2/positions` — one Dhan API call closes everything atomically.
Backup: leg-by-leg orders if DELETE fails.

**Exit leg order (backup path):**
- IC: BUY back shorts (CE short, PE short) → SELL longs (CE wing, PE wing)
- Bull Put: BUY back short PE → SELL long PE
- Straddle: BUY back higher-LTP (ITM/challenged) leg first → lower-LTP leg second

Shorts closed before longs to prevent naked short exposure between orders.

---

### 11:00 AM IST — `midday_conviction.py`

Fetches: current spread cost, S&P futures, DXY, India VIX, crude (via Dhan + yfinance).

Sends plain-English Telegram: "Trade looking good — 3 of 4 signs positive" or "Getting shaky — VIX jumped, S&P weak".

Writes `data/midday_checkpoints.csv` (one row per day). model_evolver reads reversal days at 5× weight.

---

### 3:15 PM IST — `exit_positions.py`

**Before closing:** captures live LTPs from Dhan positions API and writes them back to `today_trade.json`. This gives `trade_journal.py` real exit prices for per-leg P&L display.

- IC: writes `ce_short_exit`, `ce_long_exit`, `pe_short_exit`, `pe_long_exit`
- Bull Put / Bear Call: writes `exit_short_ltp`, `exit_long_ltp`, `exit_spread`
- Straddle: writes `exit_ce_ltp`, `exit_pe_ltp`, `exit_spread`
- Also writes `exit_done=True`, `exit_time`, `pnl_inr`

Primary: `DELETE /v2/positions` — one Dhan call closes all open positions atomically. Verifies after 15s.

Backup: leg-by-leg market orders (shorts first, then longs) if DELETE fails or positions still showing.

Retry loop until 3:20 PM hard deadline. After deadline with open positions: 🚨 EMERGENCY Telegram, manual action required.

Writes exit marker file for tomorrow's auto_trader health check.

---

### 3:30 PM IST — `trade_journal.py`

Reads `today_trade.json` (exit prices written by `exit_positions.py` at 3:15 PM).

**IC:** Shows per-leg `<pre>` table — each leg's entry price, exit price, and individual P&L:
```
SELL 24050 CE  ₹171 → ₹115  +₹3,640
BUY  24200 CE   ₹98 → ₹ 72   -₹1,690
SELL 23900 PE  ₹137 → ₹ 95  +₹2,730
BUY  23750 PE   ₹81 → ₹ 58   -₹1,495
```

**Bull Put / Straddle:** Summary with entry credit, exit spread cost, net P&L.

Appends to `data/live_ic_trades.csv`. Sends EOD Telegram.

---

### 11:00 PM IST — `model_evolver.py`

**Data:**
- All historical OHLCV + macro + options
- `compute_features()` → 64 features (all shifted 1 day — no lookahead)
- `data/live_ic_trades.csv` → real outcomes at 10× weight
- `data/midday_checkpoints.csv` → reversal days at 5× weight

**Feature selection:** RF importance — keep > 1%.

**Optuna HPO — 120 trials:** RF + XGBoost + LightGBM + CatBoost × 30 trials each. Temporal holdout = last 252 trading days. No leakage.

**Champion selection:** Best composite (accuracy + recall). Saved `models/champion.pkl` + `models/ensemble/*.pkl`.

**Telegram:** Which model won, holdout accuracy, live trade influence.

---

### Midnight IST — `autoloop_nf.py`

1. Reads paper vs live performance, midday reversals, current feature set
2. Calls Claude API → proposes ONE small change
3. Writes to `ml_engine_paper.py`
4. `autoexperiment_nf.py --module ml_engine_paper` → composite score on 252-day holdout
5. Score ≥ 0.6484 baseline (Apr 2026 post-Kalman/HMM) → keep; else revert
6. 3 consecutive nights outperforming → auto-promote paper → live, commit + push

Autoresearcher can only touch `ml_engine_paper.py`. Cannot touch live code, cron, Dhan calls.

---

### 2nd of each month — `regime_watcher.py`

Detects NSE lot size / expiry day changes via Dhan API. On change:
- Refreshes data
- Runs 6-month real-options backtest across all strategies
- Picks best strategy for new regime
- Auto-patches `LOT_SIZE` in `auto_trader.py`
- Auto-patches `BULL_PUT_MARGIN_PER_LOT` if Dhan SPAN drifted > 10%
- Sends Telegram strategy verdict

No human involvement needed.

---

## The Feedback Loop

```
Live trade → live_ic_trades.csv
                ↓ (10× weight)
           model_evolver (11 PM)
                ↓
           champion.pkl + ensemble
                ↓
           auto_trader (9:30 AM next day)
                ↓
           new IC or Bull Put trade
```

Plus:
```
Midday data → midday_checkpoints.csv → model_evolver (5× weight, reversal days)
autoloop proposes change → paper model → 3-night streak → live model
P&L exit (Dhan) + DELETE /v2/positions → safety net if spread_monitor misses SL
```

---

## What Each File Owns

| File | Written by | Read by |
|---|---|---|
| `data/today_trade.json` | auto_trader (entry), exit_positions (exit prices + exit_done) | spread_monitor, exit_positions, trade_journal |
| `data/live_ic_trades.csv` | trade_journal | model_evolver |
| `data/midday_checkpoints.csv` | midday_conviction | model_evolver, autoloop |
| `data/signals_ml.csv` | ml_engine | auto_trader |
| `models/champion.pkl` | model_evolver | ml_engine fast path |
| `models/ensemble/*.pkl` | model_evolver | ml_engine fast path |
| `data/paper_performance.csv` | autoloop | autoloop (promotion decision) |
| `data/regime_state.json` | regime_watcher | regime_watcher |

---

## Common Failure Modes

| Symptom | Likely cause | Check |
|---|---|---|
| No 8:50 AM Telegram | health_ping cron not running | `crontab -l`, `logs/health_ping.log` |
| "No positions found" from exit_positions | spread_monitor already closed on SL/TP | `data/today_trade.json` → `exit_time` field |
| model_evolver Telegram missing | Ran after midnight (took > 1h) | `logs/model_evolver.log` |
| Autoresearch not pushing | SSH key not configured | `ssh -T git@github.com` on VM |
| PARTIAL_IC critical alert | SELL leg placed but BUY fill delayed | Check Dhan app orders, manual close if needed |
| P&L exit fires unexpectedly | `profitValue` set below current P&L at time of POST | Check `logs/auto_trader.log` for P&L exit setup line |
| DELETE /v2/positions non-SUCCESS | AMO window or market closed | Check time — only works 9:15 AM–3:30 PM IST |

---

## Key Constants (all in `auto_trader.py`)

```python
PAPER_MODE         = False      # LIVE — real orders from Apr 22 2026
IRON_CONDOR_MODE   = True       # primary strategy path
LOT_SIZE           = 65         # NF lot size from Jan 6 2026
MAX_LOTS           = 10         # IC max lots
SPREAD_WIDTH       = 150        # pts between short and long leg (ATM ± 3 strikes)
CREDIT_SL_FRAC     = 0.5        # SL: spread cost grew 50% above entry credit
CREDIT_TP_FRAC     = 0.65       # TP: retain 65% (Bull Put only — IC has no TP)
ML_CONF_THRESHOLD  = 0.55       # informational only (bypassed in IRON_CONDOR_MODE)
VIX_MIN_TRADE      = 13.0       # informational only (bypassed in IRON_CONDOR_MODE)
VIX_MAX_TRADE      = 20.0       # informational only (bypassed in IRON_CONDOR_MODE)
BULL_PUT_MARGIN_PER_LOT = 55_000  # fallback only — actual sizing via Dhan SPAN API
STRADDLE_MARGIN_PER_LOT = 230_000 # auto-upgrade threshold
```
