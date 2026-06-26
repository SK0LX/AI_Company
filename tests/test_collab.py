"""Collaboration-layer tests (v2 stage 4): tasks, consent delegation, help.

No pytest / no LLM: run directly with the project venv —

    python tests/test_collab.py

Uses stub decider/picker callables and an in-memory MessageBus, so it exercises
the full state machine deterministically without spending model quota. It does
write rows to data/app.sqlite (tasks/events are append-only logs).
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab
from src.bus import MessageBus
from src.db.engine import get_session
from src.db.models import Delegation, HelpRequest, Task
from src.registry import registry


def _types(task_id: int) -> list[str]:
    return [e["type"] for e in collab.task_events(task_id)]


async def test_delegation_accepted() -> None:
    bus = MessageBus(persist=False)
    tid = collab.create_task("Build the API", created_by="ceo", owner="ceo")

    async def yes(to_agent, task_text, reason):
        return True, "on it"

    accepted, why = await collab.negotiate_delegation(
        task_id=tid, from_agent="ceo", to_agent="developer",
        task_text="Build the API", reason="you own backend", decider=yes, bus=bus,
    )
    assert accepted and why == "on it"
    with get_session() as s:
        task = s.get(Task, tid)
        assert task.status == "in_progress"
        assert task.owner_agent_id == registry.get("developer").id  # reassigned to B
    assert _types(tid) == ["created", "delegated", "accepted"]
    # B got the DELEGATE in its inbox; A got the ACCEPT back.
    assert bus.get_nowait("developer").kind == "DELEGATE"
    assert bus.get_nowait("ceo").kind == "ACCEPT"


async def test_delegation_declined() -> None:
    bus = MessageBus(persist=False)
    tid = collab.create_task("Design the logo", created_by="ceo", owner="ceo")

    async def no(to_agent, task_text, reason):
        return False, "not my area"

    accepted, why = await collab.negotiate_delegation(
        task_id=tid, from_agent="ceo", to_agent="developer",
        task_text="Design the logo", reason="", decider=no, bus=bus,
    )
    assert not accepted and why == "not my area"
    with get_session() as s:
        task = s.get(Task, tid)
        assert task.owner_agent_id == registry.get("ceo").id  # NOT reassigned
    assert _types(tid) == ["created", "delegated", "declined"]
    assert bus.get_nowait("ceo").kind == "DECLINE"


async def test_help_flow() -> None:
    bus = MessageBus(persist=False)
    tid = collab.create_task("Fix CORS", created_by="ceo", owner="frontend")

    async def pick_first(requester, summary, candidates):
        return candidates[0] if candidates else None

    helper = await collab.request_help(
        task_id=tid, requester="frontend", summary="stuck on CORS",
        candidates=["developer", "tester"], picker=pick_first, bus=bus,
    )
    assert helper == "developer"
    from sqlmodel import select
    with get_session() as s:
        hr = s.exec(select(HelpRequest).where(HelpRequest.task_id == tid)
                    .order_by(HelpRequest.id.desc())).first()
        assert hr.status == "assigned" and hr.helper_id == registry.get("developer").id
        hr_id = hr.id
    assert _types(tid)[:3] == ["created", "help_requested", "help_assigned"]
    # resolving moves it to resolved and logs the event
    collab.resolve_help(hr_id, summary="added CORS middleware", actor="developer")
    assert _types(tid)[-1] == "help_resolved"


async def main() -> None:
    registry.setup()
    await test_delegation_accepted()
    await test_delegation_declined()
    await test_help_flow()
    print("collab tests: OK")


if __name__ == "__main__":
    asyncio.run(main())
