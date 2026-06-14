"""Skill contract + loader tests (v2 stage 5).

    python tests/test_skills.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent_fs import scaffold_all
from src.registry import registry
from src.skills import run_skill, skill_loader


def main() -> None:
    registry.setup()
    scaffold_all()
    found = skill_loader.discover()
    names = {(f["owner"], f["name"]) for f in found}
    assert ("developer", "json_format") in names, names

    # the skill is registered in the DB with an owner agent_skills link
    from sqlmodel import select

    from src.db.engine import get_session
    from src.db.models import AgentSkill, Skill

    with get_session() as s:
        row = s.exec(select(Skill).where(Skill.name == "json_format")).first()
        assert row and row.is_public and row.owner_agent_id == registry.get("developer").id
        link = s.exec(
            select(AgentSkill).where(AgentSkill.skill_id == row.id)
        ).first()
        assert link and link.adopted_from is None  # owner link

    # run it: valid + invalid
    ok = run_skill("developer", "json_format", text='{"b": 2, "a": 1}')
    assert ok.ok and '"a": 1' in ok.output and ok.data["keys"] == ["a", "b"]
    bad = run_skill("developer", "json_format", text="{not json}")
    assert not bad.ok and "invalid JSON" in bad.output

    # unknown skill
    missing = run_skill("developer", "nope")
    assert not missing.ok

    print("skills tests: OK")


if __name__ == "__main__":
    main()
