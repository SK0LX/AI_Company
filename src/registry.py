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

from sqlmodel import select

from src.agents.prompts import CEO_PROMPT, ROLE_LABELS, SPECIALIST_PROMPTS
from src.db.engine import get_session, init_db
from src.db.models import Agent, AgentObligation, AgentPermission

logger = logging.getLogger(__name__)

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
        self.reload()

    def seed_if_empty(self) -> None:
        with get_session() as session:
            if session.exec(select(Agent)).first() is not None:
                return  # already seeded
            roles = {"ceo": CEO_PROMPT, **SPECIALIST_PROMPTS}
            for slug, prompt in roles.items():
                agent = Agent(
                    slug=slug,
                    name=ROLE_LABELS.get(slug, slug),
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


# Process-wide singleton.
registry = Registry()
