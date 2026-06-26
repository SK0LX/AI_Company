"""Unit tests for autonomous pull-work: agents claim+do unclaimed board tasks in
their area. Stub runner (no LLM/network). Uses a cleared board for determinism.

    python tests/test_autowork.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import autowork, collab, locks
from src.config import settings
from src.registry import registry


def _concurrency_cap() -> None:
    """tick() never starts more jobs than autowork_max_concurrent, even when more
    agents have claimable work."""
    aw0, cap0, b0 = (settings.enable_autowork, settings.autowork_max_concurrent,
                     settings.enable_budget)
    settings.enable_autowork = True
    settings.enable_budget = False
    settings.autowork_max_concurrent = 1  # only one job may start per tick
    collab.clear_board(mode="delete")
    t_dev = collab.create_task("Починить api эндпоинт на бэкенде", created_by="ceo")
    t_fe = collab.create_task("Сверстать css вёрстку экрана интерфейса", created_by="ceo")
    try:
        # two different agents each have work, so without a cap >1 would fire
        assert t_dev in autowork.candidates_for("developer", "developer")
        assert t_fe in autowork.candidates_for("frontend", "frontend")

        async def runner(slug: str, task_id: int, text: str) -> str:
            return "ok"

        fired = asyncio.run(autowork.AutoWorkService(runner).tick())
        assert fired == 1, f"cap=1 must start exactly one job, got {fired}"
        statuses = sorted([collab.get_task(t_dev)["status"], collab.get_task(t_fe)["status"]])
        assert statuses == ["done", "new"], statuses  # exactly one done, one left
    finally:
        settings.enable_autowork, settings.autowork_max_concurrent, settings.enable_budget = aw0, cap0, b0
        collab.delete_task(t_dev)
        collab.delete_task(t_fe)


def main() -> None:
    registry.setup()
    _concurrency_cap()
    aw0, b0, cap0 = settings.enable_autowork, settings.enable_budget, settings.autowork_max_concurrent
    settings.enable_autowork = False
    settings.enable_budget = False
    settings.autowork_max_concurrent = 10
    collab.clear_board(mode="delete")  # deterministic empty board

    tid = collab.create_task("Починить api эндпоинт", "нужен новый endpoint", created_by="ceo")
    try:
        # candidate matching: developer's area matches, frontend's doesn't
        assert tid in autowork.candidates_for("developer", "developer")
        assert tid not in autowork.candidates_for("frontend", "frontend")

        calls: list = []
        posts: list = []

        async def runner(slug: str, task_id: int, text: str) -> str:
            calls.append((slug, task_id))
            return "сделано"

        async def sender(text: str) -> None:
            posts.append(text)

        svc = autowork.AutoWorkService(runner, sender)

        # master switch off -> nothing happens
        assert asyncio.run(svc.tick()) == 0
        assert collab.get_task(tid)["status"] == "new"

        # on -> developer pulls it, claims atomically, does it, marks done, releases
        settings.enable_autowork = True
        fired = asyncio.run(svc.tick())
        assert fired == 1
        assert calls and calls[0] == ("developer", tid)
        assert collab.get_task(tid)["status"] == "done"
        assert locks.task_holder(tid) is None
        assert any(f"#{tid}" in p for p in posts)

        # a done task is no longer a candidate
        assert tid not in autowork.candidates_for("developer", "developer")

        # claimed/in-progress tasks aren't pulled (claim is the anti-double-work gate)
        t2 = collab.create_task("ещё один api эндпоинт", created_by="ceo")
        assert locks.claim_task(t2, "frontend")  # someone already holds it
        assert t2 not in autowork.candidates_for("developer", "developer")
        collab.delete_task(t2)
    finally:
        settings.enable_autowork, settings.enable_budget = aw0, b0
        settings.autowork_max_concurrent = cap0
        collab.delete_task(tid)

    print("autowork tests: OK")


if __name__ == "__main__":
    main()
