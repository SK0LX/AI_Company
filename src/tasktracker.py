"""Задачник — auto-posts the task lifecycle into a dedicated Telegram chat.

Mirrors the AI-Office "Задачник" channel: every task event (created, delegated,
in-progress, done, cancelled, help) becomes one short templated line posted to
``settings.task_channel_id`` — e.g. "✅ Задача #102 закрыта · исполнитель: Sam ·
от: CEO". No LLM (zero quota cost), light dedup, master switch via the channel id.

It subscribes to the same in-process event hub the board writes to, so it stays
in lock-step with the real task state.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, Optional

from src.config import settings
from src.events import hub
from src.registry import registry

logger = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]


def format_event(event: dict) -> Optional[str]:
    """Map a task event to a one-line Задачник post, or None to skip it."""
    if event.get("event") != "task_event":
        return None
    tid = event.get("task_id")
    etype = event.get("type")
    p = event.get("payload") or {}
    actor = event.get("actor")
    who = registry.label(actor) if actor else None

    if etype == "created":
        title = (p.get("title") or "").strip()
        return f"🆕 Задача #{tid}: {title}" if title else f"🆕 Задача #{tid} создана"
    if etype == "delegated":
        to = registry.label(p.get("to")) if p.get("to") else "?"
        return f"➡️ #{tid} → {to}" + (f" (от {who})" if who else "")
    if etype == "accepted":
        return f"🤝 #{tid} взял в работу {who or '?'}"
    if etype == "declined":
        reason = (p.get("reason") or "").strip()
        return f"↩️ #{tid} отклонил {who or '?'}" + (f": {reason}" if reason else "")
    if etype == "help_requested":
        summary = (p.get("summary") or "").strip()
        return f"🆘 #{tid} нужна помощь" + (f": {summary}" if summary else "")
    if etype == "help_resolved":
        return f"🤝 #{tid} помогли"
    if etype == "status":
        st = p.get("status")
        if st == "done":
            from src import collab

            t = collab.get_task(tid) or {}
            owner = t.get("owner_name") or "—"
            frm = t.get("from_name") or "—"
            return f"✅ Задача #{tid} закрыта · исполнитель: {owner} · от: {frm}"
        if st == "cancelled":
            return f"🗑 #{tid} отменена"
        if st == "in_progress":
            return f"🚧 #{tid} в работе" + (f" ({who})" if who else "")
        if st == "review":
            return f"👀 #{tid} на ревью"
        if st == "blocked":
            return f"⛔ #{tid} заблокирована"
        return None
    return None  # thoughts / results / etc. are noise for the tracker


class TaskTrackerService:
    def __init__(self, sender: Sender) -> None:
        self._sender = sender
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._recent: deque = deque(maxlen=30)  # dedup identical posts

    async def handle(self, event: dict) -> Optional[str]:
        """Post one event if the channel is configured. Returns the text (tests)."""
        if not settings.task_channel_id:
            return None
        text = format_event(event)
        if not text:
            return None
        key = hash(text)
        if key in self._recent:
            return None
        try:
            await self._sender(text)
        except Exception:  # noqa: BLE001 - a failed post must not kill the loop
            logger.exception("task tracker post failed")
            return None
        self._recent.append(key)
        return text

    async def _run(self) -> None:
        queue = hub.subscribe()
        assert self._stop is not None
        try:
            while not self._stop.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self.handle(event)
        finally:
            hub.unsubscribe(queue)

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("task tracker started (channel=%s)", settings.task_channel_id or "off")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
