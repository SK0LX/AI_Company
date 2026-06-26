"""Worker liveness registry (v3 Ф3-full).

Each per-agent worker process/container writes a heartbeat; the gateway and
dashboard read who's alive. One row per agent slug (upserted).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import WorkerHeartbeat

logger = logging.getLogger(__name__)

ALIVE_WINDOW = 60  # seconds since the last beat to still count a worker as alive


def beat(agent: str, *, host: str = "", pid: int = 0) -> None:
    """Record/refresh this worker's heartbeat. Best-effort."""
    try:
        with get_session() as session:
            row = session.exec(
                select(WorkerHeartbeat).where(WorkerHeartbeat.agent == agent)
            ).first()
            if row:
                row.host, row.pid, row.last_seen = host, pid, datetime.utcnow()
                session.add(row)
            else:
                session.add(WorkerHeartbeat(agent=agent, host=host, pid=pid))
            session.commit()
    except Exception:  # noqa: BLE001 - a heartbeat hiccup must not kill a worker
        logger.exception("heartbeat failed for %s", agent)


def is_alive(agent: str, *, within: int = ALIVE_WINDOW) -> bool:
    with get_session() as session:
        row = session.exec(
            select(WorkerHeartbeat).where(WorkerHeartbeat.agent == agent)
        ).first()
    return bool(row and row.last_seen >= datetime.utcnow() - timedelta(seconds=within))


def alive_agents(*, within: int = ALIVE_WINDOW) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(seconds=within)
    with get_session() as session:
        rows = list(session.exec(select(WorkerHeartbeat)).all())
    return {r.agent for r in rows if r.last_seen >= cutoff}


def all_workers(*, within: int = ALIVE_WINDOW) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(seconds=within)
    with get_session() as session:
        rows = list(session.exec(select(WorkerHeartbeat).order_by(WorkerHeartbeat.agent)).all())
    return [
        {"agent": r.agent, "host": r.host, "pid": r.pid,
         "last_seen": r.last_seen.isoformat(), "alive": r.last_seen >= cutoff}
        for r in rows
    ]
