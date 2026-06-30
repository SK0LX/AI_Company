"""Per-agent persistent Claude sessions.

Each named agent keeps its OWN ``claude`` session per project, so a follow-up run
``--resume``s the same conversation instead of starting cold and re-reading the
whole project. We just persist the ``session_id`` Claude returns, keyed by
``(agent_slug, project)``, in a small JSON file next to the DB (survives restarts
via the mounted data volume). The session HISTORY itself lives in Claude's config
dir; this only remembers which id to resume.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from src.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _path() -> str:
    data_dir = os.path.dirname(os.path.abspath(settings.db_path)) or "."
    return os.path.join(data_dir, "agent_sessions.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt map must not break a run
        logger.exception("failed to read agent_sessions.json")
        return {}


def _save(data: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def _key(slug: str, project: str) -> str:
    return f"{slug}::{project}"


def get(slug: str, project: str) -> str | None:
    """The stored session id to ``--resume`` for this agent+project, or None."""
    with _lock:
        return _load().get(_key(slug, project)) or None


def remember(slug: str, project: str, session_id: str | None) -> None:
    """Persist the session id Claude returned so the next run resumes it."""
    if not session_id:
        return
    with _lock:
        data = _load()
        data[_key(slug, project)] = session_id
        try:
            _save(data)
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist agent session")


def forget(slug: str, project: str) -> None:
    """Drop a session (e.g. it failed to resume) so the next run starts fresh."""
    with _lock:
        data = _load()
        if data.pop(_key(slug, project), None) is not None:
            try:
                _save(data)
            except Exception:  # noqa: BLE001
                logger.exception("failed to forget agent session")
