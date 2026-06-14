"""Agent folder standard (v2 stage 5).

Each agent gets a folder ``agents/<slug>/`` with a standard layout:

    agents/<slug>/
      manifest.yaml      # materialized view of the agent's registry row
      prompt.md          # the agent's system prompt
      skills/            # the agent's skills: <name>/{skill.yaml, impl.py}

The folder is the agent's "code": skills are discovered here (see
``src/skills.py``). ``manifest.yaml`` / ``prompt.md`` are generated FROM the
database — the registry / admin panel remains the single source of truth, so
these two files are regenerated on boot and are gitignored. The ``skills/``
subtree is real code and IS tracked.
"""
from __future__ import annotations

import logging
import os

import yaml

from src.registry import registry

logger = logging.getLogger(__name__)

# agents/ lives at the project root (one level above src/).
AGENTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "agents"))


def agent_dir(slug: str) -> str:
    return os.path.join(AGENTS_ROOT, slug)


def skills_dir(slug: str) -> str:
    return os.path.join(agent_dir(slug), "skills")


def scaffold_agent(slug: str) -> str:
    """Create/refresh ``agents/<slug>/`` from the registry. Returns the folder."""
    info = registry.as_dict(slug)
    if not info:
        raise KeyError(slug)
    root = agent_dir(slug)
    os.makedirs(skills_dir(slug), exist_ok=True)

    manifest = {
        "slug": info["slug"],
        "name": info["name"],
        "role": info["role"],
        "model": info["model"] or "(provider default)",
        "enabled": info["enabled"],
        "permissions": info["permissions"],
        "obligation": info["obligation"],
    }
    with open(os.path.join(root, "manifest.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, allow_unicode=True, sort_keys=False)
    with open(os.path.join(root, "prompt.md"), "w", encoding="utf-8") as fh:
        fh.write((info["system_prompt"] or "").rstrip() + "\n")

    # Keep the (possibly empty) skills dir under version control.
    keep = os.path.join(skills_dir(slug), ".gitkeep")
    if not os.path.exists(keep):
        open(keep, "a").close()
    return root


def scaffold_all() -> list[str]:
    """Refresh every registered agent's folder. Idempotent; safe to call on boot."""
    registry.reload()
    made: list[str] = []
    for agent in registry.list_agents():
        try:
            made.append(scaffold_agent(agent.slug))
        except Exception:  # noqa: BLE001 - one bad agent must not block the rest
            logger.exception("failed to scaffold agent folder for %s", agent.slug)
    return made
