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

        _rate_limit(body)
    finally:
        settings.api_token = prev
    print("api auth tests: OK")


def _rate_limit(body: dict) -> None:
    """Mutating calls are throttled; reads are never rate-limited."""
    from src.web import app as webapp

    orig_max = webapp._RL_MAX
    settings.api_token = ""  # so calls pass auth and actually exercise the limiter
    webapp._RL_MAX = 3
    webapp._rl_hits.clear()
    try:
        with app_client() as c:
            codes = [c.post("/api/budgets", json=body).status_code for _ in range(6)]
            assert codes.count(429) >= 1, f"expected throttling, got {codes}"
            assert c.get("/api/budgets").status_code == 200  # reads exempt
    finally:
        webapp._RL_MAX = orig_max
        webapp._rl_hits.clear()


if __name__ == "__main__":
    main()
