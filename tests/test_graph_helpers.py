"""Unit tests for pure helpers in team_graph + crypto internals + config. No LLM.

    python tests/test_graph_helpers.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage

from src import crypto
from src.config import settings
from src.graph import team_graph as tg
from src.registry import registry


def _content_and_text() -> None:
    assert tg._content_to_text("hi") == "hi"
    blocks = [{"type": "text", "text": "a"}, {"type": "tool_use"}, {"type": "text", "text": "b"}]
    assert tg._content_to_text(blocks) == "a\nb"
    assert tg._content_to_text(123) == "123"

    msgs = [HumanMessage(content="first"), AIMessage(content="reply"), HumanMessage(content="last")]
    assert tg._last_user_text(msgs) == "last"
    assert tg._last_user_text([AIMessage(content="x")]) == ""
    assert tg._findings_block(["## A\n1", "## B\n2"]) == "## A\n1\n\n## B\n2"


def _routing_and_cyrillic() -> None:
    assert tg._route({"next_role": None}) == "__end__" or tg._route({"next_role": None}) == tg.END
    assert tg._route({"next_role": "developer", "steps": 0}) == "specialist"
    assert tg._route({"next_role": "developer", "steps": tg.MAX_STEPS}) == "finalize"

    assert tg._has_cyrillic("Привет") and not tg._has_cyrillic("hello")
    # text already Russian is returned unchanged (no model call)
    assert asyncio.run(tg.aensure_russian("Привет, мир")) == "Привет, мир"
    assert asyncio.run(tg.aensure_russian("")) == ""


def _wiki_context_and_models() -> None:
    with tempfile.TemporaryDirectory() as wiki:
        settings.wiki_dir = wiki
        settings.enable_wiki = True
        # empty vault -> no context
        assert tg._wiki_context("anything") == ""
        from src.agents.tools import wiki_write_note

        wiki_write_note("projects/x", "# X\nA CRM built with Django.")
        ctx = tg._wiki_context("CRM Django")
        assert "Team wiki" in ctx and "projects/x" in ctx
        # disabled -> empty regardless
        settings.enable_wiki = False
        assert tg._wiki_context("CRM") == ""
        settings.enable_wiki = True

    # google model construction (no network at build time)
    m = tg._make_model("google", "gemini-2.5-flash-lite", "key", "")
    assert type(m).__name__ == "ChatGoogleGenerativeAI"
    # unknown provider raises
    try:
        tg._make_model("bogus", "m", "k", "")
        raise AssertionError("expected ValueError for unknown provider")
    except ValueError:
        pass


def _mark_done_and_crypto() -> None:
    registry.setup()
    tg._mark_task_done({"task_id": None})  # no task -> no-op, no error
    # crypto internals
    valid = crypto.Fernet.generate_key()
    assert crypto._normalize(valid) == valid  # already a valid key -> unchanged
    norm = crypto._normalize(b"any-passphrase")
    assert len(norm) == 44 and crypto._normalize(b"any-passphrase") == norm  # deterministic
    prev = settings.app_secret
    settings.app_secret = "my-app-secret"
    try:
        assert len(crypto._load_key()) == 44  # derived from app_secret
    finally:
        settings.app_secret = prev


def main() -> None:
    _content_and_text()
    _routing_and_cyrillic()
    _wiki_context_and_models()
    _mark_done_and_crypto()
    print("graph helpers tests: OK")


if __name__ == "__main__":
    main()
