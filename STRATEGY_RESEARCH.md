# NF Options Strategy Research — Final Verdict

**Date:** April 2026  
**Data:** Sep 2025–Apr 2026 (Tue expiry regime), 2019–Aug 2025 (Thu expiry regime)  
**Tool:** `backtest_hold_periods.py --regime-report`  
**Trades analyzed:** 1449 signal days across 7 years  

---

## TL;DR — Final Strategy

**Current capital (< ₹2.3L): IC on CALL days + Bull Put on PUT days**  
**When capital ≥ ₹2.3L: Short Straddle (auto-upgrade already coded)**

Run this to reproduce the full research:
```bash
python3 backtest_hold_periods.py --regime-report
python3 backtest_hold_periods.py --regime-report --lots 1
python3 backtest_hold_periods.py --dow-breakdown --start 2025-09-01
```

---

## Full Backtest Results (hold=0d, EOD exit, strategy default lots)

### Thu expiry regime (2019–Aug 2025, 1331 CALL + 884 PUT signal days)

| Strategy | Trades | WR% | Total P&L | Avg/trade |
|---|---|---|---|---|
| Short Straddle | 1331 | 61.9% | +₹2,44,93.4K | ₹18,402 |
| Short Strangle ±150 | 1331 | 61.9% | +₹2,37,69.1K | ₹17,858 |
| BullCall+BullPut | 1331 | 83.5% | +₹33,29.7K | ₹2,502 |
| Bull Put Credit | 447 | 92.4% | +₹13,37.1K | ₹2,991 |
| Bull Call Debit | 884 | 79.1% | +₹19,92.6K | ₹2,254 |
| **IC+BullPut ★** | **1331** | **68.7%** | **+₹15,65.2K** | **₹1,176** |
| Iron Condor (pure) | 1331 | 59.2% | +₹4,37.9K | ₹329 |
| Bear Call Credit | 884 | 13.5% | **-₹22,77.0K** | -₹2,576 |
| Bear Put Debit | 447 | 2.9% | **-₹14,88.1K** | -₹3,329 |
| Naked Call BUY | 884 | 45.8% | **-₹98,32.6K** | -₹11,123 |
| Naked Put BUY | 447 | 4.5% | **-₹1,21,46.6K** | -₹27,174 |
| Long Straddle | 1331 | 30.9% | **-₹2,48,39.1K** | -₹18,662 |
| Long Strangle | 1331 | 30.1% | **-₹2,40,90.6K** | -₹18,100 |

### Tue expiry regime (Sep 2025–Apr 2026, 118 total signal days)

| Strategy | Trades | WR% | Total P&L | Avg/trade |
|---|---|---|---|---|
| Short Straddle | 118 | 94.9% | +₹70,24.3K | ₹59,528 |
| Short Strangle ±150 | 118 | 94.9% | +₹68,72.0K | ₹58,237 |
| **IC+BullPut ★** | **118** | **97.5%** | **+₹27,08.3K** | **₹2,295** |
| BullCall+BullPut | 118 | 85.6% | +₹28,35.9K | ₹2,403 |
| Bull Put Credit | 51 | 100.0% | +₹19,35.5K | ₹3,794 |
| Bull Call Debit | 67 | 74.6% | +₹9,01.0K | ₹1,345 |
| Iron Condor (pure) | 118 | 94.9% | +₹12,17.6K | ₹1,032 |
| Bear Call Credit | 67 | 17.9% | **-₹1,25.9K** | -₹1,879 |
| Bear Put Debit | 51 | 0.0% | **-₹2,16.8K** | -₹4,251 |
| Naked Call BUY | 67 | 4.5% | **-₹41,06.4K** | -₹61,290 |
| Naked Put BUY | 51 | 0.0% | **-₹29,08.4K** | -₹57,028 |
| Long Straddle | 118 | 5.1% | **-₹70,65.3K** | -₹59,875 |
| Long Strangle | 118 | 5.1% | **-₹69,10.7K** | -₹58,566 |

### DOW breakdown — Tue expiry regime (Sep 2025+, hold=0d)

```
Strategy               Mon(DTE1)      Tue(DTE0)      Wed(DTE6)      Thu(DTE5)      Fri(DTE4)
Bull Put Credit        100%  +81.6K   100%  +35.0K   100%  +16.0K   100%  +18.4K   100%  +42.6K
Iron Condor            88.5% +32.7K   89.5% +38.9K   95.8% +14.0K   100%  +15.2K   100%  +20.8K
Bull Call Debit        60%   +9.0K    58.3% +17.9K   94.4% +27.4K   71.4% +11.9K   76.9% +24.0K
Bear Call Credit       40%   -13.7K   33.3% -23.3K   0%    -37.8K   21.4% -20.0K   7.7%  -31.1K
```

IC works on ALL 5 days in Tue-expiry regime. "Wed DTE6 is bad" was stale Thu-expiry data.

---

## Why IC+BullPut beats alternatives

### vs Pure IC
- Pure IC in old regime: 59.2% WR (barely beats coin flip)  
- IC+BullPut in old regime: 68.7% WR  
- Improvement: Bull Put 92% WR on PUT days replaces IC's weaker PUT-day performance  
- In new regime: 97.5% vs 94.9% — marginal but consistent  
- Total 7yr: ₹18.36L vs ₹5.60L — IC+BullPut is 3.3× more profitable

### vs BullCall+BullPut (highest raw P&L)
- BullCall+BullPut earns ₹36.13L (7yr) vs IC+BullPut ₹18.36L  
- BUT: Bull Call Debit = **directional bet**. BUY ATM CE + SELL CE+150. Needs market to go UP.  
- If CALL signal is wrong (market flat/down): full debit lost. Max loss = premium paid.  
- IC on CALL days = **premium seller = market-neutral**. Wins even if market sideways.  
- IC+BullPut survives wrong-signal CALL days. BullCall+BullPut does not.  
- Decision: lower P&L, higher survivability, consistent across regimes.

### vs Short Straddle (highest absolute P&L)
- Short Straddle: ₹3.15Cr over 7 years. Unbeatable P&L.  
- BUT: margin = ₹2.3L/lot. Current capital = ₹1.12L. Cannot run even 1 lot.  
- When capital reaches ₹2.3L: auto-upgrade already coded in `auto_trader.py`.  
- `STRADDLE_MARGIN_PER_LOT = 230_000` — upgrade fires automatically.

---

## Permanently Discarded Strategies

Never revisit these regardless of market conditions or "interesting regime":

| Strategy | Reason | 7yr P&L |
|---|---|---|
| Bear Call Credit | 13.5% WR (old) / 17.9% WR (new). Negative EVERY regime. Signal says CALL → market goes UP → short CE gets hit. Direction conflict. | **-₹24.03L** |
| Bear Put Debit | 2.6% WR across 7 years. Near-zero probability of profit. Needs massive directional move that almost never materializes intraday. | **-₹17.05L** |
| Long Straddle / Strangle | Buying premium = paying theta every minute. 5.1–30.9% WR. Requires enormous moves to overcome premium decay. | **-₹3.19Cr** |
| Naked Call / Put BUY | Zero edge. Theta bleeds, IV crushes. 4–45% WR depending on day. | **-₹1.50Cr** |

---

## Holding Period Analysis

For IC+BullPut (Sep 2025+ regime):

| Hold days | Trades | WR% | Total P&L |
|---|---|---|---|
| **0d (EOD)** | 118 | **97.5%** | **₹2.71L** ← use this |
| 1d | 117 | 82.9% | ₹2.21L |
| 2d | 116 | 79.3% | ₹2.33L |
| 3d | 115 | 76.5% | ₹2.40L |

**EOD exit wins on WR (97.5% → 82.9% drop at 1d hold). P&L slightly higher at 3d hold but WR collapses. Overnight risk, IV risk, gap risk all real. Stay EOD.**

For pure IC (all history), 3d hold adds ₹28K but reduces WR from 94.9% → 80.9%. Not worth it.

---

## Active Configuration (as of Apr 2026)

```
PAPER_MODE          = False         # LIVE
IRON_CONDOR_MODE    = True
IC_SKIP_DAYS        = set()         # no skip — IC profitable all 5 days
SPREAD_WIDTH        = 150           # ATM ± 150pt (3 NF strike steps of 50pt)
CREDIT_SL_FRAC      = 0.50          # SL at 150% of net credit received
LOT_SIZE            = 65            # Jan 6 2026+ (was 75 before)
STRADDLE_MARGIN_PER_LOT = 230_000   # auto-upgrade threshold

# PUT day routing (PENDING CODE CHANGE):
# CALL days: IC — 4 legs, 1 lot, ₹93K margin
# PUT days : Bull Put — 2 legs, 2 lots, ₹102K margin
```

**PENDING:** `auto_trader.py` currently runs IC on ALL days (signal=BOTH). PUT days should run Bull Put (2 legs, 2 lots). Code change required.

---

## Caveats / Known Limitations

1. **BS-model pricing**: backtest uses Black-Scholes with VIX-calibrated IV. Real options have IV compression (premium drops after open even if spot unchanged) and bid-ask slippage. Real-options backtest validation needed before increasing lot sizes.

2. **Bull Put 100% WR suspicion**: on PUT signal days, the model likely has low accuracy (CALL bias → market often goes UP on PUT days → Bull Put is safe). Not a guaranteed edge. The 100% WR reflects regime-specific luck + signal model weakness, not a permanent edge. Could drop in a strongly bearish market.

3. **New regime only 7 months**: Tue-expiry data is 118 trades (Sep 2025–Apr 2026). Not enough history for statistical significance on weekly-expiry-specific patterns.

4. **Straddle numbers are BS-model inflated**: Short Straddle ₹3.15Cr / 7yr is likely overstated. Real-options validation required before scaling straddle.

---

## Reproduction Commands

```bash
# Full regime comparison (all strategies)
python3 backtest_hold_periods.py --regime-report

# Normalize lot-size bias
python3 backtest_hold_periods.py --regime-report --lots 1

# DOW breakdown (Tue expiry only)
python3 backtest_hold_periods.py --dow-breakdown --start 2025-09-01

# Hold period analysis (IC+BullPut, current regime)
python3 backtest_hold_periods.py --strategy nf_hybrid_ic_bullput --start 2025-09-01

# Validate specific day/range
python3 backtest_hold_periods.py --start 2025-09-01 --end 2026-04-23 --dow Mon,Tue

# Real-options validation (run after fetching option cache)
python3 fetch_intraday_options.py --instrument NF --spreads --start 2025-09-01
python3 backtest_spreads.py --instrument NF --strategy nf_iron_condor --ml
```
