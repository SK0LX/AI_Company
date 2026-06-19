"""Unified activity / audit log helper.

A single ``log(actor, action, target, **details)`` that appends an immutable
``AuditLog`` row and pushes a live event to the hub. It generalizes the ad-hoc
``tools._audit`` so every control-plane event — budget warn/block, approval
decisions, routine fires, self-modify runs, cost events — lands in ONE auditable
stream that ``collab.activity_feed`` (category ``system``) already surfaces.

Best-effort by contract: auditing must never break the action it records.
"""
from __future__ import annotations

import json
import logging

from src.db.engine import get_session
from src.db.models import AuditLog

logger = logging.getLogger(__name__)


def log(actor: str, action: str, target: str = "", **details: object) -> None:
    """Append a security/control-plane event and publish it live. Never raises."""
    try:
        with get_session() as session:
            session.add(AuditLog(
                actor=actor or "system",
                action=action,
                target=str(target),
                details_json=json.dumps(details, default=str),
            ))
            session.commit()
    except Exception:  # noqa: BLE001 - auditing must never break the caller
        logger.exception("failed to write activity log (%s/%s)", action, target)
        return
    try:
        from src.events import hub

        hub.publish({"event": "audit", "actor": actor or "system",
                     "action": action, "target": str(target)})
    except Exception:  # noqa: BLE001
        pass


def recent(limit: int = 80) -> list[dict]:
    """The latest audit entries (newest first) as plain dicts."""
    from sqlmodel import select

    with get_session() as session:
        rows = list(session.exec(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500)))
        ).all())
    return [
        {"ts": r.ts.isoformat(), "actor": r.actor, "action": r.action,
         "target": r.target, "details": _safe_json(r.details_json)}
        for r in rows
    ]


def _safe_json(s: str) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:  # noqa: BLE001
        return {}
