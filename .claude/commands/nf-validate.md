# NF Validate — System Health Check

Run this before any live trade day or after any code change.

## Quick validation (all 3 commands)

```bash
# 1. Confirm entry logic works (no crash, logs intent + legs)
python3 auto_trader.py --dry-run

# 2. Confirm ML composite still meets gate
python3 autoexperiment_nf.py
# → composite must be >= 0.6484

# 3. Confirm today's prediction works
python3 ml_engine.py --predict-today
# → should print CALL or PUT with confidence
```

## Extended validation (after code changes)

```bash
# Full feature analysis — check nothing dropped to 0.000
python3 ml_engine.py --analyze

# NF IC 5yr backtest — confirm P&L not regressed
python3 backtest_spreads.py --instrument NF --strategy nf_iron_condor --ml
# → expect WR ≥ 84%, P&L ≥ ₹1.38Cr

# IC + Bull Put hybrid (all strategies)
python3 backtest_spreads.py --instrument NF --strategy all --ml
```

## Pre-market checklist (9:05 AM IST)

```bash
python3 health_ping.py    # token valid + capital + data freshness
```

## What "passing" looks like

| Check | Pass threshold |
|---|---|
| auto_trader --dry-run | No exception, prints "DRY RUN" + strategy + legs |
| autoexperiment composite | >= 0.6484 |
| ml_engine --predict-today | Prints CALL or PUT (not error) |
| backtest IC WR | >= 84% |
| backtest IC 5yr P&L | >= ₹1.38Cr |

## Current system state (Apr 25 2026)

- ML composite: 0.7071 (champion after Optuna HPO)
- Features: 64 (nf_kalman_trend + 3 HMM probs added Apr 25)
- IC 5yr P&L: ₹1.38Cr (84.7% WR)
- Strategy: IC (CALL days) + Bull Put (PUT days)
- Capital: ₹1,12,370 (go-live Apr 22 2026)
- Live mode: PAPER_MODE = False
