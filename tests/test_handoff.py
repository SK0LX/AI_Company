"""The handoff tool + the group work-chain mechanics (analyst → developer → …).
No network, no LLM (the agent turn is simulated).

    python tests/test_handoff.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents import tools as T
from src.config import settings
from src.registry import registry


def _resolve() -> None:
    assert T._resolve_agent("developer") == "developer"
    assert T._resolve_agent("@frontend") == "frontend"
    assert T._resolve_agent("разработчик") == "developer"   # loose match on the label
    assert T._resolve_agent("no-such-role") == ""


def _tool() -> None:
    T.set_current_agent("business_analyst")
    try:
        out = T.handoff.invoke({"to_agent": "developer", "task": "реализуй эндпоинт /react"})
        assert "передал" in out
        assert T._handoffs.get("business_analyst") == ("developer", "реализуй эндпоинт /react")
        # take clears it
        assert T.take_handoff("business_analyst") == ("developer", "реализуй эндпоинт /react")
        assert T.take_handoff("business_analyst") is None
        # guards
        assert "самому себе" in T.handoff.invoke({"to_agent": "business_analyst", "task": "x"})
        assert "не нашёл" in T.handoff.invoke({"to_agent": "ктототам", "task": "x"})
        assert "конкретная задача" in T.handoff.invoke({"to_agent": "developer", "task": "  "})
    finally:
        T.set_current_agent("")
        T.clear_handoff("business_analyst")


def _chain() -> None:
    """The orchestrator loop follows hand-offs across agents and stops at the cap."""
    settings.group_handoff_max_depth = 5

    # a fake agent turn: when it's the analyst's/developer's turn, they hand off once
    plan = {"business_analyst": ("developer", "сделай"), "developer": ("tester", "проверь")}

    def fake_turn(worker: str) -> None:
        T.set_current_agent(worker)
        nxt = plan.get(worker)
        if nxt:
            T.handoff.invoke({"to_agent": nxt[0], "task": nxt[1]})
        T.set_current_agent("")

    visited = []
    worker, depth = "business_analyst", 0
    while worker:
        T.clear_handoff(worker)
        visited.append(worker)
        fake_turn(worker)
        nxt = T.take_handoff(worker)
        if not nxt or depth >= settings.group_handoff_max_depth:
            break
        if nxt[0] == worker:
            break
        worker, depth = nxt[0], depth + 1
    assert visited == ["business_analyst", "developer", "tester"], visited

    # depth cap: everyone hands off forever -> stops after the cap
    settings.group_handoff_max_depth = 2
    ring = ["business_analyst", "developer", "tester", "frontend"]

    def ring_turn(worker: str) -> None:
        T.set_current_agent(worker)
        nxt = ring[(ring.index(worker) + 1) % len(ring)]
        T.handoff.invoke({"to_agent": nxt, "task": "далее"})
        T.set_current_agent("")

    n = 0
    worker, depth = "business_analyst", 0
    while worker and n < 50:
        n += 1
        T.clear_handoff(worker)
        ring_turn(worker)
        nxt = T.take_handoff(worker)
        if not nxt or depth >= settings.group_handoff_max_depth:
            break
        worker, depth = nxt[0], depth + 1
    assert n == settings.group_handoff_max_depth + 1, n  # cap+1 turns then stop


def main() -> None:
    registry.setup()
    d0 = settings.group_handoff_max_depth
    try:
        _resolve()
        _tool()
        _chain()
    finally:
        settings.group_handoff_max_depth = d0
    print("handoff tests: OK")


if __name__ == "__main__":
    main()
