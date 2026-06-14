"""Proactive posting (v2 stage 7).

Agents speak up in the team chat on their own when something noteworthy happens
(a task finishes, help is needed, a hand-off is declined). It subscribes to the
:mod:`src.events` hub, maps interesting task events to a short message FROM the
relevant agent, and posts it through an injected async ``sender``.

Guardrails (SPEC_v2 §8) — every post must pass :meth:`should_speak`:
  * master switch ``settings.enable_proactive`` + a runtime ``mute()``,
  * per-agent opt-in: the agent needs the ``proactive`` permission,
  * global cooldown between any two posts,
  * per-agent rate limit over a rolling window,
  * dedup of identical (agent, type, task) within a short TTL.

Messages are templated (no LLM, so no quota cost).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional

from src.config import settings
from src.events import hub
from src.registry import registry

logger = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]

_DEDUP_TTL = 60.0  # seconds an identical post is suppressed


def _message_for(event: dict) -> Optional[tuple[str, str, str]]:
    """Map a task event to (speaker_slug, dedup_key, text), or None to stay quiet."""
    if event.get("event") != "task_event":
        return None
    actor = event.get("actor")
    if not actor:
        return None  # system event, nobody to speak for
    task_id = event.get("task_id")
    etype = event.get("type")
    payload = event.get("payload") or {}
    label = registry.label(actor)
    key = f"{actor}:{etype}:{task_id}"

    if etype == "done":
        return actor, key, f"✅ {label}: задача #{task_id} готова."
    if etype == "help_requested":
        summary = (payload.get("summary") or "").strip()
        return actor, key, f"🆘 {label}: нужна помощь по задаче #{task_id}. {summary}".strip()
    if etype == "help_resolved":
        return actor, key, f"🤝 {label}: помог(ла) по задаче #{task_id}."
    if etype == "declined":
        reason = (payload.get("reason") or "").strip()
        return actor, key, f"↩️ {label}: не берусь за #{task_id}. {reason}".strip()
    return None


class ProactiveService:
    def __init__(self, sender: Sender) -> None:
        self._sender = sender
        self._muted = False
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._last_post = 0.0  # monotonic ts of the last post (global cooldown)
        self._per_agent: dict[str, deque] = {}  # slug -> recent post timestamps
        self._recent: dict[str, float] = {}  # dedup key -> ts

    # --- guardrails ---------------------------------------------------------

    def mute(self) -> None:
        self._muted = True

    def unmute(self) -> None:
        self._muted = False

    @property
    def muted(self) -> bool:
        return self._muted

    def should_speak(self, slug: str, key: str, *, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if not settings.enable_proactive or self._muted:
            return False
        if registry.permissions(slug).get("proactive") != "true":
            return False
        if now - self._last_post < settings.proactive_min_interval:
            return False
        # dedup identical post within TTL
        last = self._recent.get(key)
        if last is not None and now - last < _DEDUP_TTL:
            return False
        # per-agent rolling-window rate limit
        window = self._per_agent.setdefault(slug, deque())
        while window and now - window[0] > settings.proactive_window:
            window.popleft()
        if len(window) >= settings.proactive_max_per_window:
            return False
        return True

    def _record_post(self, slug: str, key: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._last_post = now
        self._per_agent.setdefault(slug, deque()).append(now)
        self._recent[key] = now
        # opportunistic dedup cleanup
        for k, ts in list(self._recent.items()):
            if now - ts > _DEDUP_TTL:
                self._recent.pop(k, None)

    # --- handling -----------------------------------------------------------

    async def handle(self, event: dict) -> Optional[str]:
        """Post for one event if guardrails allow. Returns the text posted (tests)."""
        mapped = _message_for(event)
        if not mapped:
            return None
        slug, key, text = mapped
        if not self.should_speak(slug, key):
            return None
        try:
            await self._sender(text)
        except Exception:  # noqa: BLE001 - a send failure must not kill the loop
            logger.exception("proactive send failed")
            return None
        self._record_post(slug, key)
        return text

    # --- lifecycle ----------------------------------------------------------

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
        logger.info("proactive service started (enabled=%s)", settings.enable_proactive)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
