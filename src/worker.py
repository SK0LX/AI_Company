"""Per-agent worker (AI Office v3 Ф3-full): one process / container per agent.

    python -m src.worker <agent-slug>

Each worker runs an autonomous loop for ONE agent: heartbeat → find an unclaimed
task in its area → **atomically claim** it (so no other worker double-works — see
src.locks) → do it → mark done → release. Many workers (containers) coordinate
purely through the shared DB + the atomic claim; there is no central pusher.

DRY-RUN (env ``WORKER_DRYRUN=1``): claim + complete tasks WITHOUT calling the LLM —
for smoke-testing the topology/coordination without spending any budget.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from typing import Awaitable, Callable, Optional

from src import budget, locks
from src.config import settings
from src.registry import registry

logger = logging.getLogger("worker")

Runner = Callable[[str, int, str], Awaitable[str]]


async def _real_runner(slug: str, task_id: int, text: str) -> str:
    """Do the actual work by running the specialist on the task (uses the LLM)."""
    from src.graph.team_graph import arun_specialist

    return await arun_specialist(slug, text, project=f"worker-{slug}")


async def _dry_runner(slug: str, task_id: int, text: str) -> str:
    return f"[dry-run] {slug} обработал #{task_id}"


async def _work_one(slug: str, task_id: int, runner: Runner, *, token: str = "") -> bool:
    from src import collab

    task = collab.get_task(task_id) or {}
    text = f"{task.get('title', '')}\n{task.get('description', '')}".strip()
    try:
        await runner(slug, task_id, text)
    except Exception:  # noqa: BLE001 - a failed job returns the task to the queue
        logger.exception("worker job failed: %s #%s", slug, task_id)
        collab.set_task_status(task_id, "new", actor=slug)
        locks.release_task(task_id, slug, token=token)
        return False
    collab.set_task_status(task_id, "done", actor=slug)  # fires the Задачник event
    locks.release_task(task_id, slug, token=token)
    logger.info("worker %s closed task #%s", slug, task_id)
    return True


async def tick_once(slug: str, role: str, runner: Runner) -> Optional[int]:
    """One beat: heartbeat, then claim+do at most one matching task. Returns the
    task id it completed, or None."""
    from src import workers
    from src.autowork import candidates_for

    workers.beat(slug, host=socket.gethostname(), pid=os.getpid())
    if budget.blocked(slug):
        return None
    try:
        locks.prune_expired()
    except Exception:  # noqa: BLE001
        pass
    for tid in candidates_for(slug, role):
        token = locks.claim_task(tid, slug)  # atomic — no other worker can take it
        if token:
            await _work_one(slug, tid, runner, token=token)
            return tid
    return None


async def run_worker(
    slug: str, *, runner: Optional[Runner] = None, once: bool = False,
    dry_run: Optional[bool] = None,
) -> Optional[int]:
    agent = registry.get(slug)
    if not agent:
        raise SystemExit(f"unknown agent: {slug!r}")
    if dry_run is None:
        dry_run = os.environ.get("WORKER_DRYRUN", "").lower() in ("1", "true", "yes")
    runner = runner or (_dry_runner if dry_run else _real_runner)
    role = agent.role
    logger.info("worker '%s' up (role=%s, dry_run=%s)", slug, role, dry_run)
    if once:
        return await tick_once(slug, role, runner)
    tick = max(5, settings.autowork_tick_seconds)
    while True:
        try:
            await tick_once(slug, role, runner)
        except Exception:  # noqa: BLE001 - never let the worker loop die
            logger.exception("worker tick failed")
        await asyncio.sleep(tick)


def main(argv: Optional[list[str]] = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m src.worker <agent-slug>", file=sys.stderr)
        raise SystemExit(2)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | worker | %(message)s",
    )
    registry.setup()
    asyncio.run(run_worker(argv[0]))


if __name__ == "__main__":
    main()
