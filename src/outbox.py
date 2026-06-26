"""Agent → Telegram outbox (AI Office v3).

An agent posts a chat message via the ``say`` tool; it lands here as an unsent
``Message`` row (kind ``CHAT``). The gateway's :class:`OutboxService` drains those
rows and delivers each through the **from-agent's own bot** (so it shows up as
"Alice", "Sam", … in the chat — like the video), falling back to the team bot.

Going through the DB (not a direct call) is what makes it work across processes:
a worker in its own container writes the row; the gateway — which actually holds
the Telegram bots — sends it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlmodel import select

from src.config import settings
from src.db.engine import get_session
from src.db.models import Message
from src.registry import registry

logger = logging.getLogger(__name__)

# poster(from_agent_slug, chat_id, text) -> None  (sends via that agent's bot)
Poster = Callable[[str, int, str], Awaitable[None]]


def enqueue_say(agent: str, text: str, *, chat_id: Optional[int] = None) -> Optional[int]:
    """Queue a chat message from ``agent`` for delivery. Returns the row id."""
    text = (text or "").strip()
    if not text:
        return None
    target = chat_id if chat_id is not None else settings.team_chat_id
    a = registry.get(agent)
    with get_session() as session:
        row = Message(from_agent_id=a.id if a else None, chat_id=target or None,
                      kind="CHAT", text=text[:4000], sent=False)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _slug_by_id() -> dict:
    return {a.id: a.slug for a in registry.list_agents(enabled_only=False) if a.id is not None}


class OutboxService:
    """Polls the DB for unsent agent chat messages and delivers them."""

    def __init__(self, poster: Poster) -> None:
        self._poster = poster
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    async def drain(self) -> int:
        """Send all pending CHAT messages. Returns how many were delivered."""
        by_slug = _slug_by_id()
        with get_session() as session:
            pending = list(session.exec(
                select(Message).where(Message.kind == "CHAT", Message.sent == False)  # noqa: E712
                .order_by(Message.id)
            ).all())
        sent = 0
        for m in pending:
            slug = by_slug.get(m.from_agent_id) or "system"
            chat_id = m.chat_id or settings.team_chat_id
            ok = True
            if chat_id:
                try:
                    await self._poster(slug, chat_id, m.text)
                except Exception:  # noqa: BLE001 - a send failure leaves it unsent for retry
                    logger.exception("outbox delivery failed for %s", slug)
                    ok = False
            if ok:
                with get_session() as session:
                    row = session.get(Message, m.id)
                    if row:
                        row.sent = True
                        session.add(row)
                        session.commit()
                sent += 1
        return sent

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.drain()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("outbox drain failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("outbox service started")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
