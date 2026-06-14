"""End-to-end cross-module flow via the API (TestClient). No network / no LLM.

Exercises several subsystems together: create an agent through the API → it shows
up on the office map → adopt the example skill to it → it appears in that agent's
skills → a collab task + events surface in the board and activity feed.

    python tests/test_e2e_flow.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

from src import collab

SLUG = "_e2e_devops"


def main() -> None:
    with app_client() as c:
        # 1) create an agent on a custom OpenAI-compatible provider
        r = c.post("/api/agents", json={
            "slug": SLUG, "name": "DevOps", "role": "devops",
            "provider": "openai_compatible", "model": "llama-3.3-70b",
            "base_url": "https://api.groq.com/openai/v1", "api_key": "gsk-e2e",
            "permissions": {"can_run_shell": "true"},
        })
        assert r.status_code == 201, r.text
        try:
            # 2) it shows up on the office map
            office = c.get("/api/office").json()
            assert any(n["slug"] == SLUG for n in office["nodes"])

            # 3) adopt the example skill to it (discover first)
            c.post("/api/skills/discover")
            jf = next(s for s in c.get("/api/skills").json() if s["name"] == "json_format")
            assert c.post(f"/api/agents/{SLUG}/skills/{jf['id']}/adopt").status_code == 201

            # 4) the agent now has the skill (adopted)
            mine = c.get(f"/api/agents/{SLUG}/skills").json()
            assert any(s["id"] == jf["id"] and s["adopted_from"] == "developer" for s in mine)

            # 5) a task + delegation to it surfaces on the board and activity feed
            tid = collab.create_task("ship the release", created_by="ceo", owner="ceo")
            deleg = collab.open_delegation(tid, "ceo", SLUG, reason="deploy")
            collab.close_delegation(deleg, "accepted", actor=SLUG)

            tasks = c.get("/api/tasks").json()
            assert any(t["id"] == tid and t["owner"] == SLUG for t in tasks)

            acts = c.get("/api/activity", params={"category": "tasks"}).json()
            assert any(a.get("task_id") == tid and a["type"] == "delegated" for a in acts)

            # graph now has a ceo -> devops edge
            g = c.get("/api/graph").json()
            assert any(e["from"] == "ceo" and e["to"] == SLUG for e in g["edges"])
        finally:
            c.delete(f"/api/agents/{SLUG}")
            # the discover endpoint scaffolds a folder for the temp agent — remove it
            import shutil

            from src.agent_fs import agent_dir

            shutil.rmtree(agent_dir(SLUG), ignore_errors=True)

    print("e2e flow tests: OK")


if __name__ == "__main__":
    main()
