#!/usr/bin/env python3
"""
renew_token.py
──────────────
Standalone token renewal — runs at 8:00 AM IST every day (including weekends).

This is a pure safety net. It has zero dependencies on market data, ML, or
any other part of the trading system. Its only job: keep the Dhan token alive.

Why weekends matter:
  model_evolver and auto_trader only run Mon–Fri.
  Friday 11 PM renewal expires Saturday 11 PM.
  Without this script, Monday 9:15 AM would hit a 34-hour-stale expired token
  and die with 401 before the trade.

Cron (7:55 AM IST = 2:25 AM UTC, every day):
  25 2 * * * cd /path/to/dhan-trading && python3 renew_token.py >> logs/renew_token.log 2>&1

Why 7:55 and not 8:00: the token is renewed at the previous day's 7:55 AM run, making it
expire at 7:55 AM today. Firing at exactly 8:00 AM could hit Dhan's server after expiry.
The 5-minute buffer ensures the token is still alive when the renewal call is made.
"""

import os
import re
import sys
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
ENV_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

import notify

_ts = datetime.now().strftime("%H:%M:%S IST")

if not TOKEN or not CLIENT_ID:
    msg = "🚨 Token renewer: DHAN_ACCESS_TOKEN or CLIENT_ID missing from .env — manual action needed"
    notify.send(msg)
    print(f"[{_ts}] {msg}")
    sys.exit(1)


def _update_env_token(new_token: str) -> None:
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, "r") as f:
        content = f.read()
    new_content = re.sub(
        r"^DHAN_ACCESS_TOKEN=.*$",
        f"DHAN_ACCESS_TOKEN={new_token}",
        content,
        flags=re.MULTILINE,
    )
    with open(ENV_PATH, "w") as f:
        f.write(new_content)


MAX_RETRIES = 3
last_error  = ""

for attempt in range(1, MAX_RETRIES + 1):
    try:
        resp = requests.get(
            "https://api.dhan.co/v2/RenewToken",
            headers={"access-token": TOKEN, "dhanClientId": CLIENT_ID},
            timeout=10,
        )

        if resp.status_code == 200:
            new_token = resp.json().get("token")
            if new_token and new_token != TOKEN:
                _update_env_token(new_token)
                print(f"[{_ts}] Token renewed ✓  (attempt {attempt}/{MAX_RETRIES}  .env updated)")
            else:
                # 200 but same token returned — still valid, no action needed
                print(f"[{_ts}] Token renewal 200 — no new token issued (still valid)")
            sys.exit(0)

        last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
        print(f"[{_ts}] Attempt {attempt}/{MAX_RETRIES} failed — {last_error}")

    except Exception as e:
        last_error = str(e)
        print(f"[{_ts}] Attempt {attempt}/{MAX_RETRIES} exception — {last_error}")

    if attempt < MAX_RETRIES:
        backoff = 2 ** attempt   # 2s, 4s
        print(f"[{_ts}] Retrying in {backoff}s...")
        time.sleep(backoff)

# All retries exhausted
msg = (
    f"🚨 Token renewal FAILED after {MAX_RETRIES} attempts — {last_error}\n"
    f"Manual action needed: regenerate token at dhan.co → API Settings → Access Token\n"
    f"Then update DHAN_ACCESS_TOKEN in .env on the VM."
)
notify.send(msg)
print(f"[{_ts}] {msg}")
sys.exit(1)
