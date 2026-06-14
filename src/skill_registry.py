"""Skill adoption + querying (v2 stage 5).

The :mod:`src.skills` loader discovers and registers skills (one owner each).
This module is the read/adopt layer on top:

* a catalog of public skills another agent can adopt,
* ``adopt_skill`` / ``drop_skill`` / ``set_enabled`` to manage an agent's
  ``agent_skills`` links (``adopted_from`` records the source agent),
* small semver helpers to compare versions.

Adoption never copies code: the adopter runs the OWNER's implementation, so an
adopted skill always tracks the owner's current version.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import AgentSkill, Skill
from src.registry import registry

logger = logging.getLogger(__name__)


# --- semver -----------------------------------------------------------------

def parse_semver(version: str) -> tuple[int, int, int]:
    """Lenient semver parse -> (major, minor, patch). Non-numeric parts -> 0."""
    parts = re.split(r"[.\-+]", str(version or "0"))
    nums = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])  # type: ignore[return-value]


def semver_newer(a: str, b: str) -> bool:
    """True if version ``a`` is strictly newer than ``b``."""
    return parse_semver(a) > parse_semver(b)


# --- id <-> slug ------------------------------------------------------------

def _agent_id(slug: str) -> Optional[int]:
    agent = registry.get(slug)
    return agent.id if agent else None


def _slug_by_id() -> dict[int, str]:
    return {a.id: a.slug for a in registry.list_agents() if a.id is not None}


def _skill_dict(row: Skill, by_id: dict[int, str]) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "owner": by_id.get(row.owner_agent_id, ""),
        "version": row.version,
        "description": row.description,
        "is_public": row.is_public,
        "params": (json.loads(row.manifest_json or "{}") or {}).get("params", {}),
    }


# --- queries ----------------------------------------------------------------

def list_skills() -> list[dict]:
    """Every registered skill (owner's view)."""
    by_id = _slug_by_id()
    with get_session() as session:
        rows = session.exec(select(Skill).order_by(Skill.name)).all()
        return [_skill_dict(r, by_id) for r in rows]


def public_catalog(*, exclude_owner: Optional[str] = None) -> list[dict]:
    """Public skills available to adopt, optionally hiding one agent's own."""
    exclude_id = _agent_id(exclude_owner) if exclude_owner else None
    by_id = _slug_by_id()
    with get_session() as session:
        rows = session.exec(select(Skill).where(Skill.is_public == True)).all()  # noqa: E712
    return [_skill_dict(r, by_id) for r in rows if r.owner_agent_id != exclude_id]


def agent_skills(slug: str) -> list[dict]:
    """Skills an agent has (owned + adopted), with link state for the UI."""
    agent_id = _agent_id(slug)
    if agent_id is None:
        return []
    by_id = _slug_by_id()
    out: list[dict] = []
    with get_session() as session:
        links = session.exec(
            select(AgentSkill).where(AgentSkill.agent_id == agent_id)
        ).all()
        for link in links:
            skill = session.get(Skill, link.skill_id)
            if not skill:
                continue
            d = _skill_dict(skill, by_id)
            d.update(
                {
                    "enabled": link.enabled,
                    "adopted_from": by_id.get(link.adopted_from) if link.adopted_from else None,
                    "owned": link.adopted_from is None,
                }
            )
            out.append(d)
    return out


def enabled_skills_for(slug: str) -> list[dict]:
    """Owned + adopted skills that are enabled (used to build the agent's tools)."""
    return [s for s in agent_skills(slug) if s.get("enabled")]


# --- mutations --------------------------------------------------------------

def adopt_skill(adopter_slug: str, skill_id: int) -> dict:
    """Give ``adopter_slug`` an adopted link to a public skill it doesn't own."""
    adopter_id = _agent_id(adopter_slug)
    if adopter_id is None:
        raise KeyError(adopter_slug)
    with get_session() as session:
        skill = session.get(Skill, skill_id)
        if not skill:
            raise KeyError(f"skill {skill_id}")
        if skill.owner_agent_id == adopter_id:
            raise ValueError("an agent already owns its own skill")
        if not skill.is_public:
            raise ValueError("skill is not public")
        existing = session.exec(
            select(AgentSkill).where(
                AgentSkill.agent_id == adopter_id, AgentSkill.skill_id == skill_id
            )
        ).first()
        if existing:
            existing.enabled = True
            session.add(existing)
            session.commit()
        else:
            session.add(
                AgentSkill(
                    agent_id=adopter_id,
                    skill_id=skill_id,
                    adopted_from=skill.owner_agent_id,
                    enabled=True,
                )
            )
            session.commit()
        owner_id = skill.owner_agent_id
    _audit(adopter_slug, "skill_adopt", f"skill:{skill_id}", owner_id=owner_id)
    return {"adopter": adopter_slug, "skill_id": skill_id}


def drop_skill(adopter_slug: str, skill_id: int) -> None:
    """Remove an ADOPTED link (owners can't drop their own skill this way)."""
    adopter_id = _agent_id(adopter_slug)
    if adopter_id is None:
        raise KeyError(adopter_slug)
    with get_session() as session:
        link = session.exec(
            select(AgentSkill).where(
                AgentSkill.agent_id == adopter_id, AgentSkill.skill_id == skill_id
            )
        ).first()
        if not link:
            raise KeyError("not adopted")
        if link.adopted_from is None:
            raise ValueError("cannot drop an owned skill")
        session.delete(link)
        session.commit()
    _audit(adopter_slug, "skill_drop", f"skill:{skill_id}")


def set_enabled(agent_slug: str, skill_id: int, enabled: bool) -> None:
    agent_id = _agent_id(agent_slug)
    if agent_id is None:
        raise KeyError(agent_slug)
    with get_session() as session:
        link = session.exec(
            select(AgentSkill).where(
                AgentSkill.agent_id == agent_id, AgentSkill.skill_id == skill_id
            )
        ).first()
        if not link:
            raise KeyError("skill not linked to this agent")
        link.enabled = bool(enabled)
        session.add(link)
        session.commit()


def set_public(owner_slug: str, skill_id: int, is_public: bool) -> None:
    """Publish/unpublish a skill (only its owner)."""
    owner_id = _agent_id(owner_slug)
    with get_session() as session:
        skill = session.get(Skill, skill_id)
        if not skill:
            raise KeyError(f"skill {skill_id}")
        if skill.owner_agent_id != owner_id:
            raise ValueError("only the owner can publish a skill")
        skill.is_public = bool(is_public)
        skill.updated_at = datetime.utcnow()
        session.add(skill)
        session.commit()
    _audit(owner_slug, "skill_publish", f"skill:{skill_id}", is_public=is_public)


def _audit(actor: str, action: str, target: str, **details: object) -> None:
    try:
        from src.db.models import AuditLog

        with get_session() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    target=target,
                    details_json=json.dumps(details, default=str),
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to audit %s", action)
