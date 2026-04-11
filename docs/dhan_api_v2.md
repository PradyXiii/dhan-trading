# DHAN API v2 — COMPLETE REFERENCE
*Source: https://dhanhq.co/docs/v2/ | Compiled April 2026*

---

## TABLE OF CONTENTS
1. [Introduction](#introduction)
2. [Authentication](#authentication)
3. [Orders](#orders)
4. [Super Order](#super-order)
5. [Historical Data](#historical-data)
6. [Expired Options Data](#expired-options-data)
7. [Option Chain](#option-chain)
8. [Market Quote](#market-quote)
9. [Annexure](#annexure)

---

## INTRODUCTION

DhanHQ API is a REST-based platform for building trading and investment services. All requests accept JSON. Responses are JSON. Auth via access token in header.

**Base URL:** `https://api.dhan.co/v2/`

**Standard request format:**
```
curl --request POST \
  --url https://api.dhan.co/v2/ \
  --header 'Content-Type: application/json' \
  --header 'access-token: JWT' \
  --data '{Request JSON}'
```

**Python SDK install:**
```bash
pip install dhanhq
```
```python
from dhanhq import dhanhq
dhan = dhanhq("client_id", "access_token")
```

### Error Response Structure
```json
{
  "errorType": "",
  "errorCode": "",
  "errorMessage": ""
}
```

### Rate Limits

| | Order APIs | Data APIs | Quote APIs | Non-Trading APIs |
|---|---|---|---|---|
| Per second | 10 | 5 | 1 | 20 |
| Per minute | 250 | — | Unlimited | Unlimited |
| Per hour | 1000 | — | Unlimited | Unlimited |
| Per day | 7000 | 100,000 | Unlimited | Unlimited |

Order modifications capped at 25 per order.

---

## AUTHENTICATION

### Individual Traders — Two Methods

#### Method 1: Access Token (Manual)
- Login to web.dhan.co → My Profile → Access DhanHQ APIs → Generate Access Token
- Valid for **24 hours**
- Can include Postback URL for order updates

#### Generate Token via API (requires TOTP enabled)
```
POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=1000000001&pin=111111&totp=000000
```

**Query Parameters:**
| Field | Description |
|---|---|
| dhanClientId | Your Dhan Client ID |
| pin | 6-digit Dhan PIN |
| totp | 6-digit TOTP code (from authenticator) |

**Response:**
```json
{
  "dhanClientId": "1000000401",
  "dhanClientName": "JOHN DOE",
  "dhanClientUcc": "ABCD12345E",
  "givenPowerOfAttorney": false,
  "accessToken": "eyJ...",
  "expiryTime": "2026-01-01T00:00:00.000"
}
```

#### Renew Token (extends by 24 hours)
```
curl --location 'https://api.dhan.co/v2/RenewToken' \
  --header 'access-token: {JWT Token}' \
  --header 'dhanClientId: {Client ID}'
```
Note: Only works on active (non-expired) tokens.

---

#### Method 2: API Key & Secret (OAuth flow — 3 steps)
API key valid for **12 months**. Still requires browser-based login in Step 2.

**Step 1 — Generate Consent:**
```
POST https://auth.dhan.co/app/generate-consent?client_id={dhanClientId}
Headers: app_id, app_secret
```
Response: `consentAppId`

**Step 2 — Browser Login:**
```
https://auth.dhan.co/login/consentApp-login?consentAppId={consentAppId}
```
Redirects to your URL with `tokenId` appended.

**Step 3 — Consume Consent:**
```
GET https://auth.dhan.co/app/consumeApp-consent?tokenId={Token ID}
Headers: app_id, app_secret
```
Response: `accessToken` (same structure as above)

---

### Static IP Setup
Mandatory for **Order Placement APIs only** (not required for Data APIs).
- Set primary + secondary IP
- Cannot modify for 7 days after setting
- IPv4 and IPv6 supported

```
POST https://api.dhan.co/v2/ip/setIP
Body: { "dhanClientId": "...", "ip": "10.x.x.x", "ipFlag": "PRIMARY" }
```

```
PUT  https://api.dhan.co/v2/ip/modifyIP
GET  https://api.dhan.co/v2/ip/getIP
```

---

### Setup TOTP
1. Dhan Web → DhanHQ Trading APIs → Setup TOTP
2. Scan QR or enter secret into authenticator app
3. Confirm with first TOTP code

TOTP = 6-digit code generated every 30 seconds from shared secret (RFC 6238).

---

### User Profile (Token Validation)
```
GET https://api.dhan.co/v2/profile
Header: access-token: {JWT}
```

**Response:**
```json
{
  "dhanClientId": "1100003626",
  "tokenValidity": "30/03/2025 15:37",
  "activeSegment": "Equity, Derivative, Currency, Commodity",
  "ddpi": "Active",
  "mtf": "Active",
  "dataPlan": "Active",
  "dataValidity": "2024-12-05 09:37:52.0"
}
```

---

## ORDERS

**Static IP required for all order placement, modification, cancellation.**
**Effective Mar 21 2026:** Market orders converted to LIMIT with MPP. Rate limit 10/sec.
**Effective Apr 1 2026:** All order API calls must come from whitelisted static IP.

| Method | Endpoint | Action |
|---|---|---|
| POST | /orders | Place new order |
| PUT | /orders/{order-id} | Modify pending order |
| DELETE | /orders/{order-id} | Cancel pending order |
| POST | /orders/slicing | Slice order over freeze limit |
| GET | /orders | All orders for the day |
| GET | /orders/{order-id} | Status of specific order |
| GET | /orders/external/{correlation-id} | Status by correlation ID |
| GET | /trades | All trades for the day |
| GET | /trades/{order-id} | Trades for specific order |

### Order Placement
```
POST https://api.dhan.co/v2/orders
```

**Request:**
```json
{
  "dhanClientId": "1000000003",
  "correlationId": "123abc678",
  "transactionType": "BUY",
  "exchangeSegment": "NSE_FNO",
  "productType": "INTRADAY",
  "orderType": "LIMIT",
  "validity": "DAY",
  "securityId": "11536",
  "quantity": 1,
  "price": 150.0,
  "triggerPrice": "",
  "afterMarketOrder": false,
  "amoTime": ""
}
```

**Key Parameters:**
| Field | Values |
|---|---|
| transactionType | `BUY`, `SELL` |
| productType | `CNC`, `INTRADAY`, `MARGIN`, `MTF` |
| orderType | `LIMIT`, `MARKET`, `STOP_LOSS`, `STOP_LOSS_MARKET` |
| validity | `DAY`, `IOC` |
| amoTime | `PRE_OPEN`, `OPEN`, `OPEN_30`, `OPEN_60` (only if afterMarketOrder=true) |

**Response:** `{ "orderId": "112111182198", "orderStatus": "PENDING" }`

### Order Modification
```
PUT https://api.dhan.co/v2/orders/{order-id}
```
Can modify: price, quantity, orderType, validity. Pass only fields to change plus required dhanClientId and orderId.

### Order Cancellation
```
DELETE https://api.dhan.co/v2/orders/{order-id}
```
No request body. Returns `202 Accepted` on success.

### Order Slicing
```
POST https://api.dhan.co/v2/orders/slicing
```
Same body as Order Placement. Use when quantity exceeds exchange freeze limit. Dhan splits into multiple orders automatically.

### Order Book
```
GET https://api.dhan.co/v2/orders
```
Returns all orders for the current trading day including status, filled qty, traded price.

### Trade Book
```
GET https://api.dhan.co/v2/trades
GET https://api.dhan.co/v2/trades/{order-id}
```
Returns executed trades. Per-order version gives fill details for a specific order.

Order Status values: `TRANSIT`, `PENDING`, `PART_TRADED`, `TRADED`, `REJECTED`, `CANCELLED`, `EXPIRED`

---

## SUPER ORDER

Entry + Target + Stop Loss in a single API call. Supports all segments. Optional trailing stop loss.

| Method | Endpoint | Action |
|---|---|---|
| POST | /super/orders | Create super order |
| PUT | /super/orders/{order-id} | Modify super order leg |
| DELETE | /super/orders/{order-id}/{order-leg} | Cancel order leg |
| GET | /super/orders | List all super orders |

### Place Super Order
```
POST https://api.dhan.co/v2/super/orders
```

**Request:**
```json
{
  "dhanClientId": "1000000003",
  "transactionType": "BUY",
  "exchangeSegment": "NSE_FNO",
  "productType": "INTRADAY",
  "orderType": "LIMIT",
  "securityId": "11536",
  "quantity": 1,
  "price": 150,
  "targetPrice": 225,
  "stopLossPrice": 105,
  "trailingJump": 0
}
```

- `trailingJump`: Set to 0 for no trailing stop loss
- `orderType`: Only `LIMIT` or `MARKET`

### Modify Super Order Legs
Three leg types:
- `ENTRY_LEG` — modifies entire order (only when PENDING or PART_TRADED)
- `TARGET_LEG` — modify target price only
- `STOP_LOSS_LEG` — modify SL price and trailing jump

### Cancel Super Order
```
DELETE /super/orders/{order-id}/{order-leg}
```
`order-leg` values: `ENTRY_LEG`, `TARGET_LEG`, `STOP_LOSS_LEG`, `ALL`

---

## HISTORICAL DATA

| Method | Endpoint | Action |
|---|---|---|
| POST | /charts/historical | Daily OHLCV candles |
| POST | /charts/intraday | Intraday OHLCV candles |

### Daily Historical Data
```
POST https://api.dhan.co/v2/charts/historical
```

**Request:**
```json
{
  "securityId": "13",
  "exchangeSegment": "IDX_I",
  "instrument": "INDEX",
  "expiryCode": 0,
  "oi": false,
  "fromDate": "2021-09-01",
  "toDate": "2026-04-01"
}
```

**Key fields:**
| Field | Values |
|---|---|
| securityId | 13 = Nifty 50, 25 = BankNifty |
| exchangeSegment | `IDX_I` for indices, `NSE_EQ` for equity, `NSE_FNO` for F&O |
| instrument | `INDEX`, `EQUITY`, `FUTIDX`, `OPTIDX` etc |
| expiryCode | 0 = current, 1 = next, 2 = far |

**Response:**
```json
{
  "open": [3978, 3856, ...],
  "high": [3978, 3925, ...],
  "low":  [3861, 3856, ...],
  "close": [3879, 3915, ...],
  "volume": [3937092, 1906106, ...],
  "timestamp": [1326220200, 1326306600, ...]
}
```
Timestamps are Unix epoch (seconds). Convert: `new Date(ts * 1000)`

---

### Intraday Historical Data
```
POST https://api.dhan.co/v2/charts/intraday
```

**Request:**
```json
{
  "securityId": "13",
  "exchangeSegment": "IDX_I",
  "instrument": "INDEX",
  "interval": "15",
  "oi": false,
  "fromDate": "2024-09-11 09:30:00",
  "toDate": "2024-09-15 13:00:00"
}
```

- `interval` values: `1`, `5`, `15`, `25`, `60` (minutes)
- Data available for last **5 years**
- Max date range per call: **75 days** for intraday

---

## EXPIRED OPTIONS DATA

Pre-processed historical options data on a rolling basis. ATM ±10 strikes for index options, ATM ±3 for others. Minute-level data, up to 5 years.

| Method | Endpoint | Action |
|---|---|---|
| POST | /charts/rollingoption | Get expired options data |

```
POST https://api.dhan.co/v2/charts/rollingoption
```

**Request:**
```json
{
  "exchangeSegment": "NSE_FNO",
  "interval": "15",
  "securityId": 25,
  "instrument": "OPTIDX",
  "expiryFlag": "WEEK",
  "expiryCode": 1,
  "strike": "ATM",
  "drvOptionType": "CALL",
  "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
  "fromDate": "2024-01-01",
  "toDate": "2024-01-31"
}
```

**Key Parameters:**
| Field | Values |
|---|---|
| securityId | 25 = BankNifty, 13 = Nifty |
| expiryFlag | `WEEK` or `MONTH` |
| expiryCode | 0 = current, 1 = next, 2 = far |
| strike | `ATM`, `ATM+1`, `ATM-1`, ..., `ATM+10`, `ATM-10` |
| drvOptionType | `CALL` or `PUT` |
| requiredData | Any combo: `open` `high` `low` `close` `iv` `volume` `strike` `oi` `spot` |

**Max 30 days per API call.** For 5-year backtest, chunk into 28-day windows.

**Response:**
```json
{
  "data": {
    "ce": {
      "open": [354, 360.3, ...],
      "high": [...],
      "low": [...],
      "close": [...],
      "iv": [...],
      "volume": [...],
      "strike": [...],
      "spot": [...],
      "timestamp": [1756698300, 1756699200, ...]
    },
    "pe": null
  }
}
```

---

## OPTION CHAIN

Real-time option chain for any underlying. OI, Greeks, IV, Volume, Bid/Ask for all strikes.

**Rate limit: 1 unique request per 3 seconds.**

| Method | Endpoint | Action |
|---|---|---|
| POST | /optionchain | Get full option chain |
| POST | /optionchain/expirylist | Get all active expiry dates |

### Headers Required (both endpoints)
```
access-token: {JWT}
client-id: {dhanClientId}
```

### Option Chain
```
POST https://api.dhan.co/v2/optionchain
```

**Request:**
```json
{
  "UnderlyingScrip": 25,
  "UnderlyingSeg": "IDX_I",
  "Expiry": "2026-04-17"
}
```

**Response structure:**
```json
{
  "data": {
    "last_price": 55500.0,
    "oc": {
      "55500.000000": {
        "ce": {
          "average_price": 146.99,
          "greeks": {
            "delta": 0.53871,
            "theta": -15.1539,
            "gamma": 0.00132,
            "vega": 12.18593
          },
          "implied_volatility": 9.789,
          "last_price": 134,
          "oi": 3786445,
          "previous_close_price": 244.85,
          "previous_oi": 402220,
          "security_id": 42528,
          "top_ask_price": 134,
          "top_bid_price": 133.55,
          "volume": 117567970
        },
        "pe": { ... }
      }
    }
  }
}
```

**Computing PCR from response:**
```python
total_ce_oi = sum(strike['ce']['oi'] for strike in oc.values() if strike.get('ce'))
total_pe_oi = sum(strike['pe']['oi'] for strike in oc.values() if strike.get('pe'))
pcr = total_pe_oi / total_ce_oi
```

**Computing ATM IV:**
```python
spot = data['last_price']
atm_strike = round(spot / 100) * 100  # for BankNifty use /100, Nifty use /50
atm_data = oc[str(float(atm_strike))]
atm_iv = (atm_data['ce']['implied_volatility'] + atm_data['pe']['implied_volatility']) / 2
```

---

### Expiry List
```
POST https://api.dhan.co/v2/optionchain/expirylist
```

**Request:**
```json
{
  "UnderlyingScrip": 25,
  "UnderlyingSeg": "IDX_I"
}
```

**Response:**
```json
{
  "data": ["2026-04-17", "2026-04-24", "2026-04-30", ...]
}
```

Always call this first to get the correct expiry date before calling optionchain.

---

## MARKET QUOTE

Snapshot data for up to 1000 instruments per request. Rate limit: 1 request/second.

| Method | Endpoint | Action |
|---|---|---|
| POST | /marketfeed/ltp | LTP only |
| POST | /marketfeed/ohlc | OHLC + LTP |
| POST | /marketfeed/quote | Full depth + OHLC + OI |

**Headers required:** `access-token`, `client-id`

### LTP Request
```json
{ "NSE_FNO": [49081, 49082], "IDX_I": [13, 25] }
```

### Full Quote Response includes:
- `last_price`, `ohlc`, `volume`, `oi`, `oi_day_high`, `oi_day_low`
- `depth` (5-level bid/ask)
- `upper_circuit_limit`, `lower_circuit_limit`
- `average_price` (VWAP)

---

## ANNEXURE

### Exchange Segments
| Enum | Exchange | Segment | Numeric |
|---|---|---|---|
| `IDX_I` | Index | Index Value | 0 |
| `NSE_EQ` | NSE | Equity Cash | 1 |
| `NSE_FNO` | NSE | Futures & Options | 2 |
| `NSE_CURRENCY` | NSE | Currency | 3 |
| `BSE_EQ` | BSE | Equity Cash | 4 |
| `MCX_COMM` | MCX | Commodity | 5 |
| `BSE_CURRENCY` | BSE | Currency | 7 |
| `BSE_FNO` | BSE | Futures & Options | 8 |

### Instrument Types
| Enum | Description |
|---|---|
| `INDEX` | Index |
| `FUTIDX` | Index Futures |
| `OPTIDX` | Index Options |
| `EQUITY` | Equity |
| `FUTSTK` | Stock Futures |
| `OPTSTK` | Stock Options |
| `FUTCOM` | Commodity Futures |
| `OPTFUT` | Options on Commodity Futures |
| `FUTCUR` | Currency Futures |
| `OPTCUR` | Currency Options |

### Product Types
| Enum | Description |
|---|---|
| `CNC` | Cash & Carry (equity delivery) |
| `INTRADAY` | Intraday (equity, F&O) — auto-squared off EOD |
| `MARGIN` | Carry Forward F&O — position held if SL/TP not triggered |
| `MTF` | Margin Trade Funding |

### Order Status
| Status | Meaning |
|---|---|
| `TRANSIT` | Did not reach exchange |
| `PENDING` | Awaiting execution |
| `PART_TRADED` | Partially filled |
| `TRADED` | Fully executed |
| `REJECTED` | Rejected by broker/exchange |
| `CANCELLED` | Cancelled by user |
| `EXPIRED` | Validity expired |
| `CLOSED` | Super Order — both entry and exit placed |
| `TRIGGERED` | Super Order — target or SL leg triggered |

### Expiry Code
| Code | Meaning |
|---|---|
| 0 | Current/Near Expiry |
| 1 | Next Expiry |
| 2 | Far Expiry |

### Trading API Errors
| Code | Type | Message |
|---|---|---|
| DH-901 | Invalid Auth | Token invalid or expired |
| DH-902 | Invalid Access | Data API not subscribed or no Trading API access |
| DH-903 | User Account | Segment not activated or account requirement not met |
| DH-904 | Rate Limit | Too many requests — throttle API calls |
| DH-905 | Input Exception | Missing required fields or bad parameter values |
| DH-906 | Order Error | Incorrect order request — cannot be processed (includes market-closed rejections) |
| DH-907 | Data Error | Incorrect parameters or no data available |
| DH-908 | Internal Server Error | Rare server-side failure |
| DH-909 | Network Error | Backend communication failure |
| DH-910 | Others | Miscellaneous errors |
| DH-911 | Invalid IP | Request from non-whitelisted IP — static IP not whitelisted |

### Data API Errors
| Code | Description |
|---|---|
| 800 | Internal Server Error |
| 804 | Instruments exceed limit |
| 805 | Too many requests — may result in block |
| 806 | Data APIs not subscribed |
| 807 | Access token expired |
| 808 | Auth failed — ClientID or token invalid |
| 809 | Access token invalid |
| 810 | Client ID invalid |
| 811 | Invalid Expiry Date |
| 812 | Invalid Date Format |
| 813 | Invalid SecurityId |
| 814 | Invalid Request |

### Key Security IDs
| ID | Instrument |
|---|---|
| 13 | Nifty 50 Index |
| 25 | Bank Nifty Index |

---

## PORTFOLIO AND POSITIONS

| Method | Endpoint | Action |
|---|---|---|
| GET | /holdings | All holdings in demat |
| GET | /positions | Open positions for the day |
| POST | /positions/convert | Convert intraday ↔ delivery |
| DELETE | /positions | Exit all open positions |

### Positions Response (key fields)
```json
{
  "securityId": "11536",
  "positionType": "LONG",
  "exchangeSegment": "NSE_FNO",
  "productType": "INTRADAY",
  "buyAvg": 150.0,
  "buyQty": 1,
  "netQty": 1,
  "unrealizedProfit": 1200.0,
  "drvExpiryDate": "2026-04-17",
  "drvOptionType": "CALL",
  "drvStrikePrice": 55000.0
}
```

---

## TRADER'S CONTROL

| Method | Endpoint | Action |
|---|---|---|
| POST | /killswitch?killSwitchStatus=ACTIVATE | Activate kill switch (disables all trading) |
| POST | /killswitch?killSwitchStatus=DEACTIVATE | Deactivate kill switch |
| GET | /killswitch | Get kill switch status |
| POST | /pnlExit | Configure P&L based auto-exit |
| DELETE | /pnlExit | Stop P&L based exit |
| GET | /pnlExit | Get current P&L exit config |

**Kill Switch:** Disables all trading for the day. All positions must be closed first. Resets next trading day.

**P&L Based Exit request:**
```json
{
  "profitValue": 1500.00,
  "lossValue": 500.00,
  "productType": ["INTRADAY"],
  "enableKillSwitch": true
}
```

---

## FOREVER ORDER (GTT)

Good Till Triggered orders — persist across sessions until triggered or cancelled. Two types: `SINGLE` and `OCO` (One Cancels Other).

| Method | Endpoint | Action |
|---|---|---|
| POST | /forever/orders | Create forever order |
| PUT | /forever/orders/{order-id} | Modify forever order |
| DELETE | /forever/orders/{order-id} | Cancel forever order |
| GET | /forever/orders | Get all forever orders |

---

## MARGIN CALCULATOR

| Method | Endpoint | Action |
|---|---|---|
| POST | /margincalculator | Margin for single order |
| POST | /margincalculator/multi | Margin for multiple orders |
| GET | /fundlimit | Available fund limits |

### Fund Limit Response fields
`availabelBalance` (Dhan typo), `sodLimit`, `collateralAmount`, `receiveableAmount`, `utilizedAmount`, `blockedPayoutAmount`, `withdrawableBalance`

---

## LIVE ORDER UPDATE (WEBSOCKET)

Real-time order status updates. JSON messages (not binary).

```
wss://api-order-update.dhan.co
```

**Auth after connecting:**
```json
{
  "LoginReq": { "MsgCode": 42, "ClientId": "1000000001", "Token": "JWT" },
  "UserType": "SELF"
}
```

---

## RELEASES

### v2.5.1 — Mar 17 2026
- Market orders via API converted to LIMIT with MPP (effective Mar 21)
- Order rate limits reduced to 10/sec (effective Mar 21)
- Static IP mandatory for all order APIs (effective Apr 1)

### v2.5 — Feb 09 2026
- New: Conditional Trigger Orders (price + technical indicator based)
- New: P&L Based Exit under Trader's Control
- New: Exit All Positions API
- Improved: Option Chain API enhancements

### v2.4
- New: Full Market Depth (20-level and 200-level WebSocket)

### v2.3
- New: Expired Options Data API (`/charts/rollingoption`)

### v2.2
- New: Super Order API
- New: Forever Order (GTT) API

### v2.1
- New: Option Chain API (`/optionchain` and `/optionchain/expirylist`)
- New: Live Order Update WebSocket

### v2.0
- Complete rewrite from v1. New base URL `api.dhan.co/v2/`

---

*Full docs: https://dhanhq.co/docs/v2/*
