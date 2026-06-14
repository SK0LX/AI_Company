"""Integration tests for the board/activity/office API + WebSocket. No network.

    python tests/test_api_board.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client

from src import collab


def main() -> None:
    with app_client() as c:
        # seed a task with a known timeline through the collab layer
        tid = collab.create_task("api board test", created_by="ceo", owner="ceo")
        collab.record_event(tid, "ceo", "thought", text="planning", to="developer")
        deleg = collab.open_delegation(tid, "ceo", "developer", reason="build")
        collab.close_delegation(deleg, "accepted", actor="developer")
        collab.set_task_status(tid, "done", actor="ceo")

        # /api/tasks
        tasks = c.get("/api/tasks").json()
        ours = next(t for t in tasks if t["id"] == tid)
        assert ours["status"] == "done" and ours["owner"] == "developer"

        # /api/tasks/{id} + 404
        assert c.get(f"/api/tasks/{tid}").json()["title"] == "api board test"
        assert c.get("/api/tasks/99999999").status_code == 404

        # /api/tasks/{id}/events
        types = [e["type"] for e in c.get(f"/api/tasks/{tid}/events").json()]
        assert "delegated" in types and "accepted" in types and "thought" in types

        # /api/messages
        assert isinstance(c.get("/api/messages", params={"limit": 5}).json(), list)

        # /api/graph
        g = c.get("/api/graph").json()
        assert {"nodes", "edges"} <= set(g) and len(g["nodes"]) >= 1

        # /api/activity by category
        for cat in ("all", "tasks", "thoughts", "system"):
            items = c.get("/api/activity", params={"category": cat}).json()
            assert isinstance(items, list)
            assert all(i["category"] == cat for i in items) or cat == "all"
        # invalid category falls back to all (200, not error)
        assert c.get("/api/activity", params={"category": "bogus"}).status_code == 200

        # /api/office
        office = c.get("/api/office").json()
        assert {"nodes", "edges"} <= set(office)
        assert any(n["slug"] == "ceo" for n in office["nodes"])

        # WebSocket /ws/events handshake (no events expected synchronously here)
        with c.websocket_connect("/ws/events") as ws:
            ws.close()

    print("api board tests: OK")


if __name__ == "__main__":
    main()
