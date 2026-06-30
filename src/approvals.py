"""Human-in-the-loop approvals (typed).

Originally this only gated shell commands. It now records every approval as a
typed, audited :class:`~src.db.models.Approval` row — ``shell``, ``self_modify``,
``budget_override``, ``risky_delete`` — while keeping the exact same Telegram
asker mechanism:

- The bot installs an "asker" (via :func:`set_asker`) for the current run — a
  coroutine that shows a prompt to the user with approve/skip buttons and returns
  ``True``/``False``. It's stored in a ``ContextVar`` so concurrent runs in
  different chats each carry their own asker.
- A tool/orchestrator calls :func:`request_approval` (or the back-compat
  :func:`request_command_approval`), which records a pending row, asks the user,
  records the decision, and returns the bool.

If no asker is installed (e.g. a headless run), the request is denied by default
— better safe than sorry.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

from src.config import settings

logger = logging.getLogger(__name__)

Asker = Callable[[str], Awaitable[bool]]

_asker: contextvars.ContextVar[Optional[Asker]] = contextvars.ContextVar(
    "command_asker", default=None
)

# Live, web-resolvable futures for in-flight approvals, keyed by approval id. The
# dashboard can decide a pending approval (POST /api/approvals/{id}/decide) and
# it races the Telegram asker — whichever answers first wins.
_pending: dict[int, "asyncio.Future"] = {}

# Optional notifier: push an Allow/Deny button into Telegram for approvals that
# have NO contextvar asker (e.g. the Claude-engine permission gate, which is
# web-resolvable). fn(approval_id, kind, summary, agent) — best-effort, the bot
# installs it (see TelegramManager.notify_approval). Lets the user approve from
# the phone instead of only the web dashboard.
ApprovalNotifier = Callable[[int, str, str, str], None]
_approval_notifier: Optional[ApprovalNotifier] = None


def set_approval_notifier(fn: Optional[ApprovalNotifier]) -> None:
    """Install (or clear) the Telegram approval-button notifier."""
    global _approval_notifier
    _approval_notifier = fn

_KIND_LABEL = {
    "shell": "Запустить команду",
    "self_modify": "Изменить собственный код",
    "budget_override": "Превысить бюджет",
    "risky_delete": "Удалить файлы",
    # Claude-engine per-category tool approvals (see src/claude_perms.py)
    "agent_read": "Агент: чтение файлов",
    "agent_edit": "Агент: правка файлов",
    "agent_exec": "Агент: команда (bash/git)",
    "agent_net": "Агент: сеть (web/загрузка)",
}


def set_asker(fn: Optional[Asker]) -> None:
    """Install the approval function for the current execution context."""
    _asker.set(fn)


def clear_asker() -> None:
    _asker.set(None)


def has_asker() -> bool:
    return _asker.get() is not None


# --- persistence (best-effort; never blocks the actual ask) -----------------

def _record_pending(kind: str, summary: str, requested_by: str) -> Optional[int]:
    try:
        from src.db.engine import get_session
        from src.db.models import Approval

        with get_session() as session:
            row = Approval(kind=kind, summary=summary[:1000], status="pending",
                           requested_by=requested_by or "system")
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id
    except Exception:  # noqa: BLE001 - recording must never block an approval
        logger.exception("failed to record pending approval")
        return None


def _record_decision(approval_id: Optional[int], approved: bool, *, reason: str = "") -> None:
    status = "approved" if approved else "denied"
    if approval_id is not None:
        try:
            from src.db.engine import get_session
            from src.db.models import Approval

            with get_session() as session:
                row = session.get(Approval, approval_id)
                if row:
                    row.status = status
                    row.decided_by = "user"
                    row.reason = reason
                    row.decided_at = datetime.utcnow()
                    session.add(row)
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to record approval decision")
    try:
        from src import activity

        activity.log("user", f"approval_{status}", "", approval_id=approval_id, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def _format(kind: str, summary: str) -> str:
    """What the user sees. Plain command for shell (unchanged UX); labeled else."""
    if kind == "shell":
        return summary
    return f"[{_KIND_LABEL.get(kind, kind)}] {summary}"


# --- the public approval API ------------------------------------------------

async def request_approval(
    kind: str, summary: str, *, agent: str = "system", require_asker: bool = True,
) -> bool:
    """Record a pending approval, ask the user, record + return the decision.

    The Telegram asker (if installed) and the dashboard (:func:`decide`) race —
    whichever answers first wins, bounded by ``command_approval_timeout``. The
    dashboard resolves through a module-global future, so it works even when this
    runs in an async context with no asker (e.g. the Claude-engine permission
    callback). ``require_asker=True`` keeps the old behaviour — deny at once if no
    asker is installed; pass ``False`` to allow web-only approval."""
    approval_id = _record_pending(kind, summary, agent)
    fn = _asker.get()
    if fn is None and require_asker:
        _record_decision(approval_id, False, reason="no approver installed")
        return False

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    if approval_id is not None:
        _pending[approval_id] = fut

    # No contextvar asker (e.g. a Claude-engine permission gate) but a Telegram
    # notifier is installed → push an Allow/Deny button to the chat so the user can
    # approve from the phone. It resolves the SAME future via decide(), racing the
    # web dashboard. (When fn is set, the shell asker already sends its own buttons.)
    if fn is None and _approval_notifier is not None and approval_id is not None:
        try:
            _approval_notifier(approval_id, kind, summary, agent)
        except Exception:  # noqa: BLE001 - a broken notifier must not block the gate
            logger.exception("approval notifier failed")

    async def _ask_telegram() -> None:
        try:
            ok = bool(await fn(_format(kind, summary)))
        except Exception:  # noqa: BLE001 - a broken channel must not act
            ok = False
        if not fut.done():
            fut.set_result((ok, "telegram"))

    tg_task = asyncio.create_task(_ask_telegram()) if fn is not None else None
    try:
        approved, via = await asyncio.wait_for(fut, timeout=settings.command_approval_timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        approved, via = False, "timeout"
    finally:
        if tg_task is not None:
            tg_task.cancel()
        if approval_id is not None:
            _pending.pop(approval_id, None)
    _record_decision(approval_id, approved, reason=via)
    return approved


def decide(approval_id: int, approved: bool, *, reason: str = "web") -> bool:
    """Resolve a pending approval from the dashboard. Resolves the live waiter if
    one exists; otherwise settles the DB row directly (e.g. after a restart)."""
    fut = _pending.get(approval_id)
    if fut is not None and not fut.done():
        fut.set_result((bool(approved), reason))
        return True
    _record_decision(approval_id, bool(approved), reason=reason)
    return True


def pending(limit: int = 50) -> list[dict]:
    """Approvals still awaiting a decision (for the dashboard's action list)."""
    from sqlmodel import select

    from src.db.engine import get_session
    from src.db.models import Approval

    with get_session() as session:
        rows = list(session.exec(
            select(Approval).where(Approval.status == "pending")
            .order_by(Approval.id.desc()).limit(max(1, min(limit, 200)))
        ).all())
    return [
        {"id": r.id, "ts": r.ts.isoformat(), "kind": r.kind,
         "summary": r.summary, "requested_by": r.requested_by}
        for r in rows
    ]


async def request_command_approval(command: str) -> bool:
    """Back-compat shim for ``run_shell``: a ``shell``-kind approval."""
    return await request_approval("shell", command, agent="system")


def recent(limit: int = 50) -> list[dict]:
    """Recent approvals (newest first) for the admin UI / audit."""
    from sqlmodel import select

    from src.db.engine import get_session
    from src.db.models import Approval

    with get_session() as session:
        rows = list(session.exec(
            select(Approval).order_by(Approval.id.desc()).limit(max(1, min(limit, 500)))
        ).all())
    return [
        {"id": r.id, "ts": r.ts.isoformat(), "kind": r.kind, "summary": r.summary,
         "status": r.status, "requested_by": r.requested_by, "decided_by": r.decided_by,
         "reason": r.reason,
         "decided_at": r.decided_at.isoformat() if r.decided_at else None}
        for r in rows
    ]
