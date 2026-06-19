"""Unit tests for the Задачник task tracker: event formatting + the service
(channel gating + dedup). No network.

    python tests/test_tasktracker.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab, tasktracker
from src.config import settings
from src.registry import registry


def _ev(type_, tid, **payload):
    actor = payload.pop("_actor", None)
    return {"event": "task_event", "type": type_, "task_id": tid, "actor": actor, "payload": payload}


def _formatting() -> None:
    assert tasktracker.format_event(_ev("created", 5, title="Сделать X")) == "🆕 Задача #5: Сделать X"
    assert tasktracker.format_event(_ev("created", 5)) == "🆕 Задача #5 создана"

    deleg = tasktracker.format_event(_ev("delegated", 5, _actor="ceo", to="developer"))
    assert deleg.startswith("➡️ #5 → ") and "(от" in deleg

    assert tasktracker.format_event(_ev("status", 5, status="in_progress")).startswith("🚧 #5 в работе")
    assert tasktracker.format_event(_ev("status", 5, status="cancelled")) == "🗑 #5 отменена"
    assert tasktracker.format_event(_ev("status", 5, status="review")) == "👀 #5 на ревью"

    # noise events / non-task events are skipped
    assert tasktracker.format_event(_ev("thought", 5)) is None
    assert tasktracker.format_event(_ev("result", 5, chars=10)) is None
    assert tasktracker.format_event({"event": "audit"}) is None

    # "done" enriches with исполнитель + от from the real task
    tid = collab.create_task("ut-tracker-done", created_by="ceo", owner="developer")
    try:
        out = tasktracker.format_event(_ev("status", tid, status="done"))
        assert out.startswith(f"✅ Задача #{tid} закрыта · исполнитель: ") and "от:" in out
    finally:
        collab.delete_task(tid)


def _service() -> None:
    posts: list[str] = []

    async def sender(text: str) -> None:
        posts.append(text)

    svc = tasktracker.TaskTrackerService(sender)

    async def run() -> None:
        settings.task_channel_id = 0  # off -> nothing posts
        assert await svc.handle(_ev("created", 1, title="A")) is None
        assert posts == []

        settings.task_channel_id = 123  # on
        r = await svc.handle(_ev("created", 1, title="A"))
        assert r == "🆕 Задача #1: A" and posts == [r]
        # identical event is de-duplicated
        assert await svc.handle(_ev("created", 1, title="A")) is None
        assert len(posts) == 1
        # non-task event ignored
        assert await svc.handle({"event": "audit"}) is None

    asyncio.run(run())


def main() -> None:
    registry.setup()
    ch0 = settings.task_channel_id
    try:
        _formatting()
        _service()
    finally:
        settings.task_channel_id = ch0
    print("task tracker tests: OK")


if __name__ == "__main__":
    main()
