"""Live event hub (v2 stage 6).

A tiny in-process fan-out so the admin UI can watch task activity in real time.
Anything that records a task event publishes here; each connected WebSocket holds
a subscriber queue. It is best-effort and live-only — the database is the source
of truth, so a dropped event just means the client refetches.

Publishing is safe to call from the event loop (where the team graph runs). If
called off-loop (e.g. a threadpool request handler) it silently no-ops rather
than risk touching an asyncio.Queue from the wrong thread.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_MAX_QUEUED = 1000  # cap per subscriber so a slow client can't grow unbounded


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUED)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not on the event loop — skip the live push
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # drop for a slow client; it can refetch
                pass


# Process-wide singleton.
hub = EventHub()
