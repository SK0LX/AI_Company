"""Proactive service + guardrails tests (v2 stage 7). No LLM, no Telegram.

    python tests/test_proactive.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import proactive as P
from src.config import settings
from src.registry import registry


def _event(etype, actor="developer", task_id=1, **payload):
    return {"event": "task_event", "task_id": task_id, "type": etype,
            "actor": actor, "payload": payload}


async def main() -> None:
    registry.setup()
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    svc = P.ProactiveService(sender)

    # 1) disabled by default -> nothing is posted
    settings.enable_proactive = False
    assert await svc.handle(_event("done")) is None and not sent

    # enable globally, grant the agent the proactive permission
    settings.enable_proactive = True
    settings.proactive_min_interval = 0  # don't fight the global cooldown in tests
    from src.db.engine import get_session
    from src.db.models import Agent, AgentPermission
    from sqlmodel import select
    with get_session() as s:
        dev = s.exec(select(Agent).where(Agent.slug == "developer")).first()
        if not s.exec(select(AgentPermission).where(
                AgentPermission.agent_id == dev.id,
                AgentPermission.key == "proactive")).first():
            s.add(AgentPermission(agent_id=dev.id, key="proactive", value="true"))
            s.commit()
    registry.reload()

    # 2) a done event now posts a templated message
    text = await svc.handle(_event("done", task_id=7))
    assert text and "#7" in text and "готова" in text
    assert sent and sent[-1] == text

    # 3) dedup: same event again within TTL is suppressed
    assert await svc.handle(_event("done", task_id=7)) is None

    # 4) per-agent rate limit
    settings.proactive_max_per_window = 2
    svc2 = P.ProactiveService(sender)
    a = await svc2.handle(_event("help_requested", task_id=1, summary="x"))
    b = await svc2.handle(_event("help_requested", task_id=2, summary="y"))
    c = await svc2.handle(_event("help_requested", task_id=3, summary="z"))
    assert a and b and c is None  # third one over the limit of 2

    # 5) an agent WITHOUT the permission stays silent
    none = await svc2.handle(_event("done", actor="designer", task_id=9))
    assert none is None

    # 6) mute() silences everything
    svc3 = P.ProactiveService(sender)
    svc3.mute()
    assert await svc3.handle(_event("done", task_id=11)) is None

    print("proactive tests: OK")


if __name__ == "__main__":
    asyncio.run(main())
