"""Unit tests for the agent → Telegram outbox + the `say` tool. No network
(the poster is a stub).

    python tests/test_outbox.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import outbox
from src.agents import tools as T
from src.config import settings
from src.registry import registry


def _autoreply() -> None:
    """The addressed agent auto-replies, bounded by the chain-depth cap + the gate."""
    from sqlmodel import select

    from src.db.engine import get_session
    from src.db.models import Message

    fe_id = registry.get("frontend").id

    def fe_replies(mk: str) -> list:
        with get_session() as s:
            return [r for r in s.exec(select(Message).where(Message.kind == "CHAT")).all()
                    if mk in (r.text or "") and r.from_agent_id == fe_id]

    settings.enable_agent_chat = True
    settings.agent_chat_max_depth = 2
    settings.team_chat_id = 999001
    mk = "ut-ar-" + secrets.token_hex(3)

    async def yes(slug: str, transcript: str) -> tuple:
        return (True, f"{mk} {slug} принял")

    async def poster(s: str, c: int, t: str) -> None:
        pass

    svc = outbox.OutboxService(poster, decider=yes)

    def trigger(depth: int) -> None:
        mid = outbox.enqueue_say("developer", f"@frontend {mk} нужен X", depth=depth)
        with get_session() as s:
            m = s.get(Message, mid)
        asyncio.run(svc._maybe_autoreply("developer", m, 999001))

    # below the cap -> frontend auto-replies at depth+1
    n0 = len(fe_replies(mk))
    trigger(0)
    r = fe_replies(mk)
    assert len(r) == n0 + 1, "addressed agent did not auto-reply"
    assert outbox._depth_of(r[-1]) == 1

    # at the cap -> no further reply (loop guard)
    n1 = len(fe_replies(mk))
    trigger(settings.agent_chat_max_depth)
    assert len(fe_replies(mk)) == n1, "depth cap did not stop the chain"

    # gate off -> no reply at all
    settings.enable_agent_chat = False
    n2 = len(fe_replies(mk))
    trigger(0)
    assert len(fe_replies(mk)) == n2, "auto-reply fired while disabled"


def main() -> None:
    registry.setup()
    ch0 = settings.team_chat_id
    ac0, depth0 = settings.enable_agent_chat, settings.agent_chat_max_depth
    settings.team_chat_id = 999000
    try:
        marker = "ut-say-" + secrets.token_hex(3)

        # the `say` tool enqueues a chat message from the current agent
        T.set_current_agent("developer")
        assert "отправлено" in T.say.invoke({"text": f"@frontend {marker} нужен endpoint"})
        assert "пустое" in T.say.invoke({"text": "   "})  # empty -> no-op
        T.set_current_agent("")

        # the outbox delivers it via a stub poster (as the from-agent, to the chat)
        delivered: list[tuple] = []

        async def poster(slug: str, chat_id: int, text: str) -> None:
            delivered.append((slug, chat_id, text))

        svc = outbox.OutboxService(poster)
        asyncio.run(svc.drain())
        mine = [d for d in delivered if marker in d[2]]
        assert mine, "message was not delivered"
        assert mine[0][0] == "developer" and mine[0][1] == 999000  # from developer, to team chat

        # a second drain does NOT re-deliver (it was marked sent)
        delivered.clear()
        asyncio.run(svc.drain())
        assert not any(marker in d[2] for d in delivered), "message re-delivered"

        _autoreply()
    finally:
        settings.team_chat_id = ch0
        settings.enable_agent_chat, settings.agent_chat_max_depth = ac0, depth0
    print("outbox tests: OK")


if __name__ == "__main__":
    main()
