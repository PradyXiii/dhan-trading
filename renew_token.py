#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
renew_token.py — Unconditional Dhan token renewal.

Renews every time it is called — no timing logic, no token_meta.json.
Run via cron twice daily (7:55 AM and 11:00 PM IST) plus @reboot.
The ~9h and ~15h gaps between runs keep the token well within its 24h expiry.
The @reboot entry covers any VM downtime between the two daily runs.

Cron (installed by setup_automation.sh — do not edit manually):
  25 2  * * *  cd <SCRIPT_DIR> && python3 renew_token.py >> <LOG_DIR>/renew_token.log 2>&1
  30 17 * * *  cd <SCRIPT_DIR> && python3 renew_token.py >> <LOG_DIR>/renew_token.log 2>&1
  @reboot      sleep 30 && cd <SCRIPT_DIR> && python3 renew_token.py >> <LOG_DIR>/renew_token.log 2>&1
"""

import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ENV_PATH  = os.path.join(BASE_DIR, ".env")

IST_OFFSET  = timedelta(hours=5, minutes=30)
MAX_RETRIES = 3

import notify


def _ist_now():
    """Return current time in IST (UTC+5:30) as a formatted string."""
    return (datetime.utcnow() + IST_OFFSET).strftime("%H:%M:%S IST")


def _update_env_token(new_token: str):
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, "r") as f:
        content = f.read()
    new_content = re.sub(
        r"^DHAN_ACCESS_TOKEN=.*$",
        f"DHAN_ACCESS_TOKEN={new_token}",
        content, flags=re.MULTILINE,
    )
    with open(ENV_PATH, "w") as f:
        f.write(new_content)


# ── Credential check ──────────────────────────────────────────────────────────

if not TOKEN or not CLIENT_ID:
    msg = "🚨 Token renewer: credentials missing from .env — manual action needed"
    notify.send(msg)
    print(f"[{_ist_now()}] {msg}")
    sys.exit(1)

# ── Renew token (with retries) ────────────────────────────────────────────────

last_error = ""

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
                print(f"[{_ist_now()}] Token renewed ✓  (attempt {attempt}/{MAX_RETRIES}  .env updated)")
            else:
                print(f"[{_ist_now()}] Token renewal 200 — no new token issued (still valid)")
            sys.exit(0)

        last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
        print(f"[{_ist_now()}] Attempt {attempt}/{MAX_RETRIES} failed — {last_error}")

    except Exception as e:
        last_error = str(e)
        print(f"[{_ist_now()}] Attempt {attempt}/{MAX_RETRIES} exception — {last_error}")

    if attempt < MAX_RETRIES:
        backoff = 2 ** attempt
        print(f"[{_ist_now()}] Retrying in {backoff}s...")
        time.sleep(backoff)

# ── All retries exhausted ─────────────────────────────────────────────────────

msg = (
    f"🚨 Token renewal FAILED after {MAX_RETRIES} attempts — {last_error}\n"
    f"Manual action: regenerate token at dhan.co → API Settings, "
    f"then update DHAN_ACCESS_TOKEN in .env on the VM."
)
notify.send(msg)
print(f"[{_ist_now()}] {msg}")
sys.exit(1)
