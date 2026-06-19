"""Routines / heartbeats.

A routine is a recurring job: on its schedule it creates a tracked task and wakes
the team (or one agent) with a prompt, then posts the result to the team chat —
so regular work (digests, checks, reports) happens without anyone kicking it off.

Schedules are computed by hand (no cron dependency):
- ``interval``: every N seconds (``schedule_value`` = seconds)
- ``daily``: at ``HH:MM`` UTC
- ``weekly``: at ``D HH:MM`` UTC, D = 0..6 (Monday=0, matching ``date.weekday()``)

The :class:`RoutineScheduler` async loop mirrors :class:`ProactiveService`: it
ticks on a timer, runs due routines through an injected ``runner`` (so the LLM
stays out of this module and tests use a stub), and posts via a ``sender``.
Overdue routines coalesce — a routine fires once per tick and its next slot is
recomputed from *now*, so downtime never produces a backlog of replayed runs.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from sqlmodel import select

from src.config import settings
from src.db.engine import get_session
from src.db.models import Routine

logger = logging.getLogger(__name__)

Runner = Callable[[dict], Awaitable[str]]  # (routine dict) -> result text
Sender = Callable[[str], Awaitable[None]]


# --- schedule math ----------------------------------------------------------

def compute_next(kind: str, value: str, now: datetime) -> datetime:
    """The next fire time strictly after ``now`` (naive UTC)."""
    value = (value or "").strip()
    if kind == "interval":
        try:
            secs = max(5, int(float(value)))
        except (TypeError, ValueError):
            secs = 3600
        return now + timedelta(seconds=secs)
    if kind == "daily":
        hh, mm = _parse_hhmm(value)
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    if kind == "weekly":
        parts = value.split()
        dow = _parse_int(parts[0], 0) % 7 if parts else 0
        hh, mm = _parse_hhmm(parts[1] if len(parts) > 1 else "09:00")
        days_ahead = (dow - now.weekday()) % 7
        cand = (now + timedelta(days=days_ahead)).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=7)
        return cand
    # unknown kind -> treat as hourly so a misconfig never spins hot
    return now + timedelta(hours=1)


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        hh, mm = s.split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:  # noqa: BLE001
        return 9, 0


def _parse_int(s: str, default: int) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# --- CRUD (used by the admin API) -------------------------------------------

def _as_dict(r: Routine) -> dict:
    return {
        "id": r.id, "name": r.name, "schedule_kind": r.schedule_kind,
        "schedule_value": r.schedule_value, "prompt": r.prompt, "target": r.target,
        "enabled": r.enabled, "catch_up": r.catch_up,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
    }


def list_routines() -> list[dict]:
    with get_session() as session:
        return [_as_dict(r) for r in session.exec(select(Routine).order_by(Routine.id)).all()]


def create_routine(data: dict) -> dict:
    now = datetime.utcnow()
    kind = data.get("schedule_kind") or "interval"
    value = str(data.get("schedule_value") or "3600")
    with get_session() as session:
        r = Routine(
            name=(data.get("name") or "routine").strip(),
            schedule_kind=kind,
            schedule_value=value,
            prompt=data.get("prompt") or "",
            target=data.get("target") or "team",
            enabled=bool(data.get("enabled", True)),
            catch_up=bool(data.get("catch_up", False)),
            next_run_at=compute_next(kind, value, now),
        )
        session.add(r)
        session.commit()
        session.refresh(r)
        return _as_dict(r)


def update_routine(routine_id: int, data: dict) -> Optional[dict]:
    now = datetime.utcnow()
    with get_session() as session:
        r = session.get(Routine, routine_id)
        if not r:
            return None
        for f in ("name", "schedule_kind", "schedule_value", "prompt", "target"):
            if f in data and data[f] is not None:
                setattr(r, f, str(data[f]) if f != "name" else data[f])
        if "enabled" in data:
            r.enabled = bool(data["enabled"])
        if "catch_up" in data:
            r.catch_up = bool(data["catch_up"])
        # Reschedule if the cadence changed.
        if any(f in data for f in ("schedule_kind", "schedule_value")):
            r.next_run_at = compute_next(r.schedule_kind, r.schedule_value, now)
        session.add(r)
        session.commit()
        session.refresh(r)
        return _as_dict(r)


def delete_routine(routine_id: int) -> bool:
    with get_session() as session:
        r = session.get(Routine, routine_id)
        if not r:
            return False
        session.delete(r)
        session.commit()
    return True


def trigger_now(routine_id: int) -> bool:
    """Make a routine due immediately (the scheduler runs it on its next tick)."""
    with get_session() as session:
        r = session.get(Routine, routine_id)
        if not r:
            return False
        r.next_run_at = datetime.utcnow()
        session.add(r)
        session.commit()
    return True


# --- scheduling core (pure, testable) ---------------------------------------

def due_routines(now: datetime) -> list[dict]:
    """Enabled routines whose next_run_at has arrived."""
    with get_session() as session:
        rows = session.exec(select(Routine).where(Routine.enabled)).all()
        return [_as_dict(r) for r in rows
                if r.next_run_at is not None and r.next_run_at <= now]


def mark_ran(routine_id: int, now: datetime) -> Optional[datetime]:
    """Record a run and schedule the next slot (coalescing from ``now``)."""
    with get_session() as session:
        r = session.get(Routine, routine_id)
        if not r:
            return None
        r.last_run_at = now
        r.next_run_at = compute_next(r.schedule_kind, r.schedule_value, now)
        session.add(r)
        session.commit()
        return r.next_run_at


# --- the scheduler loop -----------------------------------------------------

class RoutineScheduler:
    def __init__(self, runner: Runner, sender: Optional[Sender] = None) -> None:
        self._runner = runner
        self._sender = sender
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    async def run_one(self, routine: dict, *, now: Optional[datetime] = None) -> Optional[str]:
        """Execute one routine: record a task + activity, run it, post the result."""
        now = now or datetime.utcnow()
        from src import activity, collab

        name = routine.get("name") or f"routine#{routine.get('id')}"
        task_id = None
        try:
            task_id = collab.create_task(f"Рутина: {name}", routine.get("prompt") or "",
                                         created_by=None, owner=None)
        except Exception:  # noqa: BLE001 - tracking must never block the run
            logger.exception("routine task creation failed")
        try:
            activity.log("system", "routine_fire", name, routine_id=routine.get("id"),
                         target=routine.get("target"), task_id=task_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            result = await self._runner({**routine, "task_id": task_id})
        except Exception:  # noqa: BLE001 - a routine failure must not kill the loop
            logger.exception("routine runner failed for %s", name)
            result = f"[рутина '{name}' упала: см. логи]"
        if task_id is not None:
            try:
                collab.set_task_status(task_id, "done", actor=None)
            except Exception:  # noqa: BLE001
                pass
        if self._sender and result:
            try:
                await self._sender(f"🔔 {name}\n{result}".strip())
            except Exception:  # noqa: BLE001
                logger.exception("routine post failed")
        return result

    async def tick(self, *, now: Optional[datetime] = None) -> int:
        """Run all due routines once. Returns how many fired."""
        if not settings.enable_routines:
            return 0
        now = now or datetime.utcnow()
        fired = 0
        for routine in due_routines(now):
            mark_ran(routine["id"], now)  # advance BEFORE running so a slow run can't double-fire
            await self.run_one(routine, now=now)
            fired += 1
        return fired

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("routine tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=max(5, settings.routines_tick_seconds))
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("routine scheduler started (enabled=%s)", settings.enable_routines)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
