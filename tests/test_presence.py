"""Live office presence + group hand-off graph edges. No network.

    python tests/test_presence.py
"""
from __future__ import annotations

import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab, presence
from src.registry import registry


def _presence() -> None:
    presence.clear_activity("developer")
    assert "developer" not in presence.snapshot()
    presence.set_activity("developer", "working", "пишет код")
    snap = presence.snapshot()
    assert snap["developer"]["status"] == "working"
    assert snap["developer"]["note"] == "пишет код"
    presence.clear_activity("developer")
    assert "developer" not in presence.snapshot()


def _office_live() -> None:
    """office_state folds the live activity into each node (status/note/active)."""
    presence.set_activity("developer", "working", "пишет код")
    try:
        dev = next(n for n in collab.office_state()["nodes"] if n["slug"] == "developer")
        assert dev["status"] == "working" and dev["active"] is True
        assert dev["note"] == "пишет код"
    finally:
        presence.clear_activity("developer")
    # the live note is gone (the node may still be 'busy' from a board task, which
    # is the correct fallback — but the cleared live activity must not linger)
    dev = next(n for n in collab.office_state()["nodes"] if n["slug"] == "developer")
    assert dev["note"] != "пишет код"


def _handoff_edge() -> None:
    """A group hand-off shows up as an interaction-graph edge A→B."""
    from sqlmodel import select

    from src.db.engine import get_session
    from src.db.models import Delegation

    marker = "ut-ho-" + secrets.token_hex(3)
    collab.record_handoff("business_analyst", "developer", marker)
    try:
        edges = collab.interaction_graph()["edges"]
        assert any(e["from"] == "business_analyst" and e["to"] == "developer" for e in edges)
        # self / unknown hand-offs are ignored
        collab.record_handoff("developer", "developer", marker)  # no-op
    finally:
        with get_session() as s:
            for d in s.exec(select(Delegation).where(Delegation.reason == marker)).all():
                s.delete(d)
            s.commit()


def _step_tracer() -> None:
    """Each tool call an agent makes updates its live activity (the 'thought')."""
    import asyncio

    from src.graph.team_graph import StepTracer, _step_note

    presence.clear_activity("developer")
    tracer = StepTracer("developer")
    asyncio.run(tracer.on_tool_start({"name": "repo_tree"}, "https://github.com/x/y"))
    snap = presence.snapshot()
    assert "developer" in snap and "изучает репозиторий" in snap["developer"]["note"]
    presence.clear_activity("developer")
    # label mapping + unknown-tool fallback
    assert "пишет" in _step_note("write_file", "app.py")
    assert "🔧 mystery" in _step_note("mystery", "")


def main() -> None:
    registry.setup()
    _presence()
    _office_live()
    _handoff_edge()
    _step_tracer()
    print("presence tests: OK")


if __name__ == "__main__":
    main()
