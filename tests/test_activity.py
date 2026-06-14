"""Activity feed + thoughts tests. No LLM.

    python tests/test_activity.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab
from src.registry import registry


def main() -> None:
    registry.setup()

    tid = collab.create_task("activity test", created_by="ceo", owner="ceo")
    collab.record_event(tid, "ceo", "thought", text="delegating to developer", to="developer")
    deleg = collab.open_delegation(tid, "ceo", "developer", reason="build it")
    collab.close_delegation(deleg, "accepted", actor="developer")
    collab.set_task_status(tid, "done", actor="ceo")

    # all: contains both thoughts and task events
    allf = collab.activity_feed("all", limit=200)
    types = {(i["category"], i["type"]) for i in allf}
    assert ("thoughts", "thought") in types
    assert ("tasks", "delegated") in types and ("tasks", "status") in types

    # thoughts only
    thoughts = collab.activity_feed("thoughts", limit=200)
    assert thoughts and all(i["category"] == "thoughts" for i in thoughts)
    assert any("delegating to developer" in (i["text"] or "") for i in thoughts)

    # tasks only — no thoughts
    tasks = collab.activity_feed("tasks", limit=200)
    assert tasks and all(i["category"] == "tasks" for i in tasks)

    # system only — audit entries (created by skill/permission actions earlier)
    system = collab.activity_feed("system", limit=200)
    assert all(i["category"] == "system" for i in system)

    # items are time-sorted, newest first
    ts = [i["ts"] for i in allf]
    assert ts == sorted(ts, reverse=True)

    # a thought carries a verb for the UI
    assert any(i["verb"] for i in thoughts)

    print("activity tests: OK")


if __name__ == "__main__":
    main()
