"""Skill contract + loader (v2 stage 5).

A *skill* is a reusable capability that lives in an agent's folder:

    agents/<slug>/skills/<name>/
      skill.yaml     # name, version, description, is_public, params
      impl.py        # a subclass of BaseSkill implementing run()

The contract is intentionally tiny and synchronous: ``run(ctx, **params) ->
SkillResult``. The :class:`SkillLoader` scans every agent folder, imports each
``impl.py``, registers/updates the skill in the ``skills`` table (and an owner
row in ``agent_skills``), and keeps the instantiated objects so they can be
executed later (see :func:`run_skill`). Adoption across agents is in
``src/skill_registry.py``.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml
from sqlmodel import select

from src.agent_fs import skills_dir
from src.db.engine import get_session
from src.db.models import AgentSkill, Skill
from src.registry import registry

logger = logging.getLogger(__name__)


@dataclass
class SkillContext:
    """What a skill is given when it runs."""

    agent_slug: str
    project_dir: str = ""
    workspace_root: str = ""


@dataclass
class SkillResult:
    """What a skill returns."""

    ok: bool = True
    output: str = ""
    data: dict = field(default_factory=dict)


class BaseSkill:
    """Base class every skill implements. Keep ``run`` pure and deterministic
    where possible; declare callable params in ``params`` ({name: description})."""

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    params: dict[str, str] = {}

    def run(self, ctx: SkillContext, **params) -> SkillResult:  # noqa: D401
        raise NotImplementedError


# --- DB registration --------------------------------------------------------

def _owner_id(slug: str) -> Optional[int]:
    agent = registry.get(slug)
    return agent.id if agent else None


def _register_skill(slug: str, meta: dict, path: str) -> Optional[int]:
    """Upsert a skill row for ``slug`` and ensure the owner agent_skills link."""
    owner_id = _owner_id(slug)
    if owner_id is None:
        return None
    name = meta["name"]
    with get_session() as session:
        row = session.exec(
            select(Skill).where(Skill.owner_agent_id == owner_id, Skill.name == name)
        ).first()
        from datetime import datetime

        if row:
            row.version = str(meta.get("version", row.version))
            row.description = meta.get("description", row.description) or ""
            row.manifest_json = json.dumps(meta, default=str, ensure_ascii=False)
            row.path = path
            row.is_public = bool(meta.get("is_public", row.is_public))
            row.updated_at = datetime.utcnow()
            session.add(row)
        else:
            row = Skill(
                name=name,
                owner_agent_id=owner_id,
                version=str(meta.get("version", "0.1.0")),
                description=meta.get("description", "") or "",
                manifest_json=json.dumps(meta, default=str, ensure_ascii=False),
                path=path,
                is_public=bool(meta.get("is_public", False)),
            )
            session.add(row)
        session.commit()
        session.refresh(row)
        skill_id = row.id
        link = session.exec(
            select(AgentSkill).where(
                AgentSkill.agent_id == owner_id, AgentSkill.skill_id == skill_id
            )
        ).first()
        if not link:
            session.add(
                AgentSkill(agent_id=owner_id, skill_id=skill_id, adopted_from=None, enabled=True)
            )
            session.commit()
    return skill_id


# --- loader -----------------------------------------------------------------

class SkillLoader:
    """Discovers skills in agent folders, registers them, and holds the live
    instances keyed by ``(owner_slug, skill_name)`` for execution."""

    def __init__(self) -> None:
        self._instances: dict[tuple[str, str], BaseSkill] = {}

    def _instantiate(self, slug: str, dirname: str, impl_path: str) -> BaseSkill:
        modname = f"agent_skill_{slug}_{dirname}"
        spec = importlib.util.spec_from_file_location(modname, impl_path)
        if not spec or not spec.loader:
            raise ImportError(f"cannot load {impl_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseSkill)
                and value is not BaseSkill
            ):
                return value()
        raise ImportError(f"no BaseSkill subclass in {impl_path}")

    def discover(self) -> list[dict]:
        """(Re)scan all agent folders, (re)register skills, refresh instances."""
        self._instances.clear()
        registry.reload()
        found: list[dict] = []
        for agent in registry.list_agents():
            base = skills_dir(agent.slug)
            if not os.path.isdir(base):
                continue
            for dirname in sorted(os.listdir(base)):
                skill_path = os.path.join(base, dirname)
                yaml_path = os.path.join(skill_path, "skill.yaml")
                impl_path = os.path.join(skill_path, "impl.py")
                if not (os.path.isfile(yaml_path) and os.path.isfile(impl_path)):
                    continue
                try:
                    meta = yaml.safe_load(open(yaml_path, encoding="utf-8")) or {}
                    inst = self._instantiate(agent.slug, dirname, impl_path)
                    meta.setdefault("name", inst.name or dirname)
                    meta.setdefault("version", inst.version)
                    meta.setdefault("description", inst.description)
                    meta.setdefault("params", inst.params)
                    skill_id = _register_skill(agent.slug, meta, skill_path)
                    self._instances[(agent.slug, meta["name"])] = inst
                    found.append({"owner": agent.slug, "skill_id": skill_id, **meta})
                except Exception:  # noqa: BLE001 - one bad skill must not block the rest
                    logger.exception("failed to load skill %s/%s", agent.slug, dirname)
        logger.info("SkillLoader: discovered %d skill(s)", len(found))
        return found

    def get_instance(self, owner_slug: str, name: str) -> Optional[BaseSkill]:
        return self._instances.get((owner_slug, name))


# Process-wide singleton.
skill_loader = SkillLoader()


def run_skill(owner_slug: str, name: str, *, project_dir: str = "", **params) -> SkillResult:
    """Execute a discovered skill by its owner + name. Re-discovers lazily if the
    instance isn't loaded yet."""
    from src.config import settings

    inst = skill_loader.get_instance(owner_slug, name)
    if inst is None:
        skill_loader.discover()
        inst = skill_loader.get_instance(owner_slug, name)
    if inst is None:
        return SkillResult(ok=False, output=f"[skill not found: {owner_slug}/{name}]")
    ctx = SkillContext(
        agent_slug=owner_slug,
        project_dir=project_dir,
        workspace_root=os.path.abspath(os.path.expanduser(settings.workspace_dir)),
    )
    try:
        return inst.run(ctx, **params)
    except Exception as exc:  # noqa: BLE001 - a skill bug must not crash the caller
        logger.exception("skill %s/%s failed", owner_slug, name)
        return SkillResult(ok=False, output=f"[skill error: {exc}]")
