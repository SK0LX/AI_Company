"""Autonomous pull-work (AI Office v3) — the team self-organizes on the board.

On a heartbeat, each idle agent looks at the board for an UNCLAIMED task in its
area, **atomically claims** it (so two agents can't grab the same one — see
:mod:`src.locks`), does the work, marks it done and releases. No CEO push: agents
pull. This is the "команда сама пашет" core from the video.

Guardrails: master switch ``enable_autowork`` (off by default — it spends budget on
its own), a global concurrency cap, per-agent budget hard-stop, and the atomic
claim as the anti-double-work invariant. The actual "do the work" call is an
injected ``runner`` so the LLM stays out of this module and tests use a stub.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlmodel import select

from src import budget, locks
from src.config import settings
from src.db.engine import get_session
from src.db.models import Task
from src.group_chat import ROLE_KEYWORDS
from src.registry import registry

logger = logging.getLogger(__name__)

# runner(agent_slug, task_id, task_text) -> result text
Runner = Callable[[str, int, str], Awaitable[str]]
Sender = Callable[[str], Awaitable[None]]


def candidates_for(agent_slug: str, role: str) -> list[int]:
    """Unclaimed 'new' task ids that belong to this agent: owned by it, or whose
    text hits its role keywords. Newest-first so fresh work is picked up first."""
    agent = registry.get(agent_slug)
    agent_id = agent.id if agent else None
    kws = ROLE_KEYWORDS.get(role, ())
    out: list[int] = []
    with get_session() as session:
        rows = session.exec(select(Task).where(Task.status == "new").order_by(Task.id.desc())).all()
    for t in rows:
        if t.claimed_by:  # already taken
            continue
        if t.owner_agent_id == agent_id and agent_id is not None:
            out.append(t.id)
            continue
        text = f"{t.title} {t.description}".lower()
        if kws and any(k in text for k in kws):
            out.append(t.id)
    return out


class AutoWorkService:
    def __init__(self, runner: Runner, sender: Optional[Sender] = None) -> None:
        self._runner = runner
        self._sender = sender
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    async def tick(self) -> int:
        """One heartbeat: idle agents claim+do one matching task each, up to the
        concurrency cap. Returns how many jobs ran."""
        if not settings.enable_autowork:
            return 0
        try:
            locks.prune_expired()
        except Exception:  # noqa: BLE001
            pass
        cap = max(1, settings.autowork_max_concurrent)
        claims: list[tuple[str, int]] = []
        for a in registry.list_agents(enabled_only=True):
            if a.slug == "ceo":  # CEO coordinates, doesn't pull worker tasks
                continue
            if len(claims) >= cap:
                break
            if budget.blocked(a.slug):
                continue
            for tid in candidates_for(a.slug, a.role):
                if locks.claim_task(tid, a.slug):  # atomic — anti double-work
                    claims.append((a.slug, tid))
                    break
        if claims:
            await asyncio.gather(*[self._work(s, t) for s, t in claims], return_exceptions=True)
        return len(claims)

    async def _work(self, slug: str, task_id: int) -> None:
        from src import collab

        task = collab.get_task(task_id) or {}
        text = f"{task.get('title', '')}\n{task.get('description', '')}".strip()
        try:
            result = await self._runner(slug, task_id, text)
        except Exception:  # noqa: BLE001 - a failed job must not kill the loop
            logger.exception("autowork job failed: %s #%s", slug, task_id)
            collab.set_task_status(task_id, "new", actor=slug)  # back to the queue
            locks.release_task(task_id, slug)
            return
        collab.set_task_status(task_id, "done", actor=slug)  # fires the Задачник event
        locks.release_task(task_id, slug)
        if self._sender and result:
            try:
                await self._sender(f"🤖 {registry.label(slug)} закрыл #{task_id}: {result[:200]}")
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("autowork tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=max(10, settings.autowork_tick_seconds))
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("autowork started (enabled=%s, cap=%s)",
                    settings.enable_autowork, settings.autowork_max_concurrent)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
