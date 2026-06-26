"""The mutating /api is gated when API_TOKEN is set; reads stay open. No network.

    python tests/test_api_auth.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

from src.config import settings


def main() -> None:
    prev = settings.api_token
    settings.api_token = "s3cr3t-operator-token"
    try:
        with app_client() as c:
            body = {"scope": "agent", "scope_id": "developer", "period": "day",
                    "limit_usd": 1.0, "hard_stop": True}

            # a state-changing call WITHOUT the token is rejected (even from localhost)
            assert c.post("/api/budgets", json=body).status_code == 401
            # a wrong token is rejected
            assert c.post("/api/budgets", json=body,
                          headers={"X-Api-Token": "nope"}).status_code == 401
            # the right token passes the auth layer (200/422 — anything but 401)
            assert c.post("/api/budgets", json=body,
                          headers={"X-Api-Token": "s3cr3t-operator-token"}).status_code != 401
            # Bearer form also works
            assert c.post("/api/budgets", json=body,
                          headers={"Authorization": "Bearer s3cr3t-operator-token"}).status_code != 401
            # reads stay open (no token needed)
            assert c.get("/api/budgets").status_code == 200
            # the login endpoint itself is exempt (it validates initData on its own)
            assert c.post("/api/tg/auth", json={"init_data": ""}).status_code != 401

        # with no token configured, local/in-process callers may mutate (dev UX)
        settings.api_token = ""
        with app_client() as c:
            assert c.post("/api/budgets", json=body).status_code != 401
    finally:
        settings.api_token = prev
    print("api auth tests: OK")


if __name__ == "__main__":
    main()
