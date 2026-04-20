# Refresh Dhan API Docs

Fetch the latest Dhan API v2 documentation, update `docs/DHAN_API_V2_REFERENCE.md`, then cross-check all code files for inaccuracies.

## Steps

### 1. Fetch all doc pages

Use WebFetch on each of these URLs and capture the full content:

```
https://dhanhq.co/docs/v2/
https://dhanhq.co/docs/v2/authentication/
https://dhanhq.co/docs/v2/orders/
https://dhanhq.co/docs/v2/super-order/
https://dhanhq.co/docs/v2/historical-data/
https://dhanhq.co/docs/v2/option-chain/
https://dhanhq.co/docs/v2/market-quote/
https://dhanhq.co/docs/v2/portfolio/
https://dhanhq.co/docs/v2/positions/
https://dhanhq.co/docs/v2/forever-order/
https://dhanhq.co/docs/v2/margin/
https://dhanhq.co/docs/v2/traders-control/
https://dhanhq.co/docs/v2/live-order-update/
https://dhanhq.co/docs/v2/annexure/
```

### 2. Update docs/DHAN_API_V2_REFERENCE.md

Rewrite the file with everything from the live docs — endpoints, parameters, response fields, error codes, constraints, release notes. Nothing omitted.

### 3. Cross-check code files

Read these files and compare every API call against the fresh docs:

- `auto_trader.py` — check all Dhan API calls: orders, super orders, option chain, expirylist, fundlimit, positions
- `data_fetcher.py` — check all historical data calls (charts/historical, charts/intraday, charts/rollingoption)
- `spread_monitor.py` — check marketfeed/ltp, orders endpoints
- `exit_positions.py` — check positions, orders endpoints
- `renew_token.py` — check RenewToken endpoint
- `lot_expiry_scanner.py` — check any API calls used

For each file, flag:
- Wrong HTTP method (GET vs POST etc)
- Wrong endpoint path
- Missing required fields
- Wrong field names or values
- Wrong headers

### 4. Fix any inaccuracies found

Edit the affected files. Commit all changes. Push to branch `claude/banknifty-options-backtest-JoxCW`.
