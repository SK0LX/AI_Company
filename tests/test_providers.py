"""Per-agent LLM provider resolution tests. No network (model construction only).

    python tests/test_providers.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.graph.team_graph import _make_model, _resolve_spec
from src.registry import registry


def main() -> None:
    registry.setup()

    # 1) an agent on the Claude API
    registry.create_agent({
        "slug": "_t_claude", "name": "Claude agent", "provider": "anthropic",
        "model": "claude-opus-4-8", "api_key": "sk-ant-test",
    })
    # 2) an agent on any OpenAI-compatible endpoint (e.g. Groq)
    registry.create_agent({
        "slug": "_t_groq", "name": "Groq agent", "provider": "openai_compatible",
        "model": "llama-3.3-70b", "api_key": "gsk-test",
        "base_url": "https://api.groq.com/openai/v1",
    })
    # 3) an agent with no provider -> falls back to the global one
    registry.create_agent({"slug": "_t_default", "name": "Default agent"})

    try:
        # resolution
        prov, model, key, url = _resolve_spec("_t_claude")
        assert (prov, model, key, url) == ("anthropic", "claude-opus-4-8", "sk-ant-test", "")
        prov, model, key, url = _resolve_spec("_t_groq")
        assert prov == "openai_compatible" and model == "llama-3.3-70b"
        assert key == "gsk-test" and url == "https://api.groq.com/openai/v1"
        prov, _, _, _ = _resolve_spec("_t_default")
        assert prov == settings.llm_provider  # fallback

        # secrets: encrypted at rest, decrypted on read, never exposed in as_dict
        d = registry.as_dict("_t_claude")
        assert d["has_api_key"] is True and "api_key" not in d
        assert registry.api_key_for("_t_claude") == "sk-ant-test"

        # model construction per provider (no network)
        m_opus = _make_model("anthropic", "claude-opus-4-8", "sk-ant-test", "")
        assert type(m_opus).__name__ == "ChatAnthropic"
        assert m_opus.temperature is None  # opus 4.8 rejects temperature -> omitted
        m_sonnet = _make_model("anthropic", "claude-sonnet-4-6", "sk-ant-test", "")
        assert m_sonnet.temperature is not None  # sonnet 4.6 accepts it

        m_groq = _make_model("openai_compatible", "llama-3.3-70b", "gsk-test",
                             "https://api.groq.com/openai/v1")
        assert type(m_groq).__name__ == "ChatOpenAI"

        m_or = _make_model("openrouter", "openai/gpt-oss-120b:free", "", "")
        assert type(m_or).__name__ == "ChatOpenAI"

        print("provider tests: OK")
    finally:
        for slug in ("_t_claude", "_t_groq", "_t_default"):
            try:
                registry.delete_agent(slug)
            except Exception:
                pass


if __name__ == "__main__":
    main()
