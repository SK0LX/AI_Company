"""Edge-case tests for skill adoption/publish + semver error paths. No network.

    python tests/test_skill_registry_edges.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import skill_registry as sr
from src.agent_fs import scaffold_all
from src.registry import registry
from src.skills import skill_loader


def main() -> None:
    registry.setup()
    scaffold_all()
    skill_loader.discover()

    jf = next(s for s in sr.list_skills() if s["name"] == "json_format")
    sid = jf["id"]

    # semver edges
    assert sr.parse_semver("bad.version") == (0, 0, 0)
    assert sr.parse_semver("2") == (2, 0, 0)
    assert not sr.semver_newer("1.0.0", "1.0.0")
    assert sr.semver_newer("1.0.1", "1.0.0")

    # adopt errors
    try:
        sr.adopt_skill("developer", sid)  # owner can't adopt own
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        sr.adopt_skill("tester", 999999)  # unknown skill
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        sr.adopt_skill("nope_agent", sid)  # unknown adopter
        raise AssertionError("expected KeyError")
    except KeyError:
        pass

    # publish: only the owner can (un)publish
    try:
        sr.set_public("tester", sid, False)
        raise AssertionError("expected ValueError (non-owner publish)")
    except ValueError:
        pass
    sr.set_public("developer", sid, False)  # owner unpublishes
    assert not next(s for s in sr.list_skills() if s["id"] == sid)["is_public"]
    # now a private skill can't be adopted
    try:
        sr.adopt_skill("tester", sid)
        raise AssertionError("expected ValueError (not public)")
    except ValueError:
        pass
    sr.set_public("developer", sid, True)  # restore

    # drop errors
    try:
        sr.drop_skill("tester", sid)  # not adopted
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        sr.drop_skill("developer", sid)  # owned, not droppable
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    # set_enabled errors
    try:
        sr.set_enabled("nope_agent", sid, True)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        sr.set_enabled("tester", sid, True)  # not linked to tester
        raise AssertionError("expected KeyError")
    except KeyError:
        pass

    # enabled_skills_for returns only enabled ones for the owner
    assert any(s["name"] == "json_format" for s in sr.enabled_skills_for("developer"))

    print("skill registry edge tests: OK")


if __name__ == "__main__":
    main()
