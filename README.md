# BankNifty Options Auto-Trader

Fully automated weekly BankNifty ATM options system. Runs every trading day at 9:15 AM IST on a GCP VM — fetches data, scores signals, sizes the position, places a Dhan intraday Super Order, and sends a Telegram alert. No human input required during market hours.

---

## Backtest Results — Sep 2021 to Apr 2026

| Metric | Value |
|---|---|
| Net P&L | ₹1.45 Cr |
| Capital injected | ₹5.8L (₹30K start + ₹10K/month × 55 months) |
| Ending capital | ₹1.51 Cr |
| Return on capital | **26×** in 4.5 years |
| Win rate | 54.6% |
| Total trades | 892 |
| Max drawdown | −24.9% |
| Transaction costs | ₹3.15L (2.1% of gross P&L) |

---

## How the Strategy Works

```
Each trading day (Mon–Fri) at 9:15 AM IST:

  Score 4 indicators  →  CALL / PUT / NONE
        ↓
  CALL / PUT  →  ATM weekly option, Dhan Super Order
  NONE        →  No trade, Telegram notification
```

### Signal Engine — 4 Active Indicators

| Indicator | Bullish (+1) | Bearish (−1) |
|---|---|---|
| EMA20 | BN close > EMA20 | BN close < EMA20 |
| 5-day trend | BN 5-day change > +1% | < −1% |
| VIX direction | VIX falling | VIX rising |
| BN-NF divergence | BN outperforms Nifty50 > +0.5% | underperforms > −0.5% |

Score ≥ +1 → BUY CALL · Score ≤ −1 → BUY PUT · Score = 0 → No trade

Macro signals (S&P500, Nikkei, S&P futures, BN overnight gap) were tested and found to be net negative drag — attribution analysis showed removing them increases P&L by +47%.

### Trade Parameters

| Day | DTE | Premium | RR | Breakeven WR |
|---|---|---|---|---|
| Monday | 2 | spot × 0.57% | 1.6× | 38.5% |
| Tuesday | 1 | spot × 0.40% | 1.4× | 41.7% |
| Wednesday | 0.25 | spot × 0.20% | 1.0× | 50.0% |
| Thursday | 6 | spot × 0.98% | 2.0× | 33.3% |
| Friday | 5 | spot × 0.89% | 2.0× | 33.3% |

- Stop-loss: 30% of premium
- Lot size: 30 | Max lots: 20 (capital-based sizing at 5% risk per trade)
- No carryforward — MIS intraday, auto-exits at 3:15 PM

---

## Architecture

```
dhan-trading/
│
├── data_fetcher.py          Fetches all market data → data/
├── signal_engine.py         Computes indicators, generates CALL/PUT/NONE signals
├── backtest_engine.py       Simulates trades on historical data with full cost model
├── auto_trader.py           Morning automation: signal → Dhan order → Telegram
├── notify.py                Telegram notification helper
├── test_connection.py       Verify Dhan API token is valid
├── setup_automation.sh      One-shot setup: deps, cron, dry-run
│
├── indicator_attribution.py Research: which indicators actually drive P&L
├── strangle_backtest.py     Research: straddle vs directional long comparison
├── timing_backtest.py       Research: entry timing (9:15–9:30) + slippage sensitivity
├── fetch_intraday.py        Research: fetch 5-min BankNifty candles from Dhan
├── fetch_round2_data.py     Research: NSE bhavcopy + participant OI data
├── dhan_mcp.py              MCP server: query live positions/P&L from Claude Code
│
├── BACKTEST_LOG.md          Full run history, decisions, methodology
└── data/                    CSV files (not in GitHub — lives on GCP VM only)
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
pip3 install pandas numpy yfinance requests python-dotenv mcp --break-system-packages
```

### 2. Configure credentials

```bash
cp .env.example .env
nano .env
```

```env
DHAN_ACCESS_TOKEN=eyJ...      # from dhan.co → API settings (expires every 24h)
DHAN_CLIENT_ID=1111158553
TELEGRAM_BOT_TOKEN=85685...
TELEGRAM_CHAT_ID=6152227460
```

### 3. Fetch historical data + run backtest

```bash
python3 data_fetcher.py          # downloads all market data → data/
python3 signal_engine.py         # generates signals.csv
python3 backtest_engine.py       # runs backtest, prints summary
```

### 4. Install automation

```bash
bash setup_automation.sh         # sets up cron, tests connection, dry-run
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
               python3 test_connection.py

             →  Refresh today's data + signal
               python3 data_fetcher.py && python3 signal_engine.py

9:15 AM IST →  Cron fires automatically
               Dhan Super Order placed
               Telegram alert sent
               No action needed
```

### Token auto-refresh (optional)

If you have a Dhan account with TOTP enabled, `refresh_token.py` (separate repo) can automate the token refresh. Set up as a cron at 1 AM UTC (6:30 AM IST):

```cron
0 1 * * 1-5 python3 /home/user/dhan/refresh_token.py >> /home/user/dhan/token.log 2>&1
```

---

## Live P&L via Claude Code

An MCP server (`dhan_mcp.py`) lets you query your live Dhan positions directly from Claude Code:

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

## Built With

- **Python** — core language
- **Dhan API v2** — order placement, option chain, positions, fund limit
- **pandas / numpy** — data processing and backtesting
- **yfinance** — global market data (VIX, S&P500, Nikkei)
- **Telegram Bot API** — trade notifications
- **MCP (Model Context Protocol)** — Claude Code integration for live P&L queries
- **GCP Compute Engine** — VM for 24/7 automation (IP whitelisted on Dhan)
- **cron** — daily 9:15 AM IST scheduling

---

## Key Design Decisions

| Decision | Why |
|---|---|
| Directional long (not straddle) | BN avg daily range ~586 pts < straddle breakeven (800–1960 pts). Directional wins by ₹1.04 Cr vs straddle −₹5.4L loss |
| 4 indicators (not 10) | Attribution showed macro signals (US/Japan) are noise. India-only technical signals deliver +47% more P&L |
| All 5 days including Wed (0 DTE) | Wed has 62% WR — highest of any day. All-5-days gives best P&L and lowest drawdown |
| Threshold ±1 | Signal direction matters; score magnitude adds no edge |
| 20-lot cap | Uncapped lots hit 100+ late in backtest — unrealistic for liquidity and margin |
| 5% risk per trade | Balances growth with drawdown control |

---

*Backtest period: Sep 2021 – Apr 2026 | Strategy: BankNifty ATM weekly options, intraday, MIS*
