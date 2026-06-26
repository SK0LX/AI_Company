"""Telegram Mini App initData validation tests. No network.

    python tests/test_tg_auth.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import tg_auth
from src.config import settings

TOKEN = "123456:TEST-bot-token"


def _signed_init_data(user: dict, auth_date: str = "") -> str:
    # Default to "now" so the freshness check passes; tests override to go stale.
    auth_date = auth_date or str(int(time.time()))
    fields = {"auth_date": auth_date, "user": json.dumps(user, separators=(",", ":"))}
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def main() -> None:
    user = {"id": 42, "first_name": "Max", "username": "max"}
    good = _signed_init_data(user)

    # valid signature parses + returns the user
    parsed = tg_auth.validate_init_data(good, TOKEN)
    assert parsed and parsed["user"]["id"] == 42

    # tampered data fails
    tampered = good.replace("Max", "Eve")
    assert tg_auth.validate_init_data(tampered, TOKEN) is None
    # wrong token fails
    assert tg_auth.validate_init_data(good, "999:other") is None
    # empty fails
    assert tg_auth.validate_init_data("", TOKEN) is None

    # anti-replay: a correctly-signed but STALE payload is rejected
    stale = _signed_init_data(user, auth_date=str(int(time.time()) - tg_auth._MAX_AGE_SECONDS - 60))
    assert tg_auth.validate_init_data(stale, TOKEN) is None
    # a payload with no auth_date at all is rejected too
    no_date = _signed_init_data(user, auth_date="0")
    assert tg_auth.validate_init_data(no_date, TOKEN) is None

    # authorize through settings (point bot token + allow-list at our test values)
    settings.telegram_bot_token = TOKEN
    settings.webapp_allowed_user_ids = ""
    assert tg_auth.authenticate(good)["id"] == 42  # any valid user allowed

    settings.webapp_allowed_user_ids = "42, 7"
    assert tg_auth.authenticate(good) is not None  # 42 in allow-list

    settings.webapp_allowed_user_ids = "7, 8"
    assert tg_auth.authenticate(good) is None  # 42 not allowed
    settings.webapp_allowed_user_ids = ""  # restore

    print("tg auth tests: OK")


if __name__ == "__main__":
    main()
