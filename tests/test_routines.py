"""Unit tests for routines/heartbeats: schedule math, CRUD, and the tick loop.
No network (the scheduler runs a stub runner).

    python tests/test_routines.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import routines
from src.config import settings
from src.registry import registry


def _schedule_math() -> None:
    now = datetime(2026, 6, 19, 12, 0, 0)  # a Friday, midday UTC
    assert routines.compute_next("interval", "60", now) == now + timedelta(seconds=60)
    assert routines.compute_next("interval", "bad", now) == now + timedelta(seconds=3600)
    # daily before/after current time
    assert routines.compute_next("daily", "20:00", now) == now.replace(hour=20, minute=0)
    assert routines.compute_next("daily", "08:00", now) == now.replace(hour=8) + timedelta(days=1)
    # weekly -> Monday 09:00 in the future
    nxt = routines.compute_next("weekly", "0 09:00", now)
    assert nxt.weekday() == 0 and nxt.hour == 9 and nxt > now


def _crud_due_advance() -> None:
    r = routines.create_routine({
        "name": "_ut_routine", "schedule_kind": "interval", "schedule_value": "5",
        "prompt": "ping", "target": "team",
    })
    rid = r["id"]
    try:
        assert r["next_run_at"] is not None
        # not due yet (next ~ now+5s)
        assert all(x["id"] != rid for x in routines.due_routines(datetime.utcnow()))
        # make it due, confirm it shows up
        assert routines.trigger_now(rid) is True
        due_ids = [x["id"] for x in routines.due_routines(datetime.utcnow() + timedelta(seconds=1))]
        assert rid in due_ids
        # advancing schedules the next slot in the future
        nxt = routines.mark_ran(rid, datetime.utcnow())
        assert nxt is not None and nxt > datetime.utcnow()
        # update reschedules on cadence change
        upd = routines.update_routine(rid, {"schedule_kind": "daily", "schedule_value": "06:00"})
        assert upd["schedule_kind"] == "daily"
    finally:
        assert routines.delete_routine(rid) is True
        assert routines.delete_routine(rid) is False


def _scheduler_tick() -> None:
    calls: list[dict] = []
    posts: list[str] = []

    async def runner(r: dict) -> str:
        calls.append(r)
        return "ok-result"

    async def sender(text: str) -> None:
        posts.append(text)

    sched = routines.RoutineScheduler(runner, sender)
    r = routines.create_routine({
        "name": "_ut_tick", "schedule_kind": "interval", "schedule_value": "3600",
        "prompt": "do it", "target": "team",
    })
    rid = r["id"]
    try:
        routines.trigger_now(rid)
        # disabled master switch -> nothing fires
        settings.enable_routines = False
        assert asyncio.run(sched.tick(now=datetime.utcnow() + timedelta(seconds=1))) == 0
        # enabled -> our routine fires once, runs, and posts
        settings.enable_routines = True
        fired = asyncio.run(sched.tick(now=datetime.utcnow() + timedelta(seconds=1)))
        assert fired >= 1
        assert any(c["id"] == rid for c in calls)
        assert any("ok-result" in p for p in posts)
        # after firing, it is no longer due (next slot is ~1h out)
        assert all(x["id"] != rid for x in routines.due_routines(datetime.utcnow()))
    finally:
        settings.enable_routines = False
        routines.delete_routine(rid)


def _downtime_coalescing() -> None:
    """After hours of downtime a routine fires ONCE and reschedules from now —
    no backlog of one run per missed slot."""
    from src.db.engine import get_session
    from src.db.models import Routine

    r = routines.create_routine({
        "name": "_ut_coalesce", "schedule_kind": "interval", "schedule_value": "5",
        "prompt": "ping", "target": "team",
    })
    rid = r["id"]
    try:
        now = datetime.utcnow()
        with get_session() as s:  # simulate 2h overdue
            row = s.get(Routine, rid)
            row.next_run_at = now - timedelta(hours=2)
            s.add(row)
            s.commit()
        due = [d for d in routines.due_routines(now) if d["id"] == rid]
        assert len(due) == 1, "overdue routine must be due exactly once, not per slot"
        nxt = routines.mark_ran(rid, now)
        assert now < nxt <= now + timedelta(seconds=10), "next slot is from now, not backfilled"
        assert all(d["id"] != rid for d in routines.due_routines(now)), "not due again after run"
    finally:
        routines.delete_routine(rid)


def main() -> None:
    registry.setup()
    enable0 = settings.enable_routines
    try:
        _schedule_math()
        _crud_due_advance()
        _scheduler_tick()
        _downtime_coalescing()
    finally:
        settings.enable_routines = enable0
    print("routines tests: OK")


if __name__ == "__main__":
    main()
