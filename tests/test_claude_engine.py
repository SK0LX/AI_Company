"""Unit tests for the Claude Code CLI engine wiring (the "bash console" engine).

No network and no real `claude` CLI: ``claude_bridge.run_claude`` is monkeypatched
with a fake, so we verify the SELECTION + ROUTING + step/cost plumbing only.

    python tests/test_claude_engine.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.claude_bridge as claude_bridge
from src.config import SUPPORTED_ENGINES, settings
from src.graph import team_graph as TG
from src.registry import registry

SLUG = "_ut_claude"

# Records what the fake run_claude was called with, so we can assert plumbing.
_calls: list[dict] = []


def _install_fake_run_claude(answer: str = "FAKE-ANSWER", *, ok: bool = True,
                             cost: float = 0.01):
    async def fake_run_claude(prompt, *, cwd, resume=None, agents=None, model=None,
                              permission_mode="acceptEdits", use_subscription=False,
                              on_step=None, can_use_tool=None, no_tools=False,
                              timeout=1200.0):
        _calls.append({
            "prompt": prompt, "cwd": cwd, "agents": agents, "model": model,
            "permission_mode": permission_mode, "use_subscription": use_subscription,
            "timeout": timeout,
        })
        # Exercise the step adapters: one tool step + the final result.
        if on_step:
            await on_step("tool", "🔧 write_file backend/main.py")
            await on_step("result", answer)
        return {"ok": ok, "answer": answer, "session_id": "sess-1",
                "cost_usd": cost, "error": "" if ok else "boom"}

    claude_bridge.run_claude = fake_run_claude


def _config_defaults() -> None:
    # The shipped default is the classic LLM engine, and the resolver is robust to
    # junk values (an operator typo must never silently disable execution).
    assert settings.team_engine_resolved in SUPPORTED_ENGINES
    assert "llm" in SUPPORTED_ENGINES and "claude_cli" in SUPPORTED_ENGINES
    saved = settings.team_engine
    try:
        settings.team_engine = "claude_cli"
        assert settings.team_engine_resolved == "claude_cli"
        settings.team_engine = "nonsense"
        assert settings.team_engine_resolved == "llm"  # invalid -> safe default
    finally:
        settings.team_engine = saved


def _registry_persists_engine() -> None:
    registry.create_agent({
        "slug": SLUG, "name": "Claude agent", "role": "qa",
        "system_prompt": "p", "engine": "claude_cli",
        "permissions": {"can_edit_files": "true"},
    })
    assert registry.engine_for(SLUG) == "claude_cli"
    assert registry.as_dict(SLUG)["engine"] == "claude_cli"

    # Clearing the engine back to "" reverts the agent to the global default.
    registry.update_agent(SLUG, {"engine": ""})
    assert registry.engine_for(SLUG) == ""
    assert registry.as_dict(SLUG)["engine"] == ""
    # Unknown slug is a safe empty, never a crash.
    assert registry.engine_for("nope_zzz") == ""


def _engine_resolution() -> None:
    saved = settings.team_engine
    try:
        # Global default flows to any agent without its own choice (incl. "ceo").
        settings.team_engine = "llm"
        assert TG._engine_for("ceo") == "llm"
        assert TG._engine_for(SLUG) == "llm"
        settings.team_engine = "claude_cli"
        assert TG._engine_for("ceo") == "claude_cli"
        # A per-agent choice overrides the global default in BOTH directions.
        registry.update_agent(SLUG, {"engine": "llm"})
        assert TG._engine_for(SLUG) == "llm"  # agent says llm, global says claude
        registry.update_agent(SLUG, {"engine": "claude_cli"})
        settings.team_engine = "llm"
        assert TG._engine_for(SLUG) == "claude_cli"  # agent says claude, global llm
    finally:
        settings.team_engine = saved


def _specialist_routes_to_claude() -> None:
    """arun_specialist must divert to the bridge when the agent's engine is
    claude_cli, and ground the report with the on-disk file list."""
    _calls.clear()
    _install_fake_run_claude(answer="DEV-DONE")
    registry.update_agent(SLUG, {"engine": "claude_cli",
                                 "permissions": {"can_edit_files": "true"}})
    reply = asyncio.run(TG.arun_specialist(SLUG, "build a thing", project="utproj"))
    assert "DEV-DONE" in reply, reply
    # Tool role -> the CEO ground-truth file block is appended.
    assert "[Files actually on disk in the project folder now]" in reply
    assert len(_calls) == 1
    call = _calls[0]
    # cwd is the sandboxed project folder; the bridge gets the claude knobs.
    assert call["cwd"].replace("\\", "/").endswith("/utproj")
    assert call["permission_mode"] == settings.claude_permission_mode


def _team_routes_to_claude() -> None:
    """arun_team must hand the whole request to the bridge (no LangGraph) when the
    team engine is claude_cli, stream steps to on_event, and return the answer."""
    _calls.clear()
    _install_fake_run_claude(answer="ГОТОВО: собрал проект")
    saved_engine, saved_tr = settings.team_engine, settings.translate_chatter
    events: list[tuple] = []

    async def on_event(kind: str, text: str) -> None:
        events.append((kind, text))

    try:
        settings.team_engine = "claude_cli"
        settings.translate_chatter = False  # skip the RU-ensure LLM call
        answer, awaiting, did_work = asyncio.run(
            TG.arun_team("сделай лендинг", thread_id="ut-thread", on_event=on_event)
        )
    finally:
        settings.team_engine, settings.translate_chatter = saved_engine, saved_tr

    assert answer == "ГОТОВО: собрал проект", answer
    assert awaiting is None and did_work is True
    assert len(_calls) == 1
    # The team roster was passed to claude as --agents (built from the registry).
    assert _calls[0]["agents"] is None or isinstance(_calls[0]["agents"], dict)
    # The intermediate tool step reached the live ticker as a "delegate" line.
    assert any(k == "delegate" for k, _t in events), events
    # The final result is NOT double-sent as its own message (it's the return).
    assert all(k != "result" for k, _t in events), events


def _team_engine_error_is_graceful() -> None:
    """A bridge failure returns a friendly message, not an exception."""
    _calls.clear()
    _install_fake_run_claude(answer="", ok=False)
    saved_engine, saved_tr = settings.team_engine, settings.translate_chatter
    try:
        settings.team_engine = "claude_cli"
        settings.translate_chatter = False
        answer, awaiting, did_work = asyncio.run(
            TG.arun_team("hi", thread_id="ut-thread-2", on_event=None)
        )
    finally:
        settings.team_engine, settings.translate_chatter = saved_engine, saved_tr
    assert did_work is False and awaiting is None
    assert "claude" in answer.lower()


def main() -> None:
    registry.setup()
    # Sandbox the workspace so _project_path never touches the configured default.
    settings.workspace_dir = tempfile.mkdtemp(prefix="ut-claude-")
    _real_run_claude = claude_bridge.run_claude
    try:
        _config_defaults()
        _registry_persists_engine()
        _engine_resolution()
        _specialist_routes_to_claude()
        _team_routes_to_claude()
        _team_engine_error_is_graceful()
    finally:
        claude_bridge.run_claude = _real_run_claude
        try:
            registry.delete_agent(SLUG)
        except KeyError:
            pass

    print("claude engine tests: OK")


if __name__ == "__main__":
    main()
