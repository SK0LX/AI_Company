"""Integration tests for the proactive status/mute API. No network.

    python tests/test_api_proactive.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client


def main() -> None:
    with app_client() as c:
        st = c.get("/api/proactive").json()
        assert set(st) >= {"enabled", "muted", "team_chat_id"}

        # mute -> unmute toggle
        assert c.post("/api/proactive/mute", json={"muted": True}).json()["muted"] is True
        assert c.get("/api/proactive").json()["muted"] is True
        assert c.post("/api/proactive/mute", json={"muted": False}).json()["muted"] is False
        assert c.get("/api/proactive").json()["muted"] is False

    print("api proactive tests: OK")


if __name__ == "__main__":
    main()
