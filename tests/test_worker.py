"""Unit tests for the per-agent worker runtime (v3 Ф3-full): claim+do, the
no-double-work race between two workers, heartbeat/liveness, and /api/workers.
Uses a stub runner (no LLM, no network).

    python tests/test_worker.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _client import app_client

from src import collab, locks, worker, workers
from src.registry import registry


def _basic() -> None:
    tid = collab.create_task("api: построить эндпоинт", created_by="ceo", owner="developer")
    try:
        done: list[str] = []

        async def stub(slug: str, t: int, text: str) -> str:
            done.append(slug)
            return "ok"

        res = asyncio.run(worker.run_worker("developer", runner=stub, once=True))
        assert res == tid                                  # it picked our task
        assert done == ["developer"]                       # did the work once
        assert collab.get_task(tid)["status"] == "done"    # marked done
        assert workers.is_alive("developer")               # heartbeat recorded
        assert "developer" in workers.alive_agents()
    finally:
        collab.delete_task(tid)


def _respects_existing_claim() -> None:
    """A worker never touches a task another worker already holds — the atomic
    no-double-work invariant (real multi-process atomicity is proven by the
    threaded race in test_locks)."""
    tid = collab.create_task("нужен api и css на экране", created_by="ceo")
    try:
        assert locks.claim_task(tid, "frontend")           # frontend grabs it first
        completed: list[str] = []

        async def stub(slug: str, t: int, text: str) -> str:
            completed.append(slug)
            return "ok"

        res = asyncio.run(worker.run_worker("developer", runner=stub, once=True))
        assert res is None                                  # developer can't take it
        assert completed == []                              # so it never ran the work
        assert collab.get_task(tid)["status"] != "done"     # still held by frontend
    finally:
        collab.delete_task(tid)


def _api() -> None:
    with app_client() as c:
        workers.beat("developer", host="ut-host", pid=4242)
        rows = c.get("/api/workers").json()
        dev = next((w for w in rows if w["agent"] == "developer"), None)
        assert dev and dev["alive"] is True and dev["host"] == "ut-host"
        # home summary carries the worker flag too
        home = c.get("/api/home").json()
        d = next((a for a in home["team"] if a["slug"] == "developer"), None)
        assert d is not None and d["worker"] is True


def _failure_requeue() -> None:
    """A runner that throws must return the task to the queue, not strand it
    'in_progress', and must release the claim."""
    tid = collab.create_task("задача которая упадёт", created_by="ceo", owner="developer")
    try:
        async def boom(slug: str, t: int, text: str) -> str:
            raise RuntimeError("job exploded")

        res = asyncio.run(worker.run_worker("developer", runner=boom, once=True))
        assert res == tid                                   # it claimed + attempted
        assert collab.get_task(tid)["status"] == "new"      # requeued, not stuck
        assert locks.task_holder(tid) is None               # claim released
    finally:
        collab.delete_task(tid)


def _budget_blocked() -> None:
    """A budget-blocked agent takes no work (no claim, no run)."""
    from src import budget

    tid = collab.create_task("дорогая задача", created_by="ceo", owner="developer")
    orig = budget.blocked
    budget.blocked = lambda slug: True  # type: ignore[assignment]
    ran: list[str] = []
    try:
        async def stub(slug: str, t: int, text: str) -> str:
            ran.append(slug)
            return "ok"

        res = asyncio.run(worker.run_worker("developer", runner=stub, once=True))
        assert res is None                                  # nothing taken
        assert ran == []                                    # work never ran
        assert collab.get_task(tid)["status"] != "done"
        assert locks.task_holder(tid) is None               # not claimed
    finally:
        budget.blocked = orig  # type: ignore[assignment]
        collab.delete_task(tid)


def main() -> None:
    registry.setup()
    _basic()
    _respects_existing_claim()
    _failure_requeue()
    _budget_blocked()
    _api()
    print("worker tests: OK")


if __name__ == "__main__":
    main()
