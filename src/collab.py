"""Collaboration layer (v2 stage 4): tasks, delegation-with-consent, help.

This is the substrate the orchestration uses to coordinate agents through the
new tables (``Task`` / ``TaskEvent`` / ``Delegation`` / ``HelpRequest``) and the
:mod:`src.bus`. It is deliberately split from the LLM:

* The persistence + state-machine functions (create_task, record_event,
  open_delegation, …) take NO model calls — pure DB, cheap, always safe to run.
* The negotiation (does B accept the hand-off? who helps?) takes an injected
  ``decider`` / ``picker`` callable, so the live code can plug an LLM in while the
  tests pass deterministic stubs. This keeps stage 4 testable without spending
  the (scarce) LLM quota.

See ``docs/SPEC_v2.md`` §2, §3, §5.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

from sqlmodel import select

from src import bus as busmod
from src.db.engine import get_session
from src.db.models import Delegation, HelpRequest, Task, TaskEvent
from src.registry import registry

logger = logging.getLogger(__name__)

# A decider answers "does agent <to_agent> accept this hand-off?" -> (accept, why)
Decider = Callable[[str, str, str], Awaitable[tuple[bool, str]]]
# A picker chooses a helper slug for a stuck task, or None if nobody fits.
Picker = Callable[[str, str, list[str]], Awaitable[Optional[str]]]


def _agent_id(slug: Optional[str]) -> Optional[int]:
    agent = registry.get(slug) if slug else None
    return agent.id if agent else None


# --- tasks + timeline -------------------------------------------------------

def create_task(
    title: str,
    description: str = "",
    *,
    created_by: Optional[str] = None,
    owner: Optional[str] = None,
    parent_task_id: Optional[int] = None,
) -> int:
    """Create a task and log a ``created`` event. ``created_by``/``owner`` are
    agent slugs (None = the user). Returns the new task id."""
    with get_session() as session:
        task = Task(
            title=title,
            description=description,
            status="new",
            created_by=_agent_id(created_by),
            owner_agent_id=_agent_id(owner),
            parent_task_id=parent_task_id,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id
    record_event(task_id, created_by, "created", title=title)
    return task_id


def record_event(task_id: int, actor: Optional[str], type: str, **payload: object) -> None:
    """Append an entry to a task's timeline. ``actor`` is an agent slug or None."""
    with get_session() as session:
        session.add(
            TaskEvent(
                task_id=task_id,
                actor_agent_id=_agent_id(actor),
                type=type,
                payload_json=json.dumps(payload, default=str),
            )
        )
        session.commit()


def set_task_status(task_id: int, status: str, *, actor: Optional[str] = None) -> None:
    with get_session() as session:
        task = session.get(Task, task_id)
        if not task:
            return
        task.status = status
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()
    record_event(task_id, actor, "status", status=status)


def task_events(task_id: int) -> list[dict]:
    """The task's timeline as plain dicts (for the admin UI / tests)."""
    with get_session() as session:
        rows = session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
        ).all()
        return [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "actor_agent_id": r.actor_agent_id,
                "type": r.type,
                "payload": json.loads(r.payload_json or "{}"),
            }
            for r in rows
        ]


# --- delegation with consent ------------------------------------------------

def open_delegation(
    task_id: Optional[int],
    from_agent: str,
    to_agent: str,
    *,
    reason: str = "",
    kind: str = "task",
) -> int:
    """Record a pending A→B hand-off + a ``delegated`` task event. Returns its id."""
    with get_session() as session:
        deleg = Delegation(
            task_id=task_id,
            from_agent_id=_agent_id(from_agent),
            to_agent_id=_agent_id(to_agent),
            kind=kind,
            status="pending",
            reason=reason,
        )
        session.add(deleg)
        session.commit()
        session.refresh(deleg)
        deleg_id = deleg.id
    if task_id is not None:
        record_event(task_id, from_agent, "delegated", to=to_agent, reason=reason,
                     delegation_id=deleg_id, kind=kind)
    return deleg_id


def close_delegation(
    delegation_id: int, status: str, *, actor: Optional[str] = None, reason: str = ""
) -> Optional[int]:
    """Resolve a delegation (accepted|declined|revoked). On ``accepted`` the task
    is reassigned to the recipient and moved to in_progress. Returns the task id."""
    with get_session() as session:
        deleg = session.get(Delegation, delegation_id)
        if not deleg:
            return None
        deleg.status = status
        session.add(deleg)
        if status == "accepted" and deleg.task_id is not None:
            task = session.get(Task, deleg.task_id)
            if task:
                task.owner_agent_id = deleg.to_agent_id
                task.status = "in_progress"
                task.updated_at = datetime.utcnow()
                session.add(task)
        session.commit()
        task_id = deleg.task_id
    if task_id is not None:
        record_event(task_id, actor, status, reason=reason, delegation_id=delegation_id)
    return task_id


async def negotiate_delegation(
    *,
    task_id: Optional[int],
    from_agent: str,
    to_agent: str,
    task_text: str,
    reason: str,
    decider: Decider,
    bus: Optional[busmod.MessageBus] = None,
) -> tuple[bool, str]:
    """Full consent hand-off: record the request, ask B (via ``decider``) to
    accept or decline, persist the outcome, and emit the bus messages. Returns
    ``(accepted, why)``."""
    bus = bus or busmod.bus
    deleg_id = open_delegation(task_id, from_agent, to_agent, reason=reason)
    await bus.publish(
        busmod.Delegate(from_agent=from_agent, to_agent=to_agent, task_id=task_id, reason=reason)
    )
    try:
        accepted, why = await decider(to_agent, task_text, reason)
    except Exception:  # noqa: BLE001 - a decider failure must not strand the task
        logger.exception("delegation decider failed for %s", to_agent)
        accepted, why = True, "auto-accepted (decider error)"
    close_delegation(deleg_id, "accepted" if accepted else "declined",
                     actor=to_agent, reason=why)
    reply = busmod.Accept if accepted else busmod.Decline
    await bus.publish(
        reply(from_agent=to_agent, to_agent=from_agent, task_id=task_id, ref=deleg_id, reason=why)
    )
    return accepted, why


# --- help between agents ----------------------------------------------------

def open_help(
    task_id: Optional[int], requester: str, summary: str, scope: Optional[list[str]] = None
) -> int:
    with get_session() as session:
        hr = HelpRequest(
            task_id=task_id, requester_id=_agent_id(requester), summary=summary, status="open"
        )
        session.add(hr)
        session.commit()
        session.refresh(hr)
        help_id = hr.id
    if task_id is not None:
        record_event(task_id, requester, "help_requested", summary=summary, scope=scope or [],
                     help_id=help_id)
    return help_id


def assign_help(help_id: int, helper: str, *, actor: Optional[str] = None) -> Optional[int]:
    with get_session() as session:
        hr = session.get(HelpRequest, help_id)
        if not hr:
            return None
        hr.helper_id = _agent_id(helper)
        hr.status = "assigned"
        session.add(hr)
        session.commit()
        task_id = hr.task_id
    if task_id is not None:
        record_event(task_id, actor or helper, "help_assigned", helper=helper, help_id=help_id)
    return task_id


def resolve_help(help_id: int, *, summary: str = "", actor: Optional[str] = None) -> Optional[int]:
    with get_session() as session:
        hr = session.get(HelpRequest, help_id)
        if not hr:
            return None
        hr.status = "resolved"
        if summary:
            hr.summary = summary
        session.add(hr)
        session.commit()
        task_id = hr.task_id
    if task_id is not None:
        record_event(task_id, actor, "help_resolved", summary=summary, help_id=help_id)
    return task_id


async def request_help(
    *,
    task_id: Optional[int],
    requester: str,
    summary: str,
    scope: Optional[list[str]] = None,
    candidates: list[str],
    picker: Picker,
    bus: Optional[busmod.MessageBus] = None,
) -> Optional[str]:
    """Open a help request, pick a helper from ``candidates`` (via ``picker``),
    assign them, and emit the bus message. Returns the chosen helper slug or None."""
    bus = bus or busmod.bus
    help_id = open_help(task_id, requester, summary, scope)
    await bus.publish(
        busmod.HelpRequest(from_agent=requester, task_id=task_id, summary=summary, scope=scope or [])
    )
    try:
        helper = await picker(requester, summary, candidates)
    except Exception:  # noqa: BLE001
        logger.exception("help picker failed")
        helper = None
    if helper:
        assign_help(help_id, helper, actor=requester)
    return helper
