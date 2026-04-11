# BankNifty Options Auto-Trader

Fully automated weekly BankNifty ATM options system. Runs every trading day at 9:15 AM IST on a GCP VM — fetches data, runs an ML direction engine, sizes the position, places a Dhan intraday Super Order, and sends a Telegram alert. No human input required during market hours.

---

> **Disclaimer**
> This project is for **educational and research purposes only**. Nothing in this repository constitutes financial advice, investment advice, or a recommendation to buy or sell any financial instrument. Trading options and derivatives involves substantial risk of loss and is not suitable for all investors. Past backtest performance does not guarantee future results. Always consult a SEBI-registered investment advisor before making any trading or investment decisions. The authors and contributors of this project accept no liability for any financial losses incurred from using this software.

---

## How It Works

```
Each trading day (Mon / Tue / Thu / Fri) at 9:15 AM IST:

  Fetch latest OHLCV + global data
        ↓
  Rule-based signal engine  →  signals.csv
        ↓
  ML direction oracle       →  signals_ml.csv  (84.8% directional accuracy)
        ↓
  CALL / PUT  →  ATM weekly option, Dhan Super Order + trailing SL
  NONE        →  No trade (event day override), Telegram notification
```

All 5 weekdays traded. Phase 4 (Sep 2025+): monthly last-Tuesday expiry, so Wednesday is a normal trading day with 5–20 DTE.

---

## Signal Engine

Four rule-based indicators produce a directional score. The ML engine then overrides direction using a walk-forward Random Forest trained on 4.5 years of data.

### Rule-based indicators (4 active)

| Indicator | Bullish (+1) | Bearish (−1) |
|---|---|---|
| EMA20 | BN close > EMA20 | BN close < EMA20 |
| 5-day trend | BN 5-day change > +1% | < −1% |
| VIX direction | VIX falling | VIX rising |
| BN-NF divergence | BN outperforms Nifty50 > +0.5% | underperforms > −0.5% |

Score ≥ +1 → tentative CALL · Score ≤ −1 → tentative PUT · Score = 0 → No trade

### ML Direction Oracle

A walk-forward Random Forest trained on 21 features overrides the rule-based direction. It is a **direction oracle, not a filter** — it always outputs CALL or PUT (no skipping trades), and it disagrees with the rule-based direction on ~35% of days. On those days, ML is correct 86% of the time vs the rules at 7.5%.

Top features by importance:
- `bn_ret1` (40%) — yesterday's BankNifty return (momentum)
- `bn_nf_div` (12%) — BankNifty vs Nifty50 divergence
- `bn_gap` (12%) — overnight gap

---

## Trade Parameters

| | Value |
|---|---|
| Trading days | Mon / Tue / Wed / Thu / Fri (all 5 — Phase 4 monthly expiry) |
| Stop-loss | 15% of premium |
| Target | 30% of premium (RR = 2.0×) |
| Trailing stop | ₹5 option price jump |
| Risk per trade | 5% of available capital |
| Lot size | 30 (post Jun 2025 NSE mandate) |
| Max lots | 20 |
| Exit | Intraday only — MIS, no carryforward |

Premium approximation (no intraday options data used for sizing):
`premium = BN_open × 0.004 × sqrt(actual_DTE)`

---

## Architecture

```
dhan-trading/
│
├── auto_trader.py         Morning automation: data → ML signal → Dhan order → Telegram
├── signal_engine.py       Rule-based indicators → signals.csv
├── ml_engine.py           Walk-forward ML oracle → signals_ml.csv
├── model_evolver.py       Nightly 11 PM: HPO across RF/XGBoost/LightGBM → champion.pkl
├── backtest_engine.py     Historical simulation with full cost model
├── data_fetcher.py        Fetches OHLCV + global data → data/
├── lot_expiry_scanner.py  Monthly check: detects BankNifty lot size / expiry day changes
├── notify.py              Telegram notification helper
├── dhan_mcp.py            MCP server: query live positions/P&L from Claude Code
├── setup_automation.sh    One-shot VM setup: deps, cron, dry-run
│
└── data/                  CSV files (gitignored — lives on GCP VM only)
    ├── banknifty.csv
    ├── signals.csv
    └── signals_ml.csv
```

### Data Sources

| Data | Source |
|---|---|
| BankNifty / Nifty50 OHLCV | Dhan API (v2/charts/historical) |
| India VIX | Yahoo Finance (^INDIAVIX) |
| S&P 500, Nikkei 225, S&P Futures | Yahoo Finance |

---

## Setup

### Prerequisites

- GCP VM (or any Linux server) with Python 3.10+
- Dhan trading account with API access enabled
- Telegram bot (create at t.me/BotFather)

### 1. Clone and install

```bash
git clone https://github.com/PradyXiii/dhan-trading.git
cd dhan-trading
pip3 install pandas numpy yfinance requests python-dotenv scikit-learn mcp --break-system-packages
```

### 2. Configure credentials

```bash
cp .env.example .env
nano .env
```

```
DHAN_ACCESS_TOKEN=eyJ...           # from dhan.co → API settings (expires every 24h)
DHAN_CLIENT_ID=your_client_id      # found in Dhan API settings
TELEGRAM_BOT_TOKEN=your_bot_token  # from t.me/BotFather
TELEGRAM_CHAT_ID=your_chat_id      # your Telegram user/channel ID
```

### 3. Fetch data, generate signals, run ML walk-forward

```bash
python3 data_fetcher.py      # downloads all market data → data/
python3 signal_engine.py     # generates data/signals.csv
python3 ml_engine.py         # walk-forward ML (~2 min) → data/signals_ml.csv
python3 backtest_engine.py   # optional: run backtest, print summary
python3 backtest_engine.py --ml   # optional: ML backtest
```

### 4. Install automation

```bash
bash setup_automation.sh     # sets up cron, tests connection, dry-run
```

### 5. Test dry-run

```bash
python3 auto_trader.py --dry-run
```

---

## Daily Workflow

```
4–5 PM IST  →  Get new Dhan token (expires every 24h)
               nano .env → update DHAN_ACCESS_TOKEN

9:15 AM IST →  Cron fires automatically:
               1. data_fetcher.py       — pulls latest data
               2. signal_engine.py      — rule-based signal
               3. ml_engine.py --predict-today  — ML direction (~10 sec)
               4. Dhan Super Order placed
               5. Telegram alert sent
               No action needed
```

### Token auto-refresh (optional)

If your Dhan account has TOTP enabled, a separate `refresh_token.py` script can automate the daily token rotation. Set up as a cron at 6:30 AM IST (1 AM UTC):

```cron
0 1 * * 1-5 python3 /home/user/dhan-trading/refresh_token.py >> ~/token.log 2>&1
```

---

## Live P&L via Claude Code

`dhan_mcp.py` is an MCP server that lets you query live Dhan positions directly from Claude Code:

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

## ML Engine Modes

```bash
python3 ml_engine.py                   # full walk-forward (backtest use)
python3 ml_engine.py --predict-today   # fast single prediction (live use, ~10 sec)
python3 ml_engine.py --analyze         # feature importance + accuracy report
python3 ml_engine.py --filter 0.60     # confidence-gate mode (fewer trades, higher WR)
```

---

## Built With

- **Python** — core language
- **Dhan API v2** — order placement, option chain, positions, fund limit
- **pandas / numpy** — data processing and backtesting
- **scikit-learn** — walk-forward Random Forest ML engine
- **yfinance** — global market data (VIX, S&P500, Nikkei)
- **Telegram Bot API** — trade notifications
- **MCP (Model Context Protocol)** — Claude Code integration for live P&L queries
- **GCP Compute Engine** — VM for 24/7 automation (IP whitelisted on Dhan)
- **cron** — daily 9:15 AM IST scheduling

---

## Key Design Decisions

| Decision | Why |
|---|---|
| ML as direction oracle, not filter | Rules are directionally correct only ~50% of the time. ML hits 84.8% directional accuracy — same trade count, better direction |
| All 5 weekdays traded | Phase 4 (Sep 2025+): BankNifty is monthly last-Tuesday expiry. Wednesday is a normal trade day with 5–20 DTE, not expiry day |
| 4 rule-based indicators | Attribution showed macro signals (US/Japan) add noise. India-only technicals outperform the full 10-indicator set |
| 15% SL / 30% TP / 2.0× RR | Optimised over SL sweep (10–30%). 15% SL with 2.0× RR gives best P&L with -5.5% max drawdown |
| Trailing stop ₹5 | Locks in profit as the trade moves. Allows partial exits rather than binary WIN/LOSS |
| 5% risk per trade | Balances compounding growth with drawdown control |
| 20-lot cap | Liquidity and margin constraint as capital grows |
| No carryforward | Intraday MIS only. No overnight risk |

---

> **Risk Warning**: Options trading carries a high level of risk. You can lose more than your initial investment. This system is provided as-is with no warranty of fitness for live trading. Use at your own risk.
