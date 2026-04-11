# CLAUDE.md — BankNifty Auto-Trader Architecture Map

Quick reference for Claude Code. Read this before touching any file.

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
| `signal_engine.py` | Rule-based indicators → `data/signals.csv` |
| `ml_engine.py` | Walk-forward RF → `data/signals_ml.csv`; fast predict via champion.pkl |
| `model_evolver.py` | Nightly 11 PM — Optuna HPO (RF/XGB/LGB) → `models/champion.pkl` |
| `backtest_engine.py` | Historical P&L simulation with cost model + lot-size timeline |
| `data_fetcher.py` | Downloads OHLCV + global market data → `data/*.csv` |
| `lot_expiry_scanner.py` | Monthly cron — detects BankNifty lot size / expiry day changes |
| `signal_engine.py` | Rule-based signal scorer; 4 active indicators |
| `notify.py` | Telegram send/log helper (2 functions) |
| `dhan_mcp.py` | MCP server — exposes live Dhan positions/orders/P&L to Claude Code |
| `setup_automation.sh` | One-shot VM setup: pip deps, cron install, dry-run verification |

---

## Key Constants (auto_trader.py)

```python
LOT_SIZE     = 30        # BankNifty lot size (post Jun 2025 NSE mandate)
SL_PCT       = 0.15      # 15% stop-loss on premium
RISK_PCT     = 0.05      # 5% of capital at risk per trade
MAX_LOTS     = 20        # hard cap on position size
PREMIUM_K    = 0.004     # approx premium factor: BN_open × PREMIUM_K × sqrt(DTE)
ITM_WALK_MAX = 2         # max 200pt ITM probe when capital is flush
RR           = 2.0       # reward:risk (SL=15% → TP=30%)
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
2. compute_features() from ml_engine + extended features (gold, crude, PCR, FII)
3. Feature selection via RF importance (keep > 1%)
4. Optuna HPO: 40 trials × RF + XGB + LGB = 120 trials (~5-8 min)
5. Champion = best on 252-day temporal holdout (accuracy + recall blend)
6. Retrain champion on full data
7. Save: models/champion.pkl + models/champion_meta.json
8. Telegram: evolver report (plain-language summary)
```

---

## ML Fast Path (ml_engine.py --predict-today)

```python
# Fast path (< 5 sec): loads models/champion.pkl if trained within 2 days
# Slow fallback (30 sec): retrains RF from scratch (used when no champion exists)
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
| `data/signals.csv` | Rule-based signals (signal_engine.py output) |
| `data/signals_ml.csv` | ML-overridden signals (ml_engine.py output) |
| `models/champion.pkl` | Best HPO model from last evolver run |
| `models/champion_meta.json` | Model type, accuracy, feature list, trained_at |

---

## Cron Schedule (GCP VM)

```
45 3  * * 1-5   auto_trader.py          # 9:15 AM IST (3:45 AM UTC)
30 17 * * 1-5   model_evolver.py        # 11 PM IST (17:30 UTC)
30 4  1  * *    lot_expiry_scanner.py   # 1st of month 10 AM IST
```

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
python3 backtest_engine.py            # rule-based backtest
python3 backtest_engine.py --ml       # ML backtest

# Live test
python3 auto_trader.py --dry-run

# Nightly evolver (manually)
python3 model_evolver.py

# Lot/expiry scanner
python3 lot_expiry_scanner.py --show   # print current override state
python3 lot_expiry_scanner.py          # run scan + Telegram alert if change
```

---

## What to Read for Common Tasks

| Task | Files to read |
|---|---|
| Debug a morning trade failure | `auto_trader.py` + `logs/auto_trader.log` |
| Change SL % or RR | `auto_trader.py` constants (top of file) |
| Add a new indicator | `signal_engine.py` `score_row()` + `compute_indicators()` |
| Understand ML features | `ml_engine.py` `FEATURE_COLS` + `compute_features()` |
| Change lot size | `backtest_engine.py` `get_lot_size()` + `auto_trader.py` `LOT_SIZE` |
| Run new backtest | `backtest_engine.py` — standalone, reads `data/signals.csv` |
| Add data source | `data_fetcher.py` + `model_evolver.py` feature list |
| Check live P&L | `dhan_mcp.py` (MCP) or ask Claude "show positions" |

---

## Dhan API Notes

- **Token**: expires every 24h. Renew at dhan.co → API settings, update `.env`.
- **DH-906**: "Market closed" OR "weekend AMO block". Not an account issue.
- **AMO window**: Mon–Fri after 3:30 PM IST. Weekends reject all `afterMarketOrder: true`.
- **Super Order**: `/v2/super/orders` — single call for entry + SL + TP.
- **BankNifty scrip**: `UnderlyingScrip: 25`, `UnderlyingSeg: "IDX_I"`.
