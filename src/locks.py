"""Atomic task claim + resource locks (AI Office v3 pull-coordination).

The invariant: an agent never works on a shared task/resource without a successful
**compare-and-set** acquire — so two agents can never grab the same thing, even if
they ask "is anyone on this?" at the same instant. "Asking the CEO" is just a read
for the UX message; the truth is the atomic claim here.

All mutating ops run as a single DB statement (UPDATE … WHERE … / INSERT) and
report success via ``rowcount``/unique-constraint, which SQLite executes
atomically. Reads use a normal session.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.db.engine import engine, get_session
from src.db.models import ResourceLock, Task

logger = logging.getLogger(__name__)

_TASK = Task.__tablename__
_LOCK = ResourceLock.__tablename__
DEFAULT_TTL = 600  # seconds a resource lock lives without a renew/heartbeat


# --- task claim (compare-and-set on the task row) ---------------------------

def claim_task(task_id: int, agent: str) -> Optional[str]:
    """Atomically check out a task. Returns a lock token on success, or None if
    another agent already holds it (or it's done/cancelled). Re-claiming a task
    you already hold is idempotent."""
    token = secrets.token_hex(8)
    now = datetime.utcnow()
    with engine.begin() as conn:
        res = conn.execute(
            text(f"""
                UPDATE {_TASK}
                   SET claimed_by=:agent, claimed_at=:now, lock_token=:tok,
                       status='in_progress', updated_at=:now
                 WHERE id=:tid
                   AND (claimed_by='' OR claimed_by IS NULL OR claimed_by=:agent)
                   AND status IN ('new','in_progress','review')
            """),
            {"agent": agent, "now": now, "tok": token, "tid": task_id},
        )
        if res.rowcount == 1:
            return token
    return None


def release_task(task_id: int, agent: str) -> bool:
    """Release a task you hold (no-op if you don't hold it)."""
    with engine.begin() as conn:
        res = conn.execute(
            text(f"""
                UPDATE {_TASK} SET claimed_by='', lock_token='', claimed_at=NULL, updated_at=:now
                 WHERE id=:tid AND claimed_by=:agent
            """),
            {"tid": task_id, "agent": agent, "now": datetime.utcnow()},
        )
        return res.rowcount == 1


def task_holder(task_id: int) -> Optional[str]:
    """Who currently holds the task (for the 'занят X' UX), or None if free."""
    with get_session() as session:
        t = session.get(Task, task_id)
        return (t.claimed_by or None) if t else None


# --- resource locks (advisory, keyed, with TTL) -----------------------------

def acquire_lock(key: str, agent: str, *, ttl: int = DEFAULT_TTL) -> Optional[str]:
    """Atomically acquire the lock for ``key`` (e.g. 'repo:proj', 'file:src/app.py').
    Succeeds if free / expired / already yours. Returns a token, or None if held by
    someone else and not expired."""
    token = secrets.token_hex(8)
    now = datetime.utcnow()
    exp = now + timedelta(seconds=max(5, ttl))
    with engine.begin() as conn:
        # Take over an existing row if it's free, ours, or expired.
        res = conn.execute(
            text(f"""
                UPDATE {_LOCK} SET agent=:agent, token=:tok, acquired_at=:now, expires_at=:exp
                 WHERE key=:key AND (agent='' OR agent=:agent OR expires_at < :now)
            """),
            {"agent": agent, "tok": token, "now": now, "exp": exp, "key": key},
        )
        if res.rowcount == 1:
            return token
        # No row yet → insert. The UNIQUE(key) makes concurrent inserts race-safe.
        try:
            conn.execute(
                text(f"INSERT INTO {_LOCK} (key, agent, token, acquired_at, expires_at) "
                     "VALUES (:key, :agent, :tok, :now, :exp)"),
                {"key": key, "agent": agent, "tok": token, "now": now, "exp": exp},
            )
            return token
        except IntegrityError:
            return None  # someone inserted/holds it concurrently


def release_lock(key: str, *, agent: str = "", token: str = "") -> bool:
    """Release a lock you hold, matched by agent or token."""
    with engine.begin() as conn:
        res = conn.execute(
            text(f"DELETE FROM {_LOCK} WHERE key=:key AND "
                 "(:agent != '' AND agent=:agent OR :token != '' AND token=:token)"),
            {"key": key, "agent": agent, "token": token},
        )
        return res.rowcount >= 1


def renew_lock(key: str, token: str, *, ttl: int = DEFAULT_TTL) -> bool:
    """Heartbeat: extend a lock you hold so it doesn't expire mid-work."""
    now = datetime.utcnow()
    with engine.begin() as conn:
        res = conn.execute(
            text(f"UPDATE {_LOCK} SET expires_at=:exp WHERE key=:key AND token=:token"),
            {"exp": now + timedelta(seconds=max(5, ttl)), "key": key, "token": token},
        )
        return res.rowcount == 1


def who_holds(key: str) -> Optional[str]:
    """The agent holding ``key``, or None if free/expired."""
    with get_session() as session:
        row = session.exec(select(ResourceLock).where(ResourceLock.key == key)).first()
    if not row:
        return None
    if row.expires_at and row.expires_at < datetime.utcnow():
        return None
    return row.agent or None


def prune_expired() -> int:
    """Delete expired resource locks. Returns how many were freed."""
    with engine.begin() as conn:
        res = conn.execute(
            text(f"DELETE FROM {_LOCK} WHERE expires_at IS NOT NULL AND expires_at < :now"),
            {"now": datetime.utcnow()},
        )
        return res.rowcount or 0


def list_locks() -> list[dict]:
    """Active (non-expired) locks for the board/UX."""
    now = datetime.utcnow()
    with get_session() as session:
        rows = list(session.exec(select(ResourceLock)).all())
    return [
        {"key": r.key, "agent": r.agent,
         "expires_at": r.expires_at.isoformat() if r.expires_at else None}
        for r in rows if not (r.expires_at and r.expires_at < now)
    ]
