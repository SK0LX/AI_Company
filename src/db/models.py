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
