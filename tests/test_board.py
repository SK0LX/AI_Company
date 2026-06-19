"""Unit tests for task-board management: collab ops + board tools + permission
gating. Scoped to the normally-empty 'review'/'blocked' columns so it never
disturbs real tasks. No network.

    python tests/test_board.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab
from src.agents import tools as T
from src.registry import registry

_created: list[int] = []


def _task(title: str, status: str = "review") -> int:
    tid = collab.create_task(title, created_by=None, owner=None)
    _created.append(tid)
    if status != "new":
        collab.set_task_status(tid, status)
    return tid


def _collab_ops() -> None:
    a, b = _task("ut-board-a"), _task("ut-board-b")
    o = collab.board_overview()
    assert o["total"] >= 2 and o["by_status"]["review"] >= 2
    # delete one
    assert collab.delete_task(b) is True
    assert collab.delete_task(b) is False  # already gone
    _created.remove(b)
    # clear by column (delete) — only touches the 'review' column
    n = collab.clear_board(status="review", mode="delete")
    assert n >= 1
    assert collab.board_overview()["by_status"]["review"] == 0
    _created.remove(a)
    # cancel mode on the empty 'blocked' column
    c = _task("ut-board-c", status="blocked")
    collab.clear_board(status="blocked", mode="cancel")
    assert collab.board_overview()["by_status"]["blocked"] == 0
    _created.remove(c)


def _tools_and_gating() -> None:
    registry.setup()  # built-ins now hold can_manage_board

    # read is open to everyone
    T.set_current_agent("")  # no agent attached
    assert "Доска задач" in T.board_overview.invoke({})

    # a built-in (ceo) can mutate
    T.set_current_agent("ceo")
    d = _task("ut-board-d")
    assert T.board_set_status.invoke({"task_id": d, "status": "done"}).startswith("[task")
    assert "bad status" in T.board_set_status.invoke({"task_id": d, "status": "bogus"})
    assert T.board_delete.invoke({"task_id": d}).startswith("[deleted")
    _created.remove(d)
    # board_clear tool on the 'review' column
    e = _task("ut-board-e")
    res = T.board_clear.invoke({"status": "review", "mode": "delete"})
    assert "удалено задач" in res
    assert collab.board_overview()["by_status"]["review"] == 0
    _created.remove(e)

    # an agent WITHOUT can_manage_board is denied mutations but can still read
    registry.create_agent({"slug": "_ut_noboard", "name": "NB", "permissions": {}})
    try:
        T.set_current_agent("_ut_noboard")
        assert T.board_clear.invoke({"mode": "cancel"}).startswith("[denied")
        assert T.board_delete.invoke({"task_id": 1}).startswith("[denied")
        assert "Доска задач" in T.board_overview.invoke({})  # read allowed
    finally:
        registry.delete_agent("_ut_noboard")
    T.set_current_agent("")


def main() -> None:
    registry.setup()
    try:
        _collab_ops()
        _tools_and_gating()
    finally:
        for tid in list(_created):
            collab.delete_task(tid)
    print("board tests: OK")


if __name__ == "__main__":
    main()
