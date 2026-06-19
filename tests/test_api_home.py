"""Integration tests for the AI-Office home: /api/home, /api/system, and the
enriched task detail. Boots the app via TestClient. No network.

    python tests/test_api_home.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

from src import collab, system


def main() -> None:
    # system snapshot degrades gracefully but always has uptime + something
    s = system.snapshot()
    assert "uptime_seconds" in s and "uptime" in s
    assert ("disk_total" in s) or ("mem_used" in s)

    with app_client() as c:
        t1 = collab.create_task("ut-home-done", created_by="ceo", owner="developer")
        collab.set_task_status(t1, "done", actor="developer")
        t2 = collab.create_task("ut-home-wip", created_by="ceo", owner="developer")
        collab.set_task_status(t2, "in_progress", actor="developer")
        try:
            home = c.get("/api/home").json()
            assert {"closed_today", "team", "workload", "by_status", "active"} <= set(home)
            assert home["closed_today"] >= 1
            dev = next((a for a in home["team"] if a["slug"] == "developer"), None)
            assert dev and dev["status"] == "working" and dev["active_tasks"] >= 1
            ceo = next((a for a in home["team"] if a["slug"] == "ceo"), None)
            assert ceo and ceo["is_lead"] is True
            assert any(w["slug"] == "developer" for w in home["workload"])

            sysj = c.get("/api/system").json()
            assert "uptime" in sysj

            task = c.get(f"/api/tasks/{t1}").json()
            assert task["from_name"] and task["to_name"]
            assert "priority" in task and "complexity" in task
            assert task["closed_at"]  # a done task has a close time
        finally:
            collab.delete_task(t1)
            collab.delete_task(t2)

    print("api home tests: OK")


if __name__ == "__main__":
    main()
