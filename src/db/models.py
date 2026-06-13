"""SQLModel tables for the v2 agent platform.

Stage 1 introduces the *agent registry*: agents and their permissions /
obligations become DATA in the database instead of hardcoded values in
``prompts.py``. Later stages add tasks, events, messages and skills (see
``docs/SPEC_v2.md``). Tables are created with ``SQLModel.metadata.create_all``
for now; Alembic migrations come once schemas start evolving.
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
    model: str = ""  # empty -> provider default
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
