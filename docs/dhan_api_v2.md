# Dhan API v2 — Complete Reference
*Source: dhanhq.co/docs/v2 + DhanHQ-py SDK | Updated April 2026*

---

## Base URL & Auth Headers

```
Base URL:  https://api.dhan.co/v2

Headers (all REST calls):
  access-token:   <your_access_token>
  client-id:      <your_client_id>
  Content-Type:   application/json
```

`dhanClientId` must also be included in every POST/PUT request body.

---

## Rate Limits

| API Group | Per Second | Per Minute | Per Hour | Per Day |
|---|---|---|---|---|
| Order APIs | 10 | 250 | 1,000 | 7,000 |
| Data APIs | 5 | — | — | 100,000 |
| Quote APIs | 1 | Unlimited | Unlimited | Unlimited |
| Non-Trading | 20 | Unlimited | Unlimited | Unlimited |
| Option Chain | 1 per 3 seconds | — | — | — |

Order modifications capped at 25 per order.

---

## Authentication

### Individual Trader — Manual Token
- Login → web.dhan.co → My Profile → Access DhanHQ APIs → Generate Access Token
- Valid for **24 hours**

### Renew Token (extends active token by 24h)
```
GET https://api.dhan.co/v2/RenewToken
Headers: access-token, dhanClientId
```
Only works on non-expired tokens. Returns new token.

### Generate Token via API (requires TOTP)
```
POST https://auth.dhan.co/app/generateAccessToken
Query params: dhanClientId, pin, totp
```

### OAuth Flow (App/API Key — 12 months)
1. `POST https://auth.dhan.co/app/generate-consent?client_id={id}` → `consentAppId`
2. Browser: `https://auth.dhan.co/login/consentApp-login?consentAppId={id}` → redirects with `tokenId`
3. `GET https://auth.dhan.co/app/consumeApp-consent?tokenId={id}` → `accessToken`

### Token Validity Check
```
GET https://api.dhan.co/v2/profile
```
Response: `dhanClientId`, `tokenValidity`, `activeSegment`, `ddpi`, `mtf`, `dataPlan`, `dataValidity`

### Static IP (mandatory for order APIs from Apr 1 2026)
```
POST  /ip/setIP      body: { "ip": "x.x.x.x", "ipFlag": "PRIMARY"|"SECONDARY" }
PUT   /ip/modifyIP   (cannot modify within 7 days of last set)
GET   /ip/getIP
```

---

## Orders

**From Apr 1 2026:** All order placement/modification/cancellation must come from whitelisted static IP.
**From Mar 21 2026:** Market orders converted to LIMIT with MPP. Rate limit 10/sec.

| Method | Endpoint | Action |
|---|---|---|
| POST | /orders | Place order |
| PUT | /orders/{order-id} | Modify pending order |
| DELETE | /orders/{order-id} | Cancel pending order |
| POST | /orders/slicing | Place slice order (large qty) |
| GET | /orders | All orders today |
| GET | /orders/{order-id} | Single order status |
| GET | /orders/external/{correlation-id} | Order by correlation ID |
| GET | /trades | All trades today |
| GET | /trades/{order-id} | Trades for one order |
| GET | /trades/{from}/{to}/{page} | Paginated trade history |

### Place Order — POST /orders

**Request body:**
```json
{
  "dhanClientId":      "string",
  "correlationId":     "string",
  "transactionType":   "BUY|SELL",
  "exchangeSegment":   "NSE_FNO|NSE_EQ|BSE_EQ|...",
  "productType":       "CNC|INTRADAY|MARGIN|MTF",
  "orderType":         "LIMIT|MARKET|STOP_LOSS|STOP_LOSS_MARKET",
  "validity":          "DAY|IOC",
  "securityId":        "string",
  "quantity":          0,
  "disclosedQuantity": 0,
  "price":             0.0,
  "triggerPrice":      0.0,
  "afterMarketOrder":  false,
  "amoTime":           "OPEN|OPEN_30|OPEN_60"
}
```

- `amoTime` only required when `afterMarketOrder: true`. Must be one of the 3 values above.
- `correlationId` is optional (your own tag for tracking)

**Response:** `{ "orderId": "string", "orderStatus": "TRANSIT" }`

### Order Status Values
`TRANSIT` → `PENDING` → `PART_TRADED` → `TRADED`
Also: `REJECTED`, `CANCELLED`, `EXPIRED`
Super Order only: `CLOSED` (both entry and exit placed), `TRIGGERED` (SL or TP leg hit)

### Modify Order — PUT /orders/{order-id}
```json
{
  "orderId": "string",
  "orderType": "LIMIT|MARKET|STOP_LOSS|STOP_LOSS_MARKET",
  "legName": "ENTRY_LEG|TARGET_LEG|STOP_LOSS_LEG",
  "quantity": 0,
  "price": 0.0,
  "disclosedQuantity": 0,
  "triggerPrice": 0.0,
  "validity": "DAY|IOC"
}
```
Can only modify when status is `PENDING` or `PART_TRADED`.

### Trade Book — GET /trades/{from}/{to}/{page}
Paginated. Includes cost breakdown fields: `sebiTax`, `stt`, `brokerageCharges`, `serviceTax`, `exchangeTransactionCharges`, `stampDuty`.

---

## Super Orders

Entry + Target + Stop-Loss in a single API call. Trailing SL supported.

| Method | Endpoint | Action |
|---|---|---|
| POST | /super/orders | Create super order |
| PUT | /super/orders/{order-id} | Modify a leg |
| DELETE | /super/orders/{order-id}/{leg} | Cancel leg or all |
| GET | /super/orders | All super orders |

### Place Super Order — POST /super/orders

**Request body (exact fields — no extras):**
```json
{
  "dhanClientId":    "string",
  "correlationId":   "string",
  "transactionType": "BUY|SELL",
  "exchangeSegment": "NSE_FNO|NSE_EQ|...",
  "productType":     "CNC|INTRADAY|MARGIN",
  "orderType":       "LIMIT|MARKET",
  "securityId":      "string",
  "quantity":        0,
  "price":           0.0,
  "targetPrice":     0.0,
  "stopLossPrice":   0.0,
  "trailingJump":    0.0
}
```

**Super orders do NOT support:** `validity`, `disclosedQuantity`, `afterMarketOrder`, `amoTime`.

**Validation:**
- At least one of `targetPrice` or `stopLossPrice` must be > 0
- BUY: `targetPrice > price` AND `stopLossPrice < price`
- SELL: `targetPrice < price` AND `stopLossPrice > price`
- `trailingJump`: 0 = no trailing SL

**Leg rules:**
- `ENTRY_LEG`: modifiable only when status is `PENDING` or `PART_TRADED`
- `TARGET_LEG` / `STOP_LOSS_LEG`: modifiable after entry is `TRADED` (price + trailingJump only)
- Once a leg is individually cancelled, it cannot be re-added

### Modify Super Order — PUT /super/orders/{order-id}

ENTRY_LEG: `{ "orderId", "legName": "ENTRY_LEG", "orderType", "quantity", "price", "targetPrice", "stopLossPrice", "trailingJump" }`

TARGET_LEG: `{ "orderId", "legName": "TARGET_LEG", "targetPrice" }`

STOP_LOSS_LEG: `{ "orderId", "legName": "STOP_LOSS_LEG", "stopLossPrice", "trailingJump" }`

### Cancel Super Order — DELETE /super/orders/{order-id}/{leg}
`leg` values: `ENTRY_LEG`, `TARGET_LEG`, `STOP_LOSS_LEG`
Cancelling by `orderId` without leg cancels all legs.

---

## Portfolio & Positions

| Method | Endpoint | Action |
|---|---|---|
| GET | /holdings | All holdings in demat |
| GET | /positions | Open positions today |
| POST | /positions/convert | Convert intraday ↔ delivery |
| DELETE | /positions | Exit all open positions |

### Positions Response (key fields)
```json
{
  "securityId": "string",
  "positionType": "LONG|SHORT",
  "exchangeSegment": "NSE_FNO|...",
  "productType": "INTRADAY|MARGIN|...",
  "buyAvg": 0.0,
  "buyQty": 0,
  "sellAvg": 0.0,
  "sellQty": 0,
  "netQty": 0,
  "costPrice": 0.0,
  "realizedProfit": 0.0,
  "unrealizedProfit": 0.0,
  "drvExpiryDate": "string",
  "drvOptionType": "CALL|PUT",
  "drvStrikePrice": 0.0
}
```

---

## Funds & Margin

### Fund Limit — GET /fundlimit

```json
{
  "dhanClientId": "string",
  "availabelBalance": 0.0,
  "sodLimit": 0.0,
  "collateralAmount": 0.0,
  "receiveableAmount": 0.0,
  "utilizedAmount": 0.0,
  "blockedPayoutAmount": 0.0,
  "withdrawableBalance": 0.0
}
```
**Note:** Field is `availabelBalance` — that's a typo in the Dhan API itself, not our code.

### Margin Calculator — POST /margincalculator
```json
{
  "dhanClientId": "string",
  "securityId": "string",
  "exchangeSegment": "NSE_FNO|...",
  "transactionType": "BUY|SELL",
  "quantity": 0,
  "productType": "CNC|INTRADAY|...",
  "price": 0.0,
  "triggerPrice": 0.0
}
```
Response: `totalMargin`, `spanMargin`, `exposureMargin`, `availableBalance`, `brokerage`, `leverage`

---

## Historical Data

### Daily OHLCV — POST /charts/historical
```json
{
  "dhanClientId": "string",
  "securityId": "string",
  "exchangeSegment": "IDX_I|NSE_EQ|NSE_FNO|...",
  "instrument": "INDEX|EQUITY|FUTIDX|OPTIDX|...",
  "expiryCode": 0,
  "oi": false,
  "fromDate": "YYYY-MM-DD",
  "toDate": "YYYY-MM-DD"
}
```
`expiryCode` valid values: `0` (current), `1` (next), `2` (far), `3`

**Response:** arrays of `open`, `high`, `low`, `close`, `volume`, `timestamp` (Unix epoch seconds)

### Intraday OHLCV — POST /charts/intraday
```json
{
  "dhanClientId": "string",
  "securityId": "string",
  "exchangeSegment": "IDX_I|NSE_FNO|...",
  "instrument": "INDEX|OPTIDX|...",
  "interval": 15,
  "oi": false,
  "fromDate": "YYYY-MM-DD",
  "toDate": "YYYY-MM-DD"
}
```
`interval` valid values (int): `1`, `5`, `15`, `25`, `60`
Max date range: 75 days. Data available for last 5 years.

### Expired Options (Rolling) — POST /charts/rollingoption
```json
{
  "dhanClientId": "string",
  "securityId": 25,
  "exchangeSegment": "NSE_FNO",
  "instrument": "OPTIDX",
  "expiryFlag": "WEEK|MONTH",
  "expiryCode": 1,
  "strike": "ATM",
  "drvOptionType": "CALL|PUT",
  "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
  "fromDate": "YYYY-MM-DD",
  "toDate": "YYYY-MM-DD",
  "interval": 15
}
```
- `interval` valid values (int): `1`, `5`, `15`, `25`, `60`
- `expiryCode` valid values: `0`, `1`, `2`, `3`
- `strike`: `ATM`, `ATM+1` to `ATM+10`, `ATM-1` to `ATM-10`
- **Max 30 days per call.** Chunk into 28-day windows for multi-year fetches.
- **Quirk:** `expiryCode: 0` treated as missing by API — use `1` for current/nearest expiry.

**Response:**
```json
{
  "data": {
    "ce": { "open": [...], "close": [...], "iv": [...], "strike": [...], "spot": [...], "timestamp": [...] },
    "pe": null
  }
}
```

---

## Option Chain

Rate limit: **1 unique request per 3 seconds**

### Expiry List — POST /optionchain/expirylist
```json
{ "UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I" }
```
**Response:** `{ "data": ["YYYY-MM-DD", "YYYY-MM-DD", ...] }` — nearest first

Always call this first before calling `/optionchain`.

### Option Chain — POST /optionchain
```json
{ "UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": "YYYY-MM-DD" }
```
Note: these three fields use **PascalCase** — different from all other Dhan endpoints.
`UnderlyingScrip` is an **int** (not string).

**Response:**
```json
{
  "data": {
    "last_price": 55500.0,
    "oc": {
      "55500.000000": {
        "ce": {
          "security_id": 42528,
          "implied_volatility": 9.789,
          "last_price": 134,
          "average_price": 146.99,
          "oi": 3786445,
          "previous_close_price": 244.85,
          "previous_oi": 402220,
          "volume": 117567970,
          "top_bid_price": 133.55,
          "top_ask_price": 134,
          "greeks": { "delta": 0.539, "theta": -15.15, "gamma": 0.00132, "vega": 12.19 }
        },
        "pe": { "...same fields..." }
      }
    }
  }
}
```
Strike keys are float-strings: `"55500.000000"`. `security_id` is an int.

---

## Market Quote

Snapshot for up to 1000 instruments. Rate limit: 1 req/sec.
Headers required: `access-token`, `client-id`

| Method | Endpoint | Data |
|---|---|---|
| POST | /marketfeed/ltp | LTP only |
| POST | /marketfeed/ohlc | OHLC + LTP |
| POST | /marketfeed/quote | Full depth + OHLC + OI |

**Request:** `{ "NSE_FNO": [49081, 49082], "IDX_I": [13, 25] }`

Full quote response includes: `last_price`, `ohlc`, `volume`, `oi`, `oi_day_high`, `oi_day_low`, 5-level `depth`, `upper_circuit_limit`, `lower_circuit_limit`, `average_price` (VWAP)

---

## Trader's Control

```
POST  /killswitch?killSwitchStatus=ACTIVATE     body: {}
POST  /killswitch?killSwitchStatus=DEACTIVATE   body: {}
GET   /killswitch
```
Kill switch disables all trading for the day. All positions must be closed first. Resets next trading day.
`killSwitchStatus` is a query parameter, not in the body.

### P&L Based Exit
```
POST   /pnlExit   body: { "profitValue": 1500.0, "lossValue": 500.0, "productType": ["INTRADAY"], "enableKillSwitch": true }
DELETE /pnlExit
GET    /pnlExit
```

---

## Forever Orders (GTT)

Good Till Triggered — persist across sessions.

| Method | Endpoint | Action |
|---|---|---|
| POST | /forever/orders | Create |
| PUT | /forever/orders/{id} | Modify |
| DELETE | /forever/orders/{id} | Cancel |
| GET | /forever/orders | List all |

`orderFlag`: `SINGLE` or `OCO` (One Cancels Other)
OCO extra fields: `price1`, `triggerPrice1`, `quantity1` (stop-loss leg)

---

## Live Order Update (WebSocket)

```
wss://api-order-update.dhan.co
```

**Auth after connect:**
```json
{
  "LoginReq": { "MsgCode": 42, "ClientId": "string", "Token": "JWT" },
  "UserType": "SELF"
}
```

---

## Market Feed WebSocket

```
wss://api-feed.dhan.co?version=2&token={token}&clientId={id}&authType=2
```

**Subscription message:**
```json
{
  "RequestCode": 15,
  "InstrumentCount": 1,
  "InstrumentList": [{ "ExchangeSegment": "NSE_FNO", "SecurityId": "token_string" }]
}
```

Request codes: `15` Ticker, `17` Quote, `21` Full (5-level depth), `23` FullDepth (20/200 level)
Unsubscribe = subscribe code + 1. Disconnect: `{ "RequestCode": 12 }`
Max 100 instruments per subscription batch.

**Binary packet first byte:** `2` Ticker, `3` Depth, `4` Quote, `5` OI, `8` Full, `50` Server disconnect

**Disconnect error codes:** `805` Too many connections, `806` Not subscribed, `807` Token expired, `808` Invalid client, `809` Auth failed

### Full Market Depth WebSocket
- 20-level: `wss://depth-api-feed.dhan.co/twentydepth?token=...&clientId=...&authType=2` (up to 50 instruments)
- 200-level: `wss://full-depth-api.dhan.co/?token=...&clientId=...&authType=2` (1 instrument only)
- Both support NSE_EQ and NSE_FNO only.

---

## Security Master

Compact CSV: `https://images.dhan.co/api-data/api-scrip-master.csv`
Detailed CSV: `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`

---

## Annexure

### Exchange Segments
| Enum | Description | Numeric |
|---|---|---|
| `IDX_I` | Index | 0 |
| `NSE_EQ` | NSE Equity | 1 |
| `NSE_FNO` | NSE F&O | 2 |
| `NSE_CURRENCY` | NSE Currency | 3 |
| `BSE_EQ` | BSE Equity | 4 |
| `MCX_COMM` | MCX Commodity | 5 |
| `BSE_CURRENCY` | BSE Currency | 7 |
| `BSE_FNO` | BSE F&O | 8 |

Numeric codes used in WebSocket only.

### Product Types
| Enum | Description |
|---|---|
| `CNC` | Cash & Carry (delivery) |
| `INTRADAY` | Intraday — auto squared off EOD |
| `MARGIN` | Carry Forward F&O — held if SL/TP not triggered |
| `MTF` | Margin Trade Funding |

### Instrument Types
`INDEX`, `EQUITY`, `FUTIDX`, `OPTIDX`, `FUTSTK`, `OPTSTK`, `FUTCOM`, `OPTFUT`, `FUTCUR`, `OPTCUR`

### Key Security IDs
| ID | Instrument |
|---|---|
| 13 | Nifty 50 |
| 25 | BankNifty |

### Error Codes — Trading API
| Code | Meaning |
|---|---|
| DH-901 | Token invalid or expired |
| DH-902 | Data API not subscribed / no Trading API access |
| DH-903 | Segment not activated |
| DH-904 | Rate limit exceeded |
| DH-905 | Missing/bad parameter values (also: weekend/holiday, no data) |
| DH-906 | Incorrect order request — includes market-closed rejections |
| DH-907 | Incorrect parameters or no data available |
| DH-908 | Internal server error |
| DH-909 | Network/backend failure |
| DH-910 | Miscellaneous |
| DH-911 | Request from non-whitelisted IP |

### Error Codes — Data API
| Code | Meaning |
|---|---|
| 800 | Internal server error |
| 804 | Instruments exceed limit |
| 805 | Too many requests |
| 806 | Data APIs not subscribed |
| 807 | Access token expired |
| 808 | ClientID or token invalid |
| 809 | Access token invalid |
| 810 | Client ID invalid |
| 811 | Invalid expiry date |
| 812 | Invalid date format |
| 813 | Invalid SecurityId |
| 814 | Invalid request |

### Standard Response Envelope
```json
{
  "status": "success|failure",
  "remarks": "" | { "error_code": "DH-9xx", "error_type": "...", "error_message": "..." },
  "data": {}
}
```

---

## Release Notes

### v2.5.1 — Mar 17 2026
- Market orders via API converted to LIMIT with MPP (effective Mar 21)
- Order rate limits → 10/sec (effective Mar 21)
- Static IP mandatory for all order APIs (effective Apr 1)

### v2.5 — Feb 09 2026
- New: Conditional Trigger Orders
- New: P&L Based Exit under Trader's Control
- New: Exit All Positions API
- Improved: Option Chain API enhancements

### v2.4
- New: Full Market Depth WebSocket (20-level and 200-level)

### v2.3
- New: Expired Options Data API (`/charts/rollingoption`)

### v2.2
- New: Super Order API
- New: Forever Order (GTT) API

### v2.1
- New: Option Chain API
- New: Live Order Update WebSocket

### v2.0
- Complete rewrite. New base URL `api.dhan.co/v2/`

---

*Full docs: https://dhanhq.co/docs/v2/*
