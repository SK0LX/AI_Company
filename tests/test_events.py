"""Live event hub test (v2 stage 6b). No LLM.

    python tests/test_events.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab
from src.events import hub
from src.registry import registry


async def main() -> None:
    registry.setup()
    q = hub.subscribe()
    try:
        # recording a task event must push a live event to subscribers
        tid = collab.create_task("live test", created_by="ceo", owner="ceo")
        # create_task emits a "created" event
        evt = await asyncio.wait_for(q.get(), timeout=1)
        assert evt["event"] == "task_event" and evt["type"] == "created"
        assert evt["task_id"] == tid

        collab.set_task_status(tid, "done", actor="ceo")
        evt2 = await asyncio.wait_for(q.get(), timeout=1)
        assert evt2["type"] == "status" and evt2["payload"]["status"] == "done"
    finally:
        hub.unsubscribe(q)
    print("events tests: OK")


if __name__ == "__main__":
    asyncio.run(main())
