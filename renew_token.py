#!/usr/bin/env python3
"""
renew_token.py
──────────────
Dynamic token renewal — runs once daily via cron, renews when 23h50m
have elapsed since the last renewal (10-minute buffer before 24h expiry).
@reboot entry handles VM restarts that occur during the renewal window.

Why once daily instead of polling every 5 minutes:
  Polling 288×/day wastes CPU credits on burstable VMs (e2-micro/small).
  A daily cron at 7:55 AM IST fires once; the internal 23h50m check still
  guarantees the token never expires even if a run is slightly delayed.
  The @reboot entry covers the edge case of the VM being down at renewal time.

How it works:
  1. Reads token_meta.json for last_renewed_at timestamp
  2. If now < last_renewed_at + 23h50m  → exits silently (not due yet)
  3. If now >= last_renewed_at + 23h50m → renews token, updates token_meta.json
  4. All three components that write token_meta.json (renew_token.py,
     auto_trader.py, model_evolver.py) share the same clock — whoever
     renewed last resets the 23h50m countdown.

Cron (once daily 7:55 AM IST = 2:25 AM UTC, plus @reboot safety net):
  25 2 * * *  cd ~/dhan-trading && python3 renew_token.py >> logs/renew_token.log 2>&1
  @reboot     sleep 30 && cd ~/dhan-trading && python3 renew_token.py >> logs/renew_token.log 2>&1
"""

import os
import re
import sys
import json
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ENV_PATH  = os.path.join(BASE_DIR, ".env")
META_PATH = os.path.join(BASE_DIR, "token_meta.json")

RENEWAL_INTERVAL = timedelta(hours=23, minutes=50)   # renew 10 min before expiry
IST_OFFSET       = timedelta(hours=5, minutes=30)
MAX_RETRIES      = 3

import notify

def _ist_now():
    """Return current time in IST (UTC+5:30) as a formatted string."""
    return (datetime.utcnow() + IST_OFFSET).strftime("%H:%M:%S IST")

def _to_ist_display(dt):
    """Convert a UTC datetime to IST display string for logs."""
    return (dt + IST_OFFSET).strftime("%d %b %H:%M")


def _read_last_renewed():
    """Return the last renewal datetime, or None if file missing/corrupt."""
    try:
        if os.path.exists(META_PATH):
            with open(META_PATH) as f:
                return datetime.fromisoformat(json.load(f)["last_renewed_at"])
    except Exception:
        pass
    return None


def _write_last_renewed(ts: datetime):
    """Persist renewal timestamp so all components share the same clock."""
    try:
        with open(META_PATH, "w") as f:
            json.dump({"last_renewed_at": ts.isoformat()}, f, indent=2)
    except Exception as e:
        print(f"[{_ist_now()}] Warning: could not write token_meta.json — {e}")


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


# ── Check if renewal is due ───────────────────────────────────────────────────

if not TOKEN or not CLIENT_ID:
    msg = "🚨 Token renewer: credentials missing from .env — manual action needed"
    notify.send(msg)
    print(f"[{_ist_now()}] {msg}")
    sys.exit(1)

now          = datetime.utcnow()   # UTC — consistent with token_meta.json
last_renewed = _read_last_renewed()

if last_renewed is not None:
    elapsed   = now - last_renewed
    remaining = RENEWAL_INTERVAL - elapsed
    if remaining.total_seconds() > 0:
        mins_left = int(remaining.total_seconds() / 60)
        print(f"[{_ist_now()}] Not due — {mins_left} min until next renewal "
              f"(last: {_to_ist_display(last_renewed)})")
        sys.exit(0)
    # else: overdue → fall through and renew

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
                _write_last_renewed(now)
                print(f"[{_ist_now()}] Token renewed ✓  (attempt {attempt}/{MAX_RETRIES}  "
                      f".env + token_meta.json updated  next renewal in 23h50m)")
            else:
                _write_last_renewed(now)
                print(f"[{_ist_now()}] Token renewal 200 — no new token issued (still valid, clock reset)")
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
