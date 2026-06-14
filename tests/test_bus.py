"""MessageBus unit tests (v2 stage 4).

No pytest dependency: run directly with the project venv —

    python tests/test_bus.py

Uses ``persist=False`` so the bus is exercised without touching the database.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bus import Chat, Delegate, HelpRequest, MessageBus, ORCHESTRATOR


async def test_direct_delivery() -> None:
    bus = MessageBus(persist=False)
    await bus.publish(Delegate(from_agent="ceo", to_agent="developer", task_id=1,
                               reason="build the API"))
    msg = await asyncio.wait_for(bus.receive("developer"), timeout=1)
    assert msg.kind == "DELEGATE" and msg.from_agent == "ceo" and msg.task_id == 1
    # The sender's inbox stays empty.
    assert bus.get_nowait("ceo") is None


async def test_chat_broadcast() -> None:
    bus = MessageBus(persist=False)
    a, b = bus.subscribe_chat(), bus.subscribe_chat()
    await bus.publish(Chat(from_agent="tester", text="tests are green"))
    for q in (a, b):
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg.kind == "CHAT" and msg.text == "tests are green"
    bus.unsubscribe_chat(a)
    await bus.publish(Chat(from_agent="tester", text="second"))
    assert a.empty() and not b.empty()


async def test_unaddressed_goes_to_orchestrator() -> None:
    bus = MessageBus(persist=False)
    await bus.publish(HelpRequest(from_agent="frontend", task_id=7,
                                  summary="stuck on CORS"))
    msg = await asyncio.wait_for(bus.receive(ORCHESTRATOR), timeout=1)
    assert msg.kind == "HELP_REQUEST" and msg.from_agent == "frontend"


async def main() -> None:
    await test_direct_delivery()
    await test_chat_broadcast()
    await test_unaddressed_goes_to_orchestrator()
    print("bus tests: OK")


if __name__ == "__main__":
    asyncio.run(main())
