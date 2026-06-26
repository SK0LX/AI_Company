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
import json
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
# decider(addressed_slug, transcript) -> (respond?, reply_text)
Decider = Callable[[str, str], Awaitable[tuple[bool, str]]]


def enqueue_say(agent: str, text: str, *, chat_id: Optional[int] = None,
                depth: int = 0) -> Optional[int]:
    """Queue a chat message from ``agent`` for delivery. ``depth`` is the
    auto-reply chain position (0 = a fresh agent-initiated message). Returns the id."""
    text = (text or "").strip()
    if not text:
        return None
    target = chat_id if chat_id is not None else settings.team_chat_id
    a = registry.get(agent)
    if not a:
        # Fail closed: never create an unattributed CHAT row — it would surface as
        # "system" in drain and let an auto-reply chain start from a non-agent.
        logger.warning("enqueue_say: unknown agent %r — dropped", agent)
        return None
    with get_session() as session:
        row = Message(from_agent_id=a.id, chat_id=target or None,
                      kind="CHAT", text=text[:4000], sent=False,
                      meta_json=json.dumps({"depth": int(depth)}))
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _slug_by_id() -> dict:
    return {a.id: a.slug for a in registry.list_agents(enabled_only=False) if a.id is not None}


def _depth_of(m: Message) -> int:
    try:
        return int(json.loads(m.meta_json or "{}").get("depth", 0))
    except Exception:  # noqa: BLE001
        # Fail CLOSED: a corrupt/missing depth must not reset the chain to 0 and
        # let an auto-reply loop run forever — treat it as already at the cap.
        return settings.agent_chat_max_depth


async def _default_decider(slug: str, transcript: str) -> tuple[bool, str]:
    """The live decider: the addressed agent decides whether/what to reply."""
    from src.graph.team_graph import agroup_decide

    return await agroup_decide(slug, transcript)


class OutboxService:
    """Polls the DB for unsent agent chat messages and delivers them. When an agent
    message addresses another agent (and ``enable_agent_chat`` is on), the addressed
    agent auto-replies — a bounded back-and-forth (chain depth capped)."""

    def __init__(self, poster: Poster, decider: Optional[Decider] = None) -> None:
        self._poster = poster
        self._decider = decider or _default_decider
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
                await self._maybe_autoreply(slug, m, chat_id)
        return sent

    async def _maybe_autoreply(self, sender: str, m: Message, chat_id: Optional[int]) -> None:
        """If this message addresses another agent (and agent-chat is on), have that
        agent reply — bounded by the chain-depth cap so it can't loop forever."""
        if not settings.enable_agent_chat or not chat_id:
            return
        depth = _depth_of(m)
        if depth >= settings.agent_chat_max_depth:
            return
        from src import group_chat as gc

        roster = [(a.slug, registry.label(a.slug), a.role)
                  for a in registry.list_agents(enabled_only=True)]
        target = gc.detect_addressed(m.text, roster)
        if not target or target == sender:
            return
        # Don't let a long auto-reply chain (up to agent_chat_max_depth hops, each
        # an LLM call) run an agent that's already over budget.
        from src import budget

        if budget.blocked(target):
            return
        try:
            respond, reply = await self._decider(target, f"{registry.label(sender)}: {m.text}")
        except Exception:  # noqa: BLE001 - a decider hiccup just ends the thread
            logger.exception("auto-reply decider failed for %s", target)
            return
        if respond and reply:
            enqueue_say(target, reply, chat_id=chat_id, depth=depth + 1)

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
