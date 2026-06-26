"""Integration tests for the Telegram Mini App auth endpoint. No network.

    python tests/test_api_tg.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

from src.config import settings

TOKEN = "999999:IT-test-token"


def _signed(user: dict) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def main() -> None:
    prev_token = settings.telegram_bot_token
    prev_allow = settings.webapp_allowed_user_ids
    settings.telegram_bot_token = TOKEN
    settings.webapp_allowed_user_ids = ""
    try:
        with app_client() as c:
            # /api/webapp config flag
            assert "enabled" in c.get("/api/webapp").json()

            good = _signed({"id": 7, "first_name": "Max"})

            # valid signature -> ok
            r = c.post("/api/tg/auth", json={"init_data": good})
            assert r.status_code == 200 and r.json()["user"]["id"] == 7

            # forged -> 403
            assert c.post("/api/tg/auth", json={"init_data": good.replace("Max", "Eve")}).status_code == 403
            assert c.post("/api/tg/auth", json={"init_data": ""}).status_code == 403

            # allow-list excludes user 7 -> 403
            settings.webapp_allowed_user_ids = "1,2"
            assert c.post("/api/tg/auth", json={"init_data": good}).status_code == 403
    finally:
        settings.telegram_bot_token = prev_token
        settings.webapp_allowed_user_ids = prev_allow

    print("api tg auth tests: OK")


if __name__ == "__main__":
    main()
