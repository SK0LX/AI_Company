"""Shared per-project team memory — a plain ``TEAM_MEMORY.md`` in the project folder.

The relay agents each keep their OWN resumed Claude session (personal continuity),
but that means agent B doesn't automatically know what agent A just changed. This
file is the COMMON ground-truth they all share: the lead seeds it with the task,
the manager appends every agent's result, and each agent is told to read it before
working. It's a small file, so reading it each turn is cheap (unlike re-scanning the
whole repo).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

FILENAME = "TEAM_MEMORY.md"


def path_for(project_dir: str) -> str:
    return os.path.join(project_dir, FILENAME)


def seed(project_dir: str, task: str) -> None:
    """Start a fresh shared board for a task (overwrites any previous one)."""
    try:
        os.makedirs(project_dir, exist_ok=True)
        header = (
            "# TEAM_MEMORY — общая память команды\n\n"
            "Это общий контекст по текущей задаче. Каждый агент читает его перед работой "
            "и видит, что уже сделали коллеги.\n\n"
            f"## Задача\n{task.strip()}\n\n"
            "## Ход работы\n"
        )
        with open(path_for(project_dir), "w", encoding="utf-8") as f:
            f.write(header)
    except Exception:  # noqa: BLE001 - memory must never break a run
        logger.exception("failed to seed TEAM_MEMORY.md")


def append(project_dir: str, agent_label: str, note: str) -> None:
    """Record one agent's result onto the shared board."""
    try:
        line = f"\n### {agent_label}\n{(note or '').strip()[:1500]}\n"
        with open(path_for(project_dir), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        logger.exception("failed to append to TEAM_MEMORY.md")


def read(project_dir: str) -> str:
    """The current shared board (or "" if none)."""
    try:
        with open(path_for(project_dir), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001
        logger.exception("failed to read TEAM_MEMORY.md")
        return ""
