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


def main() -> None:
    registry.setup()
    ch0 = settings.team_chat_id
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
    finally:
        settings.team_chat_id = ch0
    print("outbox tests: OK")


if __name__ == "__main__":
    main()
