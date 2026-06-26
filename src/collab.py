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
from src.db.models import (
    TASK_STATUSES,
    AuditLog,
    Delegation,
    HelpRequest,
    Message,
    Task,
    TaskEvent,
)
from src.registry import registry

logger = logging.getLogger(__name__)

# A decider answers "does agent <to_agent> accept this hand-off?" -> (accept, why)
Decider = Callable[[str, str, str], Awaitable[tuple[bool, str]]]
# A picker chooses a helper slug for a stuck task, or None if nobody fits.
Picker = Callable[[str, str, list[str]], Awaitable[Optional[str]]]


def _agent_id(slug: Optional[str]) -> Optional[int]:
    agent = registry.get(slug) if slug else None
    return agent.id if agent else None


def _slug_by_id() -> dict[int, str]:
    return {a.id: a.slug for a in registry.list_agents() if a.id is not None}


def _name_by_id() -> dict[int, str]:
    return {a.id: a.name for a in registry.list_agents() if a.id is not None}


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
    """Append an entry to a task's timeline. ``actor`` is an agent slug or None.
    Also pushes the event to the live hub (best-effort) for the admin board."""
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
    try:
        from src.events import hub

        hub.publish({"event": "task_event", "task_id": task_id, "type": type,
                     "actor": actor, "payload": payload})
    except Exception:  # noqa: BLE001 - the live push must never break recording
        logger.exception("failed to publish live event")


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


# --- board reads (stage 6) --------------------------------------------------

def list_tasks() -> list[dict]:
    """All tasks for the kanban: owner/creator slugs, subtask count, timestamps."""
    by_slug = _slug_by_id()
    with get_session() as session:
        tasks = session.exec(select(Task).order_by(Task.id.desc())).all()
        children: dict[int, int] = {}
        for t in session.exec(select(Task)).all():
            if t.parent_task_id:
                children[t.parent_task_id] = children.get(t.parent_task_id, 0) + 1
        return [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "owner": by_slug.get(t.owner_agent_id),
                "created_by": by_slug.get(t.created_by),
                "parent_task_id": t.parent_task_id,
                "subtasks": children.get(t.id, 0),
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t in tasks
        ]


def get_task(task_id: int) -> Optional[dict]:
    by_slug = _slug_by_id()
    with get_session() as session:
        t = session.get(Task, task_id)
        if not t:
            return None
        owner = by_slug.get(t.owner_agent_id)
        creator = by_slug.get(t.created_by)
        closed = t.status in ("done", "cancelled")
        return {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "owner": owner,
            "owner_name": registry.label(owner) if owner else None,
            "created_by": creator,
            "from_name": registry.label(creator) if creator else "Пользователь",
            "to_name": registry.label(owner) if owner else "—",
            "priority": t.priority or "обычный",
            "complexity": t.complexity or 1,
            "parent_task_id": t.parent_task_id,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "closed_at": t.updated_at.isoformat() if closed else None,
        }


def home_summary() -> dict:
    """The AI-Office home view: tasks-closed-today, team with working/idle status,
    workload split, and column counts."""
    by_slug = _slug_by_id()
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {st: 0 for st in TASK_STATUSES}
    closed_today = 0
    busy: dict[str, int] = {}
    with get_session() as session:
        for t in session.exec(select(Task)).all():
            counts[t.status] = counts.get(t.status, 0) + 1
            if t.status == "done" and t.updated_at and t.updated_at >= today:
                closed_today += 1
            if t.status == "in_progress":
                owner = by_slug.get(t.owner_agent_id)
                if owner:
                    busy[owner] = busy.get(owner, 0) + 1
    try:
        from src import workers

        alive = workers.alive_agents()
    except Exception:  # noqa: BLE001
        alive = set()
    team = [
        {"slug": a.slug, "name": a.name, "role": a.role,
         "status": "working" if busy.get(a.slug) else "idle",
         "active_tasks": busy.get(a.slug, 0), "is_lead": a.slug == "ceo",
         "worker": a.slug in alive}
        for a in registry.list_agents(enabled_only=True)
    ]
    total_active = sum(busy.values())
    workload = [
        {"slug": s, "name": registry.label(s), "count": n,
         "share": round(100 * n / total_active) if total_active else 0}
        for s, n in sorted(busy.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "closed_today": closed_today,
        "total": sum(counts.values()),
        "by_status": counts,
        "active": total_active,
        "team": team,
        "workload": workload,
    }


# --- board management (agents can tidy the kanban the user sees) -------------

def board_overview() -> dict:
    """Counts per column + total, for an agent to see the board at a glance."""
    with get_session() as session:
        tasks = session.exec(select(Task)).all()
    counts = {st: 0 for st in TASK_STATUSES}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    return {"total": len(tasks), "by_status": counts}


def delete_task(task_id: int) -> bool:
    """Permanently remove ONE task and everything tied to it (events, help requests,
    delegations) so no orphan rows survive a rowid reuse. False if missing."""
    with get_session() as session:
        task = session.get(Task, task_id)
        if not task:
            return False
        for ev in session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)).all():
            session.delete(ev)
        for hr in session.exec(select(HelpRequest).where(HelpRequest.task_id == task_id)).all():
            session.delete(hr)
        for d in session.exec(select(Delegation).where(Delegation.task_id == task_id)).all():
            session.delete(d)
        session.delete(task)
        session.commit()
    return True


def clear_board(*, status: Optional[str] = None, mode: str = "cancel") -> int:
    """Bulk-tidy the board. ``mode='cancel'`` moves matched tasks to the
    'cancelled' column (reversible); ``mode='delete'`` removes them and their
    events permanently. ``status`` limits to one column (e.g. 'done'); None = all.
    Returns how many tasks were affected."""
    with get_session() as session:
        stmt = select(Task)
        if status:
            stmt = stmt.where(Task.status == status)
        tasks = session.exec(stmt).all()
        n = len(tasks)
        if mode == "delete":
            for t in tasks:
                # Mirror delete_task: drop every dependent row so no orphan
                # HelpRequest/Delegation survives a later rowid reuse.
                for ev in session.exec(select(TaskEvent).where(TaskEvent.task_id == t.id)).all():
                    session.delete(ev)
                for hr in session.exec(select(HelpRequest).where(HelpRequest.task_id == t.id)).all():
                    session.delete(hr)
                for d in session.exec(select(Delegation).where(Delegation.task_id == t.id)).all():
                    session.delete(d)
                session.delete(t)
        else:  # cancel
            now = datetime.utcnow()
            for t in tasks:
                t.status = "cancelled"
                t.updated_at = now
                session.add(t)
        session.commit()
    return n


def task_timeline(task_id: int) -> list[dict]:
    """Task events enriched with the actor's slug (for the timeline view)."""
    by_slug = _slug_by_id()
    return [
        {**e, "actor": by_slug.get(e["actor_agent_id"])}
        for e in task_events(task_id)
    ]


def recent_messages(limit: int = 50) -> list[dict]:
    """The latest MessageBus traffic (newest first)."""
    by_slug = _slug_by_id()
    with get_session() as session:
        rows = session.exec(
            select(Message).order_by(Message.id.desc()).limit(limit)
        ).all()
        return [
            {
                "id": m.id,
                "ts": m.ts.isoformat(),
                "from": by_slug.get(m.from_agent_id),
                "to": by_slug.get(m.to_agent_id),
                "kind": m.kind,
                "text": m.text,
            }
            for m in rows
        ]


_EVENT_VERB = {
    "created": "создал задачу",
    "delegated": "делегировал",
    "accepted": "принял",
    "declined": "отклонил",
    "result": "выдал результат",
    "done": "завершил",
    "status": "статус",
    "help_requested": "просит помощь",
    "help_assigned": "назначил помощника",
    "help_resolved": "помог",
    "thought": "размышляет",
}


def _describe_event(etype: str, payload: dict) -> str:
    """Short human-readable line for a task event (for the activity feed)."""
    p = payload or {}
    if etype == "thought":
        return (p.get("text") or p.get("note") or "").strip()
    if etype == "created":
        return p.get("title") or ""
    if etype == "delegated":
        to = p.get("to") or "?"
        reason = (p.get("reason") or "").strip()
        return f"→ {to}" + (f": {reason}" if reason else "")
    if etype == "declined":
        return (p.get("reason") or "").strip()
    if etype == "result":
        return f"{p.get('chars', 0)} симв."
    if etype == "status":
        return p.get("status") or ""
    if etype in ("help_requested",):
        return (p.get("summary") or "").strip()
    if etype in ("help_assigned",):
        return p.get("helper") or ""
    return ""


def activity_feed(category: str = "all", limit: int = 80) -> list[dict]:
    """Unified, time-sorted activity stream for the dashboard.

    Categories: ``tasks`` (task work), ``thoughts`` (agent reasoning / Сознания),
    ``system`` (audit log), or ``all``. Each item: ts, category, actor, type,
    task_id, text, verb."""
    by_slug = _slug_by_id()
    items: list[dict] = []
    with get_session() as session:
        if category in ("all", "tasks", "thoughts"):
            rows = session.exec(
                select(TaskEvent).order_by(TaskEvent.id.desc()).limit(limit * 3)
            ).all()
            for e in rows:
                is_thought = e.type == "thought"
                cat = "thoughts" if is_thought else "tasks"
                if category == "tasks" and is_thought:
                    continue
                if category == "thoughts" and not is_thought:
                    continue
                payload = {}
                try:
                    payload = json.loads(e.payload_json or "{}")
                except Exception:  # noqa: BLE001
                    pass
                items.append({
                    "ts": e.ts.isoformat(),
                    "category": cat,
                    "actor": by_slug.get(e.actor_agent_id),
                    "type": e.type,
                    "verb": _EVENT_VERB.get(e.type, e.type),
                    "task_id": e.task_id,
                    "text": _describe_event(e.type, payload),
                })
        if category in ("all", "system"):
            for a in session.exec(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
            ).all():
                items.append({
                    "ts": a.ts.isoformat(),
                    "category": "system",
                    "actor": a.actor,
                    "type": a.action,
                    "verb": a.action,
                    "task_id": None,
                    "text": a.target,
                })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def office_state() -> dict:
    """A live snapshot of the team for the office map: each agent with role,
    busy flag (owns an in-progress task), current task, and last-seen time, plus
    the delegation edges between them."""
    by_slug = _slug_by_id()
    roster = {a.slug: a for a in registry.list_agents(enabled_only=False)}
    busy: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    with get_session() as session:
        for t in session.exec(select(Task).where(Task.status == "in_progress")).all():
            owner = by_slug.get(t.owner_agent_id)
            if owner and owner not in busy:
                busy[owner] = t.title
        for e in session.exec(
            select(TaskEvent).order_by(TaskEvent.id.desc()).limit(300)
        ).all():
            slug = by_slug.get(e.actor_agent_id)
            if slug and slug not in last_seen:
                last_seen[slug] = e.ts.isoformat()
    graph = interaction_graph()
    nodes = []
    for n in graph["nodes"]:
        agent = roster.get(n["slug"])
        nodes.append({
            "slug": n["slug"],
            "name": n["name"],
            "role": agent.role if agent else "",
            "enabled": agent.enabled if agent else True,
            "busy": n["slug"] in busy,
            "current_task": busy.get(n["slug"]),
            "last_seen": last_seen.get(n["slug"]),
        })
    return {"nodes": nodes, "edges": graph["edges"]}


def interaction_graph() -> dict:
    """Agents as nodes, accepted/total delegations between them as weighted edges."""
    by_slug, by_name = _slug_by_id(), _name_by_id()
    nodes = [{"slug": s, "name": by_name.get(i, s)} for i, s in by_slug.items()]
    edges: dict[tuple[str, str], dict] = {}
    with get_session() as session:
        for d in session.exec(select(Delegation)).all():
            frm, to = by_slug.get(d.from_agent_id), by_slug.get(d.to_agent_id)
            if not frm or not to:
                continue
            edge = edges.setdefault((frm, to), {"from": frm, "to": to, "count": 0, "accepted": 0})
            edge["count"] += 1
            if d.status == "accepted":
                edge["accepted"] += 1
    return {"nodes": nodes, "edges": list(edges.values())}
