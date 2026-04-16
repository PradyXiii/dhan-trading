# CLAUDE.md — BankNifty Auto-Trader Architecture Map

Quick reference for Claude Code. Read this before touching any file.

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

## What This System Does

Fully automated BankNifty options trading. Cron fires at 9:15 AM IST on trading days:
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
| `dhan_mcp.py` | MCP server — exposes live Dhan positions/orders/P&L to Claude Code |
| `autoloop_bn.py` | Saturday autoresearch — Claude API proposes feature/signal changes, keep/revert via git |
| `autoexperiment_bn.py` | Fast 252-day holdout evaluator used by autoloop (outputs JSON composite score) |
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

## 9:15 AM Flow (auto_trader.py main)

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
| `data/live_trades.csv` | Daily live-trade outcomes (written by trade_journal.py) |
| `data/today_trade.json` | What auto_trader placed today (read by trade_journal) |
| `models/champion.pkl` | Best HPO model from last evolver run |
| `models/champion_meta.json` | Model type, accuracy, feature list, trained_at |
| `models/ensemble/*.pkl` | 4-model ensemble (rf/xgb/lgb/cat) for live voting |
| `models/ensemble_meta.json` | Per-model meta for each ensemble member |

---

## Cron Schedule (GCP VM)

Installed by `setup_automation.sh`:

```
*/5 *  * * *    renew_token.py          # every 5 min, all 7 days
20 3   * * 1-5  health_ping.py          # 8:50 AM IST
45 3   * * 1-5  auto_trader.py          # 9:15 AM IST
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

# Fetch historical ATM option premiums (one-time, then incremental)
python3 data_fetcher.py --fetch-options

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
| Run/debug autoresearch | `autoloop_bn.py` + `autoexperiment_bn.py` + `research_program_bn.md` |
| Change lot size | `backtest_engine.py` `get_lot_size()` + `auto_trader.py` `LOT_SIZE` |
| Run new backtest | `backtest_engine.py` — standalone, reads `data/signals.csv` |
| Add data source | `data_fetcher.py` + `model_evolver.py` feature list |
| Change HPO trials | `model_evolver.py` top — `N_TRIALS = 30` |
| Add a new model | `model_evolver.py` `_build_model()` + competition loop |
| Check live P&L | `dhan_mcp.py` (MCP) or ask Claude "show positions" |

---

## Dhan API Notes

- **Token**: expires every 24h; auto-renewed by `renew_token.py` every 5 min at T+23h50m. `.env` is rewritten in place.
- **DH-906**: "Market closed" OR "weekend AMO block". Not an account issue.
- **AMO window**: Mon–Fri after 3:30 PM IST. Weekends reject all `afterMarketOrder: true`.
- **Super Order**: `/v2/super/orders` — single call for entry + SL + TP.
- **BankNifty scrip**: `UnderlyingScrip: 25`, `UnderlyingSeg: "IDX_I"`.
- **Rate limit**: Data API = 10 req/s. Long fetches (historical PCR) pace at 2 req/s for 5× headroom.
