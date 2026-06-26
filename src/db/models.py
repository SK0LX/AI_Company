"""SQLModel tables for the v2 agent platform.

Stage 1 introduced the *agent registry*: agents and their permissions /
obligations as DATA in the database instead of hardcoded values in ``prompts.py``.

Stage 4 adds the collaboration substrate: ``Task`` + ``TaskEvent`` (work and its
audit trail), ``Message`` (the persisted log of the in-process MessageBus),
``Delegation`` / ``HelpRequest`` (the consent-based hand-offs), and ``AuditLog``
(security-relevant actions). See ``docs/SPEC_v2.md`` §3. Tables are created with
``SQLModel.metadata.create_all``; Alembic migrations come once schemas evolve.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Agent(SQLModel, table=True):
    """One team member. The system prompt and model make it an LLM persona; the
    telegram fields (filled in a later stage) give it its own bot."""

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)  # stable key, e.g. "developer"
    name: str  # human label, e.g. "Backend-разработчик"
    role: str  # canonical role family (often == slug for the seed set)
    system_prompt: str
    # LLM connection (per-agent). Empty provider -> the global LLM_PROVIDER.
    # provider ∈ openrouter | anthropic | google | openai_compatible
    provider: str = ""
    model: str = ""  # empty -> provider default
    api_key: str = ""  # encrypted; empty -> the global key for the provider
    base_url: str = ""  # for openai_compatible (any OpenAI-compatible endpoint)
    telegram_token: str = ""  # encrypted in a later stage; empty for now
    telegram_username: str = ""
    folder_path: str = ""  # agents/<slug>/ — its own codebase (later stage)
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentPermission(SQLModel, table=True):
    """A capability grant. ``value`` is a scalar or JSON string (e.g. delegate_to
    is a JSON list). Kept as key/value rows so new permissions need no migration."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True, foreign_key="agent.id")
    key: str
    value: str = "true"


class AgentObligation(SQLModel, table=True):
    """What the agent is responsible for (free-text responsibilities / SLAs)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True, foreign_key="agent.id")
    key: str
    description: str = ""


# --- stage 4: collaboration substrate ---------------------------------------

# Canonical string statuses (kept as plain str columns — no enum migrations).
TASK_STATUSES = ("new", "in_progress", "blocked", "review", "done", "cancelled")
DELEGATION_STATUSES = ("pending", "accepted", "declined", "revoked")
HELP_STATUSES = ("open", "assigned", "resolved", "cancelled")


class Task(SQLModel, table=True):
    """A unit of work. Owned by one agent, optionally a subtask of another task.
    The status drives the (future) admin kanban; the full history lives in
    :class:`TaskEvent`."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: str = ""
    status: str = "new"  # one of TASK_STATUSES
    owner_agent_id: Optional[int] = Field(default=None, index=True, foreign_key="agent.id")
    parent_task_id: Optional[int] = Field(default=None, index=True, foreign_key="task.id")
    created_by: Optional[int] = Field(default=None, foreign_key="agent.id")  # agent or null=user
    priority: str = "обычный"  # обычный | высокий | низкий (free-text)
    complexity: int = 1  # 1..5, a rough effort estimate
    # Atomic claim (v3 pull-coordination): an agent "checks out" a task via a
    # compare-and-set so two agents can never grab the same one (see src/locks.py).
    claimed_by: str = ""  # agent slug that holds the task, "" = free
    claimed_at: Optional[datetime] = Field(default=None)
    lock_token: str = ""  # opaque token of the current claim
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TaskEvent(SQLModel, table=True):
    """An append-only entry in a task's timeline (created → delegated → helped →
    done). ``payload_json`` carries type-specific detail for the UI/audit."""

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(index=True, foreign_key="task.id")
    ts: datetime = Field(default_factory=datetime.utcnow)
    actor_agent_id: Optional[int] = Field(default=None, foreign_key="agent.id")
    type: str  # e.g. created|delegated|accepted|declined|help_requested|help_resolved|status|done
    payload_json: str = "{}"


class Message(SQLModel, table=True):
    """Persisted log of MessageBus traffic. A message is a direct one (``to_agent_id``
    set), a team-chat post (``chat_id`` set), or both. ``meta_json`` holds extras
    (refs, scope, etc.)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    from_agent_id: Optional[int] = Field(default=None, index=True, foreign_key="agent.id")
    to_agent_id: Optional[int] = Field(default=None, index=True, foreign_key="agent.id")
    chat_id: Optional[int] = Field(default=None)  # telegram chat for team posts
    kind: str  # DELEGATE|ACCEPT|DECLINE|HELP_REQUEST|HELP_RESULT|STATUS|CHAT
    text: str = ""
    meta_json: str = "{}"
    # Outbox flag: a CHAT message an agent wants delivered to Telegram. The gateway's
    # OutboxService drains unsent rows and sends them via the from-agent's own bot.
    sent: bool = Field(default=False, index=True)


class Delegation(SQLModel, table=True):
    """A hand-off A→B of a task (or a permission grant), accepted/declined by B.
    Permission grants may carry a TTL/expiry in ``meta`` of the related event."""

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: Optional[int] = Field(default=None, index=True, foreign_key="task.id")
    from_agent_id: int = Field(foreign_key="agent.id")
    to_agent_id: int = Field(index=True, foreign_key="agent.id")
    kind: str = "task"  # task|permission
    status: str = "pending"  # one of DELEGATION_STATUSES
    reason: str = ""
    ts: datetime = Field(default_factory=datetime.utcnow)


class HelpRequest(SQLModel, table=True):
    """A request for help on a task; a helper is assigned and (later) resolves it."""

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: Optional[int] = Field(default=None, index=True, foreign_key="task.id")
    requester_id: int = Field(foreign_key="agent.id")
    helper_id: Optional[int] = Field(default=None, foreign_key="agent.id")
    status: str = "open"  # one of HELP_STATUSES
    summary: str = ""
    ts: datetime = Field(default_factory=datetime.utcnow)


class AuditLog(SQLModel, table=True):
    """Security-relevant actions (permission checks, grants, shell runs, edits of
    others' code). ``actor`` is an agent slug or 'system'/'user'."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    actor: str = ""
    action: str = ""
    target: str = ""
    details_json: str = "{}"


# --- stage 5: skills + adoption ---------------------------------------------

class Skill(SQLModel, table=True):
    """A reusable capability owned by one agent, discovered from its folder
    (``agents/<slug>/skills/<name>/``). ``manifest_json`` is the parsed skill.yaml;
    ``path`` is the skill directory. Public skills can be adopted by other agents."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # skill key, unique per owner
    owner_agent_id: Optional[int] = Field(default=None, index=True, foreign_key="agent.id")
    version: str = "0.1.0"  # semver
    description: str = ""
    manifest_json: str = "{}"
    path: str = ""  # filesystem path to the skill directory
    is_public: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentSkill(SQLModel, table=True):
    """Which skills an agent has. The owner gets a row automatically; other agents
    get one when they ADOPT a public skill (``adopted_from`` = the source agent)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True, foreign_key="agent.id")
    skill_id: int = Field(index=True, foreign_key="skill.id")
    adopted_from: Optional[int] = Field(default=None, foreign_key="agent.id")  # null = owner
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --- control plane (2026-06-19 upgrade) -------------------------------------

class CostEvent(SQLModel, table=True):
    """One model call's token usage and computed cost. Append-only ledger that
    powers cost reporting and budget hard-stops. ``agent`` is a slug or 'system'."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    agent: str = Field(default="system", index=True)
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    task_id: Optional[int] = Field(default=None, index=True)


# Budget scopes/windows kept as plain strings (no enum migrations).
BUDGET_WINDOWS = ("day", "month", "lifetime")


class BudgetPolicy(SQLModel, table=True):
    """A spend cap. ``scope`` is 'global' or an agent slug; ``window`` is one of
    BUDGET_WINDOWS. When ``hard_stop`` is on and spend reaches the limit, the
    scope is blocked until the window resets (or the policy is raised)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = Field(default="global", index=True)  # 'global' | <agent-slug>
    limit_usd: float = 1.0
    window: str = "day"  # one of BUDGET_WINDOWS
    warn_percent: int = 80
    hard_stop: bool = True
    enabled: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Routine schedule kinds (computed by hand — no cron dependency).
ROUTINE_KINDS = ("interval", "daily", "weekly")


class Routine(SQLModel, table=True):
    """A recurring job: on schedule it creates a task and wakes the team or one
    agent with ``prompt``. ``schedule_value`` meaning depends on ``schedule_kind``:
    interval=seconds, daily='HH:MM' (UTC), weekly='DOW HH:MM' (0=Mon)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    schedule_kind: str = "interval"  # one of ROUTINE_KINDS
    schedule_value: str = "3600"
    prompt: str = ""
    target: str = "team"  # 'team' | <agent-slug>
    enabled: bool = True
    catch_up: bool = False  # run missed slots once (True) or skip to next (False)
    last_run_at: Optional[datetime] = Field(default=None)
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# Approval kinds + statuses.
APPROVAL_KINDS = ("shell", "self_modify", "budget_override", "risky_delete")
APPROVAL_STATUSES = ("pending", "approved", "denied")


class Approval(SQLModel, table=True):
    """A human-in-the-loop decision request. Generalizes the old per-command shell
    approval into typed, audited approvals."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    kind: str = "shell"  # one of APPROVAL_KINDS
    summary: str = ""
    status: str = "pending"  # one of APPROVAL_STATUSES
    requested_by: str = ""  # agent slug or 'system'
    decided_by: str = ""  # 'user' once decided
    reason: str = ""
    decided_at: Optional[datetime] = Field(default=None)


class ResourceLock(SQLModel, table=True):
    """An advisory lock over an arbitrary resource key (e.g. ``repo:proj``,
    ``file:src/app.py``, ``area:frontend``) so parallel agents don't step on each
    other. Acquired/released atomically (compare-and-set) in :mod:`src.locks`.
    ``expires_at`` (TTL) frees a lock whose holder died without releasing."""

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    agent: str = ""  # slug of the holder
    token: str = ""  # opaque token of this acquisition
    acquired_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = Field(default=None, index=True)


class WorkerHeartbeat(SQLModel, table=True):
    """Liveness beat from a per-agent worker process/container (v3 Ф3). The gateway
    and dashboard read this to show which agents have a live worker. One row per
    agent slug (upserted)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent: str = Field(index=True, unique=True)
    host: str = ""  # container/host name
    pid: int = 0
    last_seen: datetime = Field(default_factory=datetime.utcnow)
