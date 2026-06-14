"""Skill adoption + semver tests (v2 stage 5).

    python tests/test_skill_adoption.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import skill_registry as sr
from src.agent_fs import scaffold_all
from src.registry import registry
from src.skills import skill_loader


def test_semver() -> None:
    assert sr.parse_semver("1.2.3") == (1, 2, 3)
    assert sr.parse_semver("2.0") == (2, 0, 0)
    assert sr.semver_newer("1.2.0", "1.1.9")
    assert not sr.semver_newer("1.0.0", "1.0.0")


def test_adoption() -> None:
    # the developer owns json_format; the tester adopts it.
    catalog = sr.public_catalog(exclude_owner="tester")
    jf = next(s for s in catalog if s["name"] == "json_format")
    assert jf["owner"] == "developer"

    sr.adopt_skill("tester", jf["id"])
    tester_skills = {s["name"]: s for s in sr.agent_skills("tester")}
    assert "json_format" in tester_skills
    adopted = tester_skills["json_format"]
    assert adopted["adopted_from"] == "developer" and not adopted["owned"]

    # owner still shows it as owned
    dev = {s["name"]: s for s in sr.agent_skills("developer")}["json_format"]
    assert dev["owned"] and dev["adopted_from"] is None

    # disable then drop the adopted link
    sr.set_enabled("tester", jf["id"], False)
    assert not next(s for s in sr.agent_skills("tester") if s["id"] == jf["id"])["enabled"]
    sr.drop_skill("tester", jf["id"])
    assert all(s["id"] != jf["id"] for s in sr.agent_skills("tester"))

    # cannot drop an owned skill
    try:
        sr.drop_skill("developer", jf["id"])
        raise AssertionError("expected ValueError dropping an owned skill")
    except ValueError:
        pass


def main() -> None:
    registry.setup()
    scaffold_all()
    skill_loader.discover()
    test_semver()
    test_adoption()
    print("skill adoption tests: OK")


if __name__ == "__main__":
    main()
