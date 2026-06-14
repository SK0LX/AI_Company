"""Integration test for stage-4 wiring in the team graph (no LLM / no quota).

Stubs ``arun_specialist`` and the consent decider so ``_specialist_node`` can be
exercised end-to-end: it should record the delegation hand-off, the result, and
(when a builder saves nothing) a help request — and honor a consent decline.

    python tests/test_graph_collab.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage

from src import collab
from src.graph import team_graph as tg
from src.registry import registry


def _state(role: str, task_id: int, instruction: str = "do the thing") -> dict:
    return {
        "messages": [HumanMessage(content="build a todo app")],
        "findings": [],
        "next_role": role,
        "instruction": instruction,
        "project_dir": "test-proj",
        "structure": "",
        "task_id": task_id,
        "steps": 0,
    }


async def test_auto_accept_records_delegation_and_result() -> None:
    tg.settings.enable_negotiation = False
    tid = collab.create_task("build a todo app", created_by="ceo", owner="ceo")

    async def fake_specialist(role, text, project=""):
        return "Done. Saved backend/main.py."

    tg.arun_specialist = fake_specialist
    out = await tg._specialist_node(_state("developer", tid))
    assert out["last_role"] == "developer" and out["steps"] == 1
    types = [e["type"] for e in collab.task_events(tid)]
    assert "delegated" in types and "accepted" in types and "result" in types


async def test_no_files_opens_help() -> None:
    tg.settings.enable_negotiation = False
    tid = collab.create_task("build a todo app", created_by="ceo", owner="ceo")

    async def empty_specialist(role, text, project=""):
        return "I described the files.\n\n(no files were actually saved!)"

    tg.arun_specialist = empty_specialist
    out = await tg._specialist_node(_state("developer", tid))
    assert "[help]" in out["findings"][-1]  # helper suggestion surfaced to the CEO
    types = [e["type"] for e in collab.task_events(tid)]
    assert "help_requested" in types and "help_assigned" in types


async def test_consent_decline_reroutes() -> None:
    tg.settings.enable_negotiation = True
    tid = collab.create_task("design a logo", created_by="ceo", owner="ceo")

    async def decline(to_agent, task_text, reason):
        return False, "outside my role"

    tg._consent_decider = decline

    async def should_not_run(role, text, project=""):
        raise AssertionError("specialist ran despite a decline")

    tg.arun_specialist = should_not_run
    out = await tg._specialist_node(_state("developer", tid))
    assert "declined" in out["findings"][-1].lower()
    types = [e["type"] for e in collab.task_events(tid)]
    assert "declined" in types and "result" not in types
    tg.settings.enable_negotiation = False  # restore


async def main() -> None:
    registry.setup()
    await test_auto_accept_records_delegation_and_result()
    await test_no_files_opens_help()
    await test_consent_decline_reroutes()
    print("graph-collab integration tests: OK")


if __name__ == "__main__":
    asyncio.run(main())
