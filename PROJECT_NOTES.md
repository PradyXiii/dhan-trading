# BankNifty Auto-Trader — Full Project Notes

Complete record of the design process, decisions, experiments, and findings.
Written as a reference for future work.

---

## What We Built

A fully automated options trading system for BankNifty weekly ATM options on the NSE.

- Runs on a **GCP VM** (us-central1, IP 34.45.55.132, whitelisted on Dhan)
- **No human input during market hours** — cron fires at 9:15 AM IST, trade placed, Telegram sent
- **Backtested on 4.5 years** of daily data (Sep 2021 – Apr 2026)
- **Final result**: ₹30K starting capital + ₹10K/month → ₹1.51 Cr in 4.5 years (26× return)

---

## Architecture Overview

```
data_fetcher.py
  └─ Dhan API  → banknifty.csv, nifty50.csv
  └─ yfinance  → india_vix.csv, sp500.csv, nikkei.csv, sp500_futures.csv

signal_engine.py
  └─ Merges all sources
  └─ Computes 10 indicators (4 active, 6 inactive audit trail)
  └─ Outputs signals.csv  (CALL / PUT / NONE for each trading day)

backtest_engine.py
  └─ Reads signals.csv + banknifty.csv
  └─ Simulates trades with capital management + full Dhan cost model
  └─ Outputs trade_log.csv + equity_curve.csv

auto_trader.py  [cron: 45 3 * * 1-5]
  └─ Runs data_fetcher + signal_engine
  └─ Reads today's signal
  └─ Calls Dhan option chain API → ATM security_id
  └─ Sizes the trade (capital × 5% risk ÷ SL × LOT_SIZE)
  └─ Places Dhan Super Order (entry + SL + TP in one call)
  └─ Sends 2-message Telegram summary

dhan_mcp.py  [Claude Code MCP server]
  └─ Exposes get_positions, get_orders, get_daily_pnl, get_funds
  └─ Ask Claude: "What's today's P&L?" → live Dhan API response
```

---

## Experiments Run (in order)

### 1. Initial setup — Dhan API + data fetch
- Connected to Dhan v2 API for BankNifty and Nifty50 daily OHLCV
- Discovered **timezone bug**: Dhan timestamps are midnight IST, parsed as UTC → dates shifted 1 day earlier
- Fix: add IST offset (+5h30m) when parsing Dhan timestamps
- Impact: all runs before Run 7 used wrong dates ("Wednesday" data was actually Thursday, etc.)

### 2. Signal engine — 10 indicators
Started with 8 core indicators:
1. EMA20 (price vs 20-day moving average)
2. RSI14 (momentum oscillator)
3. 5-day trend (recent directional momentum)
4. VIX direction (fear gauge direction)
5. S&P500 previous-day change
6. Nikkei 225 previous-day change
7. S&P500 futures gap (overnight global signal)
8. BN-NF divergence (BankNifty vs Nifty relative performance)

Later added: HV20 (historical volatility), BN overnight gap = total 10.

### 3. Round 2 indicators — all failed
Tested: PCR, OI direction, Max Pain, FII net F&O, IV Rank.
- All 5 degraded performance (WR dropped 50.7% → 47%)
- Root cause: wrong time horizon (weekly/monthly data for intraday trades)
- Kept data files; removed from scoring

### 4. Day combinations tested
| Config | Net P&L | Max DD |
|---|---|---|
| Mon–Fri all 5 | ₹98.5L | −23.2% |
| Mon+Tue+Thu+Fri | ₹86.9L | −27.4% |
| Thu+Tue | ₹40.8L | −33.3% |
| Wed alone | ₹5.85L | −20.7% |

**Key finding:** More days = better P&L AND lower drawdown (diversification).
Wednesday (0 DTE) has highest WR (62%) of any day — gamma signals are cleaner.

### 5. Straddle vs Directional Long
Tested: always straddle, straddle on signal days, directional long.
- BankNifty average daily range: ~586 points
- Straddle breakeven: Mon 1131pts, Tue 800pts, Thu 1960pts, Fri 1789pts
- **Result**: Directional ₹98.7L vs Straddle **−₹5.4L loss**
- Straddle is structurally broken for this setup (only Wed at 400pts breakeven is viable, barely)

### 6. Entry timing + slippage
Tested 9:15 / 9:20 / 9:25 / 9:30 AM entry using 5-minute candle data.
- Difference between best and worst: ₹248/trade — statistical noise
- Friday's 9-trade sample dominated variance → not meaningful
- **Decision**: keep 9:15 AM. Earlier = better fill on high-liquidity open
- Slippage sensitivity: strategy survives 1% (₹81L). Degrades at 3%+. Real ATM spread ~0.5–1.5%.

### 7. Indicator attribution — the key experiment
Tested each of 10 indicators individually, then in combinations.

**Individual P&L contribution (positive = adds value):**
| Indicator | Role |
|---|---|
| 5-day trend | **Best performer** — captures medium-term momentum |
| BN-NF divergence | **2nd best** — relative strength signal |
| EMA20 | Positive |
| VIX direction | Positive |
| RSI14 | Marginally positive |
| HV20 | Marginally positive |
| S&P500 change | **Negative** — US market direction ≠ BN direction |
| Nikkei change | **Negative** |
| S&P500 futures gap | **Negative** |
| BN overnight gap | **Negative** (proxy for GIFT Nifty) |

**Why macro signals hurt:**
BankNifty has high correlation with Indian macro/RBI/FII flows, not with S&P500 on any given day. On days when US went up but BN went down (and vice versa), the macro signal overrode the correct India-specific signal.

**Combination results:**
| Config | Trades | WR | Net P&L |
|---|---|---|---|
| All 10 (baseline) | 990 | 50.5% | ₹1.00 Cr |
| No macro (5 India-only) | ~895 | 51.8% | ₹1.30 Cr |
| Top 2: trend5 + BN-NF | 585 | 55.6% | ₹1.65 Cr |
| **Top 4: +EMA20 +VIX** | **756** | **54.6%** | **₹1.50 Cr** |
| Top 5: +RSI14 | 834 | 53.3% | ₹1.40 Cr |

**Selected: Top 4** over Top 2 because:
- 756 trades vs 585 → more statistical confidence, less overfitting risk
- Only 10% less P&L (₹1.50 Cr vs ₹1.65 Cr)
- Smoother equity curve with more trades

### 8. GIFT Nifty / SGX Nifty as signal source
User asked: is GIFT Nifty better than US close as a signal?
- GIFT Nifty = SGX Nifty = BN overnight gap (all capture same thing: overnight move into India open)
- None of the overnight/gap signals improved performance vs India-only technical signals
- **Conclusion**: India-specific technicals beat global pre-market signals for intraday BN direction

---

## Final Configuration (Run 8 — Live)

### Signal Engine
- **4 active indicators**: EMA20, 5-day trend, VIX direction, BN-NF divergence
- Score range: −4 to +4
- Trade threshold: ±1 (any net directional signal = trade)
- Event filter: RBI MPC + Budget days = forced NO TRADE

### Trade Rules
| | Value |
|---|---|
| Lot size | 30 |
| Max lots | 20 |
| Risk per trade | 5% of capital |
| Stop-loss | 30% of premium |
| RR (Thu/Fri) | 2.0× |
| RR (Mon) | 1.6× |
| RR (Tue) | 1.4× |
| RR (Wed) | 1.0× |
| Premium formula | spot × 0.004 × √DTE |

### Capital Model
- Starting: ₹30,000
- Monthly top-up: ₹10,000 at first trade of each new month
- Lots = floor(capital × 0.05 ÷ (LOT_SIZE × premium × 0.30))

### Backtest Final Numbers
```
Net P&L         : ₹1.45 Cr
Win rate        : 54.6%
Total trades    : 892
Trades/year     : ~198
Max drawdown    : −24.9%
Transaction costs: ₹3.15L (2.1% of gross P&L)
Injected capital: ₹5.8L
Ending capital  : ₹1.51 Cr
26× return in 4.5 years
```

---

## Live Trading Setup

### Infrastructure
- **VM**: GCP us-central1 (Iowa), e2-micro or similar, ~$5/month
  - Region doesn't matter for trading — VM is UTC, cron converts to IST
  - IP 34.45.55.132 whitelisted on Dhan API
- **Cron**: `45 3 * * 1-5` = 3:45 AM UTC = 9:15 AM IST

### Daily Routine
1. **4–5 PM IST**: Get new Dhan token from dhan.co → API settings
2. Update `~/dhan-trading/.env` → `DHAN_ACCESS_TOKEN=eyJ...`
3. Run `python3 test_connection.py` to verify
4. Run `python3 data_fetcher.py && python3 signal_engine.py` to refresh signals
5. **Next morning at 9:15 AM IST**: cron fires automatically

### Token auto-refresh (optional)
If TOTP is available, a separate `refresh_token.py` script (using DHAN_TOTP_SECRET) can automate step 1. Already installed at `/home/pradeeshr_r9/dhan/refresh_token.py`, runs at 1 AM UTC (6:30 AM IST).

### Telegram Notifications
On trade days, 2 Telegram messages:
1. **Trade details**: signal, capital, option symbol, spot, premium, DTE, SL, TP, risk/reward
2. **Order result**: order ID (live) or "Dry Run Complete" (test mode)

On no-trade days, 1 message: reason (score = 0, event day, etc.)

---

## Known Limitations

1. **Estimated premium** — real ATM premiums vary with IV crush/spike. High-IV days have expensive premiums not fully captured by the formula.
2. **Daily OHLCV exit simulation** — real intraday SL/TP hits are approximated. Actual outcomes may differ.
3. **20-lot cap** — arbitrary constraint; real cap depends on margin and liquidity.
4. **Thursday edge is thin** — 44% WR vs 33.3% breakeven = only 10.7% margin. Could flip in live trading.
5. **Compounding dominates late equity** — large lots in year 4–5 drive most of the P&L. Earlier years are smaller.
6. **No slippage modeled in main backtest** — sensitivity test showed survival at 1%, degradation at 3%+.
7. **Token expires every 24h** — requires daily manual (or scripted) refresh.

---

## Possible Future Improvements

- **IV-adjusted premium**: use India VIX to scale premium estimate dynamically
- **Wednesday-only SL tightening**: on 0 DTE gamma, 30% SL may be too wide; test 20%
- **Thursday skip**: thin edge (44% WR) — monitor first 6 months live, consider removing
- **Post-trade monitoring**: use 5-min intraday data to trail SL after 50% target hit
- **Multi-broker support**: add fallback to Angel One API if Dhan order fails
- **Real-time P&L dashboard**: extend dhan_mcp.py with a simple Streamlit web view

---

## Files and What Each Does

| File | Purpose |
|---|---|
| `data_fetcher.py` | Fetches all market data in 90-day chunks from Dhan + yfinance |
| `signal_engine.py` | Computes 10 indicators, applies threshold, generates signals.csv |
| `backtest_engine.py` | Full simulation with capital mgmt + Dhan cost model |
| `auto_trader.py` | Morning cron script — data → signal → order → Telegram |
| `notify.py` | Telegram helper; `send()` for Telegram, `log()` for console-only |
| `test_connection.py` | Quick Dhan API token validation |
| `setup_automation.sh` | One-shot setup: install deps, create cron, run dry-run |
| `dhan_mcp.py` | MCP server for Claude Code — query live positions/P&L |
| `indicator_attribution.py` | Research: per-indicator P&L contribution analysis |
| `strangle_backtest.py` | Research: straddle vs directional long comparison |
| `timing_backtest.py` | Research: entry timing (9:15–9:30) + slippage sensitivity |
| `fetch_intraday.py` | Research: 5-min BankNifty candles from Dhan (last 90 days) |
| `fetch_round2_data.py` | Research: NSE bhavcopy, participant OI (PCR, FII etc.) |
| `BACKTEST_LOG.md` | Full run history, methodology, all decisions documented |
| `PROJECT_NOTES.md` | This file — deep dive into all experiments and reasoning |
| `data/` | All CSV data files (not committed to GitHub — VM only) |

---

## Dhan API Reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/v2/charts/historical` | POST | Daily OHLCV |
| `/v2/charts/intraday` | POST | 5-min intraday candles (~90 day retention) |
| `/v2/optionchain` | POST | Live option chain with security_id, LTP |
| `/v2/super-order` | POST | Entry + SL + TP in one API call |
| `/v2/orders` | GET/POST | Order book + place orders |
| `/v2/positions` | GET | Open positions + unrealized P&L |
| `/v2/tradeBook` | GET | Executed trades |
| `/v2/fundlimit` | GET | Available margin / fund details |

**Auth**: Headers `access-token` + `client-id` (token expires every 24h)

---

## Quick Reference — Run These to Restart from Scratch

```bash
# On GCP VM
cd ~/dhan-trading
git pull origin claude/banknifty-options-backtest-JoxCW

# Update token
nano .env                           # update DHAN_ACCESS_TOKEN

# Refresh data + signal
python3 data_fetcher.py
python3 signal_engine.py

# Backtest (optional — data already validated)
python3 backtest_engine.py

# Test dry run
python3 auto_trader.py --dry-run

# Verify cron
crontab -l
```

---

*Last updated: Apr 2026 — Project complete. Live trading begins when funds added to Dhan account.*
