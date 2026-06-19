"""Offline coverage for team_graph runtime helpers (no LLM, no network calls).

Covers the in-flight run registry (status/additions), the compiled-graph builder,
and CEO-vs-specialist spec resolution.

    python tests/test_graph_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client  # noqa: F401  (ensures src on path / consistent env)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.graph import team_graph as tg
from src.registry import registry

TID = "_rt_thread"


def _run_registry() -> None:
    tg._run_start(TID, "build a landing page")
    info = tg._run_info[TID]
    assert info["task"] == "build a landing page" and info["additions"] == []

    tg._set_status(TID, "Подключаю: developer")
    assert tg._run_info[TID]["status"] == "Подключаю: developer"

    tg._record_addition(TID, "also add a contact form")
    tg._record_addition(TID, "use a dark theme")
    drained = tg._drain_additions(TID)
    assert drained == ["also add a contact form", "use a dark theme"]
    assert tg._drain_additions(TID) == []  # cleared after draining

    tg._run_end(TID)
    assert TID not in tg._run_info
    # status/addition on an unknown thread are safe no-ops
    tg._set_status("ghost", "x")
    tg._record_addition("ghost", "y")
    assert tg._drain_additions("ghost") == []


def _spec_resolution() -> None:
    registry.setup()
    # explicit per-agent provider + model -> used verbatim, independent of globals
    registry.create_agent({
        "slug": "_rt_ceo", "name": "Boss",
        "provider": "anthropic", "model": "claude-haiku-4-5",
    })
    try:
        prov, model, _key, _url = tg._resolve_spec("_rt_ceo", is_ceo=True)
        assert prov == "anthropic" and model == "claude-haiku-4-5"
        # an agent with no skills yields no skill tools
        assert tg._skill_tools_for("_rt_ceo") == []
    finally:
        registry.delete_agent("_rt_ceo")


def _graph_build() -> None:
    # building the compiled graph is offline (sqlite checkpointer on the local db)
    app = asyncio.run(tg._get_team_app())
    assert app is not None
    # cached on second call
    assert asyncio.run(tg._get_team_app()) is app


def main() -> None:
    _run_registry()
    _spec_resolution()
    _graph_build()
    print("graph runtime tests: OK")


if __name__ == "__main__":
    main()
