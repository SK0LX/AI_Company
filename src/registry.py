"""Agent registry — the single source of truth for who the agents are.

Stage 1: agents move from hardcoded ``prompts.py`` into the database. On first
run the current 9 roles (CEO + 8 specialists) are seeded from ``prompts.py`` with
sensible default permissions/obligations. Agents are cached in memory and
refreshed on demand, so the async bot path never blocks on the DB.

This module is ADDITIVE — it does not change the running bot yet. Later stages
(admin CRUD, multi-bot runtime) build on top of it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlmodel import select

from src.agents.prompts import (
    CEO_PROMPT,
    ROLE_LABELS,
    ROLE_LABELS_RU,
    SPECIALIST_PROMPTS,
)
from src.db.engine import get_session, init_db
from src.db.models import Agent, AgentObligation, AgentPermission

logger = logging.getLogger(__name__)

CEO_SLUG = "ceo"

# Default permission grants per role for the seed set. Values are scalars or
# JSON strings (delegate_to is a JSON list / "*").
_SHELL_ROLES = {"developer", "frontend", "tester", "backend_reviewer", "frontend_reviewer"}
_FILE_ROLES = {"developer", "frontend", "tester", "backend_reviewer", "frontend_reviewer", "reviewer"}

# One-line responsibility per role (seed obligations).
_OBLIGATIONS = {
    "ceo": "Единая точка общения с пользователем; план, делегирование, синтез ответа.",
    "business_analyst": "Цели, пользователи, user stories, критерии приёмки, объём.",
    "system_analyst": "Архитектура, требования, модель данных, API-контракты, стек.",
    "developer": "Серверный код, API, БД; сборка и проверка backend.",
    "frontend": "UI, компоненты, интеграция с API; сборка фронта.",
    "tester": "Стратегия и кейсы тестов; реальный прогон тестов.",
    "designer": "UX-потоки, вайрфреймы, дизайн-система, доступность.",
    "backend_reviewer": "Ревью backend-кода: баги, безопасность, логика.",
    "frontend_reviewer": "Ревью frontend-кода: state/props, доступность, баги рендера.",
    "reviewer": "Тех-лид: сверка структуры проекта, перенос/чистка файлов.",
}


def _default_permissions(slug: str) -> list[tuple[str, str]]:
    perms: list[tuple[str, str]] = []
    if slug in _FILE_ROLES:
        perms.append(("can_edit_files", "true"))
    if slug in _SHELL_ROLES:
        perms.append(("can_run_shell", "true"))
    if slug == "reviewer":
        perms.append(("can_edit_others_code", "true"))
    if slug == "ceo":
        perms.append(("delegate_to", json.dumps("*")))
        perms.append(("can_modify_agents", "true"))
    return perms


class Registry:
    """In-memory cache of agents backed by the database."""

    def __init__(self) -> None:
        self._by_slug: dict[str, Agent] = {}

    def setup(self) -> None:
        """Create tables, seed defaults on first run, load the cache."""
        init_db()
        self.seed_if_empty()
        self._ensure_ru_names()
        self.reload()

    def _ensure_ru_names(self) -> None:
        """One-time normalization: give built-in roles their Russian display
        names if a row was seeded earlier with the English label."""
        with get_session() as session:
            changed = False
            for slug, ru in ROLE_LABELS_RU.items():
                agent = session.exec(select(Agent).where(Agent.slug == slug)).first()
                if agent and agent.name == ROLE_LABELS.get(slug):
                    agent.name = ru
                    session.add(agent)
                    changed = True
            if changed:
                session.commit()

    def seed_if_empty(self) -> None:
        with get_session() as session:
            if session.exec(select(Agent)).first() is not None:
                return  # already seeded
            roles = {"ceo": CEO_PROMPT, **SPECIALIST_PROMPTS}
            for slug, prompt in roles.items():
                agent = Agent(
                    slug=slug,
                    name=ROLE_LABELS_RU.get(slug) or ROLE_LABELS.get(slug, slug),
                    role=slug,
                    system_prompt=prompt,
                    folder_path=f"agents/{slug}",
                )
                session.add(agent)
                session.commit()
                session.refresh(agent)
                for key, value in _default_permissions(slug):
                    session.add(AgentPermission(agent_id=agent.id, key=key, value=value))
                session.add(
                    AgentObligation(
                        agent_id=agent.id,
                        key="primary",
                        description=_OBLIGATIONS.get(slug, ""),
                    )
                )
            session.commit()
            logger.info("Registry seeded %d agents from prompts.py", len(roles))

    def reload(self) -> None:
        with get_session() as session:
            agents = session.exec(select(Agent)).all()
            for a in agents:
                session.expunge(a)  # detach so scalar fields are usable after close
            self._by_slug = {a.slug: a for a in agents}

    # --- read API (used by the bot/admin later) -----------------------------

    def list_agents(self, *, enabled_only: bool = False) -> list[Agent]:
        agents = list(self._by_slug.values())
        return [a for a in agents if a.enabled] if enabled_only else agents

    def get(self, slug: str) -> Agent | None:
        return self._by_slug.get(slug)

    def permissions(self, slug: str) -> dict[str, str]:
        agent = self._by_slug.get(slug)
        if not agent:
            return {}
        with get_session() as session:
            rows = session.exec(
                select(AgentPermission).where(AgentPermission.agent_id == agent.id)
            ).all()
            return {r.key: r.value for r in rows}

    def obligation(self, slug: str) -> str:
        agent = self._by_slug.get(slug)
        if not agent:
            return ""
        with get_session() as session:
            row = session.exec(
                select(AgentObligation)
                .where(AgentObligation.agent_id == agent.id)
                .where(AgentObligation.key == "primary")
            ).first()
            return row.description if row else ""

    # --- roster accessors (the bot/graph read these instead of prompts.py) ---

    def prompt(self, slug: str) -> str:
        agent = self._by_slug.get(slug)
        return agent.system_prompt if agent else ""

    def ceo_prompt(self) -> str:
        return self.prompt(CEO_SLUG)

    def label(self, slug: str) -> str:
        agent = self._by_slug.get(slug)
        return agent.name if agent else slug

    def model_for(self, slug: str) -> str:
        agent = self._by_slug.get(slug)
        return (agent.model or "") if agent else ""

    def specialist_slugs(self, *, enabled_only: bool = True) -> list[str]:
        return [
            a.slug
            for a in self.list_agents(enabled_only=enabled_only)
            if a.slug != CEO_SLUG
        ]

    def is_specialist(self, slug: str | None) -> bool:
        if not slug or slug == CEO_SLUG:
            return False
        agent = self._by_slug.get(slug)
        return bool(agent) and agent.enabled

    def roster_block(self) -> str:
        """Human-readable list of delegatable specialists for the CEO prompt."""
        lines = [
            f"- {a.slug} — {a.name}"
            for a in self.list_agents(enabled_only=True)
            if a.slug != CEO_SLUG
        ]
        return "Specialists you can delegate to (use the exact key on the left):\n" + "\n".join(lines)

    def as_dict(self, slug: str) -> dict | None:
        """Full view of an agent for the admin API."""
        agent = self._by_slug.get(slug)
        if not agent:
            return None
        return {
            "slug": agent.slug,
            "name": agent.name,
            "role": agent.role,
            "system_prompt": agent.system_prompt,
            "model": agent.model,
            "telegram_username": agent.telegram_username,
            "enabled": agent.enabled,
            "permissions": self.permissions(slug),
            "obligation": self.obligation(slug),
        }

    # --- write API (used by the admin panel) --------------------------------

    def _set_permissions(self, session, agent_id: int, perms: dict[str, str]) -> None:
        existing = session.exec(
            select(AgentPermission).where(AgentPermission.agent_id == agent_id)
        ).all()
        for row in existing:
            session.delete(row)
        for key, value in (perms or {}).items():
            session.add(AgentPermission(agent_id=agent_id, key=key, value=str(value)))

    def _set_obligation(self, session, agent_id: int, text: str) -> None:
        row = session.exec(
            select(AgentObligation)
            .where(AgentObligation.agent_id == agent_id)
            .where(AgentObligation.key == "primary")
        ).first()
        if row:
            row.description = text or ""
            session.add(row)
        else:
            session.add(
                AgentObligation(agent_id=agent_id, key="primary", description=text or "")
            )

    def create_agent(self, data: dict) -> Agent:
        with get_session() as session:
            if session.exec(select(Agent).where(Agent.slug == data["slug"])).first():
                raise ValueError(f"agent '{data['slug']}' already exists")
            agent = Agent(
                slug=data["slug"],
                name=data.get("name") or data["slug"],
                role=data.get("role") or data["slug"],
                system_prompt=data.get("system_prompt", ""),
                model=data.get("model", ""),
                telegram_token=data.get("telegram_token", ""),
                telegram_username=data.get("telegram_username", ""),
                folder_path=data.get("folder_path") or f"agents/{data['slug']}",
                enabled=data.get("enabled", True),
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            self._set_permissions(session, agent.id, data.get("permissions", {}))
            self._set_obligation(session, agent.id, data.get("obligation", ""))
            session.commit()
        self.reload()
        return self._by_slug[data["slug"]]

    def update_agent(self, slug: str, data: dict) -> Agent:
        with get_session() as session:
            agent = session.exec(select(Agent).where(Agent.slug == slug)).first()
            if not agent:
                raise KeyError(slug)
            for field in ("name", "role", "system_prompt", "model",
                          "telegram_token", "telegram_username", "enabled"):
                if field in data and data[field] is not None:
                    setattr(agent, field, data[field])
            agent.updated_at = datetime.utcnow()
            session.add(agent)
            session.commit()
            if "permissions" in data:
                self._set_permissions(session, agent.id, data["permissions"])
            if "obligation" in data:
                self._set_obligation(session, agent.id, data["obligation"])
            session.commit()
        self.reload()
        return self._by_slug[slug]

    def delete_agent(self, slug: str) -> None:
        with get_session() as session:
            agent = session.exec(select(Agent).where(Agent.slug == slug)).first()
            if not agent:
                raise KeyError(slug)
            for row in session.exec(
                select(AgentPermission).where(AgentPermission.agent_id == agent.id)
            ).all():
                session.delete(row)
            for row in session.exec(
                select(AgentObligation).where(AgentObligation.agent_id == agent.id)
            ).all():
                session.delete(row)
            session.delete(agent)
            session.commit()
        self.reload()


# Process-wide singleton.
registry = Registry()
