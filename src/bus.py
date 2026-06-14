"""In-process message bus (v2 stage 4).

A tiny async pub/sub that lets agents talk to each other inside the single
process. Each agent has an :class:`asyncio.Queue` inbox; every published message
is also persisted to the ``messages`` table for the audit trail / future admin
UI. Messages are typed Pydantic models — a discriminated union on ``kind`` that
mirrors the inter-agent protocol in ``docs/SPEC_v2.md`` §5:

    DELEGATE(task,from,to,kind,reason) → ACCEPT/DECLINE(ref,reason)
    HELP_REQUEST(task,from,summary,scope) → HELP_RESULT(task,helper,summary)
    STATUS(task,from,state,note)
    CHAT(from,chat_id,text)

Routing: a message with ``to_agent`` goes to that agent's inbox; a ``CHAT`` with
no recipient is broadcast to chat subscribers; anything else with no recipient
(e.g. an unaddressed HELP_REQUEST) lands on the orchestrator inbox.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal, Optional, Union

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Inbox key for messages that need an orchestrator decision (e.g. who helps).
ORCHESTRATOR = "__orchestrator__"


# --- typed messages ---------------------------------------------------------

class _BaseMsg(BaseModel):
    from_agent: str  # slug of the sender
    task_id: Optional[int] = None


class Delegate(_BaseMsg):
    kind: Literal["DELEGATE"] = "DELEGATE"
    to_agent: str
    delegation_kind: Literal["task", "permission"] = "task"
    reason: str = ""


class Accept(_BaseMsg):
    kind: Literal["ACCEPT"] = "ACCEPT"
    to_agent: str  # whom to notify (the original requester)
    ref: Optional[int] = None  # delegation id this answers
    reason: str = ""


class Decline(_BaseMsg):
    kind: Literal["DECLINE"] = "DECLINE"
    to_agent: str
    ref: Optional[int] = None
    reason: str = ""


class HelpRequest(_BaseMsg):
    kind: Literal["HELP_REQUEST"] = "HELP_REQUEST"
    to_agent: Optional[str] = None  # None -> orchestrator picks a helper
    summary: str = ""
    scope: list[str] = []


class HelpResult(_BaseMsg):
    kind: Literal["HELP_RESULT"] = "HELP_RESULT"
    to_agent: str  # the original requester
    helper: str
    summary: str = ""


class Status(_BaseMsg):
    kind: Literal["STATUS"] = "STATUS"
    to_agent: Optional[str] = None
    state: str = ""
    note: str = ""


class Chat(_BaseMsg):
    kind: Literal["CHAT"] = "CHAT"
    to_agent: Optional[str] = None
    chat_id: Optional[int] = None
    text: str = ""


BusMessage = Union[Delegate, Accept, Decline, HelpRequest, HelpResult, Status, Chat]


# --- persistence ------------------------------------------------------------

def _record(msg: BusMessage) -> None:
    """Append the message to the ``messages`` table. Best-effort: a logging
    failure must never break delivery."""
    try:
        from src.db.engine import get_session
        from src.db.models import Message
        from src.registry import registry

        def _id(slug: Optional[str]) -> Optional[int]:
            agent = registry.get(slug) if slug else None
            return agent.id if agent else None

        body = (
            getattr(msg, "text", "")
            or getattr(msg, "summary", "")
            or getattr(msg, "reason", "")
            or getattr(msg, "note", "")
        )
        meta = msg.model_dump(
            exclude={"from_agent", "to_agent", "kind", "text", "chat_id", "task_id"}
        )
        row = Message(
            from_agent_id=_id(msg.from_agent),
            to_agent_id=_id(getattr(msg, "to_agent", None)),
            chat_id=getattr(msg, "chat_id", None),
            kind=msg.kind,
            text=body,
            meta_json=json.dumps(meta, default=str),
        )
        with get_session() as session:
            session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to persist bus message (%s)", getattr(msg, "kind", "?"))


# --- the bus ----------------------------------------------------------------

class MessageBus:
    """Async pub/sub with one inbox queue per agent, plus chat broadcast."""

    def __init__(self, *, persist: bool = True) -> None:
        self._inboxes: dict[str, asyncio.Queue] = {}
        self._chat_subscribers: set[asyncio.Queue] = set()
        self._persist = persist

    def inbox(self, slug: str) -> asyncio.Queue:
        q = self._inboxes.get(slug)
        if q is None:
            q = asyncio.Queue()
            self._inboxes[slug] = q
        return q

    def subscribe_chat(self) -> asyncio.Queue:
        """Get a queue that receives every broadcast CHAT (team chat / live UI)."""
        q: asyncio.Queue = asyncio.Queue()
        self._chat_subscribers.add(q)
        return q

    def unsubscribe_chat(self, q: asyncio.Queue) -> None:
        self._chat_subscribers.discard(q)

    async def publish(self, msg: BusMessage) -> None:
        """Persist + route a message. Never blocks (queues are unbounded)."""
        if self._persist:
            _record(msg)
        to = getattr(msg, "to_agent", None)
        if to:
            self.inbox(to).put_nowait(msg)
        elif msg.kind == "CHAT":
            for q in list(self._chat_subscribers):
                q.put_nowait(msg)
        else:
            self.inbox(ORCHESTRATOR).put_nowait(msg)

    async def receive(self, slug: str) -> BusMessage:
        """Await the next message for ``slug`` (blocks until one arrives)."""
        return await self.inbox(slug).get()

    def get_nowait(self, slug: str) -> Optional[BusMessage]:
        try:
            return self.inbox(slug).get_nowait()
        except asyncio.QueueEmpty:
            return None


# Process-wide singleton (mirrors the registry).
bus = MessageBus()
