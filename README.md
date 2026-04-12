# BankNifty Options Auto-Trader

Fully automated BankNifty options trading on a GCP VM. Every weekday at 9:15 AM IST, the system wakes up, reads the market, picks a direction, sizes the position, places a Dhan Super Order with stop-loss and target baked in, and sends a Telegram alert. Nothing to do during market hours.

---

> **Disclaimer**
> This project is for educational and research purposes only. Nothing here is financial advice or a recommendation to trade. Options trading involves substantial risk of loss. Past backtest results don't guarantee future performance. Trade at your own risk. Consult a SEBI-registered advisor before making any real trading decisions.

---

## What runs and when

```
Every 5 minutes (all 7 days)
  └── renew_token.py       — checks if token is 23h50m old, renews if so
                             (never lets it expire, no manual action needed)

9:15 AM IST, Mon–Fri
  └── auto_trader.py
        1. Pull latest market data
        2. Run rule-based signal engine  →  CALL / PUT / NONE
        3. Run ML direction model        →  overrides rules when confident
        4. Size the position (lots)      →  5% of capital at risk, min 1 lot
        5. Place Dhan Super Order        →  entry + SL + target in one call
        6. Send Telegram alert

11:00 PM IST, Mon–Fri
  └── model_evolver.py
        — Retrains the ML brain: Optuna HPO across RF + XGBoost + LightGBM
        — Picks the best model, saves it as champion.pkl
        — Sends a Telegram brain-training report

1st of every month, 10:00 AM IST
  └── lot_expiry_scanner.py
        — Checks if NSE has changed BankNifty lot size or expiry structure
        — Alerts via Telegram if anything has changed
```

---

## Trade parameters

| | |
|---|---|
| Instrument | BankNifty options (monthly last-Tuesday expiry, Phase 4 Sep 2025+) |
| Trading days | All 5 weekdays — Wednesday is a normal day (not an expiry day anymore) |
| Direction | CALL or PUT — decided by ML oracle, informed by rule-based score |
| Strike | ATM first, walks OTM if unaffordable for your capital |
| Stop-loss | 15% of premium |
| Target | 37.5% of premium (RR = 2.5×) |
| Trailing stop | ₹5 option price movement |
| Risk per trade | 5% of available capital |
| Minimum trade | 1 lot (always trades if you can physically afford it) |
| Max lots | 20 |
| Lot size | 30 (Jan 2026+) |
| Exit | Same-day intraday (MIS) — no overnight risk |

---

## How direction is decided

**Step 1 — Rule score (4 signals):**

| Signal | Bullish +1 | Bearish −1 |
|---|---|---|
| EMA20 | BN above 20-day average | BN below 20-day average |
| 5-day trend | BN up >+1% over 5 days | BN down >−1% over 5 days |
| VIX direction | VIX falling | VIX rising |
| BN vs Nifty | BN outperforming Nifty >+0.5% | Underperforming >−0.5% |

Score ≥ +1 → tentative CALL · Score ≤ −1 → tentative PUT

**Step 2 — ML override:**

A walk-forward Random Forest (trained on 4.5 years of data, 71 models) overrides the rule direction when it disagrees. The ML model is right ~86% of the time when it conflicts with the rules. It is a direction oracle — always outputs CALL or PUT, never skips.

Top ML features: yesterday's BN return (40% weight), BN-Nifty divergence, overnight gap.

**Nightly evolution:** `model_evolver.py` runs Optuna HPO across three model types every night and promotes the best one. So the model you trade with tomorrow was selected last night from a competition of 120 variants.

---

## Architecture

```
auto_trader.py         Morning runner — orchestrates everything, places the order
signal_engine.py       Rule-based indicators → data/signals.csv
ml_engine.py           Walk-forward ML → data/signals_ml.csv; loads champion.pkl for fast morning predict
model_evolver.py       Nightly 11 PM — Optuna HPO (RF/XGB/LGB) → models/champion.pkl
backtest_engine.py     Historical P&L simulation with full cost model
data_fetcher.py        Downloads OHLCV + global data → data/  (incremental, parallel)
lot_expiry_scanner.py  Monthly check: BankNifty lot size / expiry day changes
renew_token.py         Dynamic token renewal — 23h50m interval, 7 days a week
notify.py              Telegram send helper (2 functions)
dhan_mcp.py            MCP server — query live Dhan positions/P&L from Claude Code
setup_automation.sh    One-shot VM setup: install deps, set up cron, dry-run test
```

---

## Setup

### What you need

- GCP VM (or any Linux server) with Python 3.10+
- Dhan trading account with API access enabled
- Telegram bot (create one at t.me/BotFather)

### 1. Clone and install

```bash
git clone https://github.com/PradyXiii/dhan-trading.git
cd dhan-trading
pip3 install pandas numpy yfinance requests python-dotenv scikit-learn \
             optuna xgboost lightgbm joblib mcp --break-system-packages
```

### 2. Set up credentials

```bash
cp .env.example .env
nano .env
```

```
DHAN_ACCESS_TOKEN=eyJ...           # from dhan.co → API settings
DHAN_CLIENT_ID=your_client_id
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. First-time data fetch and ML training

```bash
python3 data_fetcher.py       # downloads all market data → data/
python3 signal_engine.py      # generates data/signals.csv
python3 ml_engine.py          # walk-forward training (~2 min) → data/signals_ml.csv
python3 model_evolver.py      # first HPO run — takes 5–10 min, saves models/champion.pkl
```

### 4. Install cron and test

```bash
bash setup_automation.sh      # sets up all cron jobs, tests API, runs dry-run
```

This sets up four cron jobs: token renewer (every 5 min), auto trader (9:15 AM), model evolver (11 PM), lot scanner (1st of month).

### 5. Verify it's all working

```bash
python3 auto_trader.py --dry-run    # full morning flow without placing an order
crontab -l                          # confirm all 4 cron jobs are listed
cat token_meta.json                 # shows when token was last renewed
```

---

## Useful commands

```bash
# Data and signals
python3 data_fetcher.py                      # fetch latest market data
python3 signal_engine.py                     # recompute rule signals
python3 ml_engine.py --predict-today         # fast single ML prediction (<10 sec)
python3 ml_engine.py --analyze               # feature importance report

# Backtesting
python3 backtest_engine.py                   # rule-based backtest (uses real premiums if available)
python3 backtest_engine.py --real-premium    # explicitly use real premiums
python3 backtest_engine.py --real-premium-ml # ML backtest with real premiums

# Live test
python3 auto_trader.py --dry-run             # see exactly what would trade today

# Model evolver
python3 model_evolver.py                     # run nightly HPO manually
python3 model_evolver.py --no-data           # HPO only, skip data fetch

# Token management (manual, rarely needed)
python3 renew_token.py                       # check / force token renewal
cat token_meta.json                          # see when it was last renewed

# Lot/expiry scanner
python3 lot_expiry_scanner.py --show         # print current lot size and expiry override
python3 lot_expiry_scanner.py                # run scan + alert if anything changed
```

---

## Live P&L from Claude Code

`dhan_mcp.py` is an MCP server that lets you ask Claude Code about your live positions:

> "Show me my current positions"  
> "What's today's P&L?"  
> "Did my stop-loss trigger?"

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "dhan": {
      "command": "python3",
      "args": ["/home/user/dhan-trading/dhan_mcp.py"]
    }
  }
}
```

---

## Key design choices

**ML as direction oracle, not filter** — Rules alone are only ~50% directionally accurate. The ML model hits 84.8% directional accuracy. Rather than filtering trades away, it just corrects the direction.

**Monthly expiry (Phase 4, Sep 2025+)** — BankNifty switched from weekly Thursday expiry to monthly last-Tuesday expiry. This means Wednesday is now a normal trading day with 5–20 DTE, not an expiry-day skip.

**Dynamic token renewal** — Rather than renewing at a fixed time daily (which creates an exact 24h gap over weekends), `renew_token.py` checks every 5 minutes and renews at exactly T+23h50m. Max gap is always 23h55m regardless of day.

**1-lot minimum** — The 5% risk rule is for sizing (more capital → more lots), not a gate. If you can physically afford 1 lot, the system trades it even if 5% risk would say 0 lots.

**Nightly model competition** — Three model types (RF, XGBoost, LightGBM) compete every night via Optuna HPO. The winner is promoted to champion. Next morning loads in <5 seconds.

---

> **Risk warning**: Options trading carries a high level of risk. This system is provided as-is with no warranty of fitness for live trading. Use at your own risk.
