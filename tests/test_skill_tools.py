"""Skills-as-tools wiring test (v2 stage 5d). No LLM.

    python tests/test_skill_tools.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import skill_registry as sr
from src.agent_fs import scaffold_all
from src.graph.team_graph import _skill_tools_for
from src.registry import registry
from src.skills import skill_loader


def main() -> None:
    registry.setup()
    scaffold_all()
    skill_loader.discover()

    # The owner (developer) gets a tool for its skill, and it runs.
    tools = {t.name: t for t in _skill_tools_for("developer")}
    assert "skill_json_format" in tools, list(tools)
    out = tools["skill_json_format"].invoke({"text": '{"b":2,"a":1}'})
    assert '"a": 1' in out

    # After adoption, the adopter gets the same tool (running the owner's impl).
    catalog = sr.public_catalog(exclude_owner="tester")
    jf = next(s for s in catalog if s["name"] == "json_format")
    sr.adopt_skill("tester", jf["id"])
    try:
        tester_tools = {t.name: t for t in _skill_tools_for("tester")}
        assert "skill_json_format" in tester_tools
        out2 = tester_tools["skill_json_format"].invoke({"text": '{"x":1}'})
        assert '"x": 1' in out2
    finally:
        sr.drop_skill("tester", jf["id"])  # leave state clean

    print("skill tools tests: OK")


if __name__ == "__main__":
    main()
