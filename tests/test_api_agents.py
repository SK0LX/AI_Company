"""Integration tests for the agent CRUD API (FastAPI TestClient). No network.

    python tests/test_api_agents.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

SLUG = "_it_agent"


def main() -> None:
    with app_client() as c:
        # seeded roster is served
        agents = c.get("/api/agents").json()
        assert isinstance(agents, list) and any(a["slug"] == "ceo" for a in agents)

        # 404 for missing
        assert c.get("/api/agents/nope_zzz").status_code == 404

        # create
        r = c.post("/api/agents", json={
            "slug": SLUG, "name": "IT agent", "role": "qa",
            "provider": "anthropic", "model": "claude-opus-4-8",
            "api_key": "sk-secret", "system_prompt": "You test.",
            "permissions": {"can_edit_files": "true"}, "obligation": "testing",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == SLUG and body["provider"] == "anthropic"
        assert body["has_api_key"] is True and "api_key" not in body  # key never exposed

        # duplicate -> 409
        assert c.post("/api/agents", json={"slug": SLUG, "name": "dup"}).status_code == 409

        # missing slug -> 422
        assert c.post("/api/agents", json={"name": "no slug"}).status_code == 422

        # get
        got = c.get(f"/api/agents/{SLUG}").json()
        assert got["model"] == "claude-opus-4-8" and got["permissions"]["can_edit_files"] == "true"

        # patch — change provider + add permission
        r = c.patch(f"/api/agents/{SLUG}", json={
            "provider": "openrouter", "permissions": {"can_run_shell": "true"},
        })
        assert r.status_code == 200
        got = c.get(f"/api/agents/{SLUG}").json()
        assert got["provider"] == "openrouter" and got["permissions"] == {"can_run_shell": "true"}
        assert got["has_api_key"] is True  # patch without api_key keeps the existing one

        # patch missing -> 404
        assert c.patch("/api/agents/nope_zzz", json={"name": "x"}).status_code == 404

        # delete
        assert c.delete(f"/api/agents/{SLUG}").status_code == 204
        assert c.get(f"/api/agents/{SLUG}").status_code == 404
        assert c.delete(f"/api/agents/{SLUG}").status_code == 404  # already gone

    print("api agents tests: OK")


if __name__ == "__main__":
    main()
