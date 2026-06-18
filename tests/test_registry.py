"""Unit tests for the agent registry CRUD + cache. No network.

    python tests/test_registry.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.registry import registry

SLUG = "_ut_reg"


def main() -> None:
    registry.setup()

    # seeding is idempotent: ceo + specialists present
    assert registry.get("ceo") is not None
    assert registry.is_specialist("developer") and not registry.is_specialist("ceo")
    n_before = len(registry.list_agents())

    # create with permissions + obligation + secret
    registry.create_agent({
        "slug": SLUG, "name": "Reg agent", "role": "qa",
        "system_prompt": "p", "model": "m", "telegram_token": "tok-123",
        "permissions": {"can_run_shell": "true"}, "obligation": "do qa",
    })
    try:
        assert registry.get(SLUG) is not None
        assert registry.permissions(SLUG) == {"can_run_shell": "true"}
        assert registry.obligation(SLUG) == "do qa"
        assert registry.token_for(SLUG) == "tok-123"  # encrypted at rest, decrypts
        assert registry.as_dict(SLUG)["has_token"] is True
        assert len(registry.list_agents()) == n_before + 1

        # update: replace permissions, keep token (no token in payload)
        registry.update_agent(SLUG, {
            "name": "Renamed", "permissions": {"can_edit_files": "true"}, "obligation": "new",
        })
        assert registry.label(SLUG) == "Renamed"
        assert registry.permissions(SLUG) == {"can_edit_files": "true"}
        assert registry.obligation(SLUG) == "new"
        assert registry.token_for(SLUG) == "tok-123"  # unchanged

        # enabled toggle reflected in specialist_slugs / list filter
        registry.update_agent(SLUG, {"enabled": False})
        assert SLUG not in registry.specialist_slugs(enabled_only=True)
        assert any(a.slug == SLUG for a in registry.list_agents())  # still listed (all)

        # reload reflects DB state
        registry.reload()
        assert registry.get(SLUG).name == "Renamed"
    finally:
        registry.delete_agent(SLUG)

    assert registry.get(SLUG) is None
    assert len(registry.list_agents()) == n_before

    # guards for unknown slugs return safe empties (no crash)
    assert registry.permissions("nope_zzz") == {}
    assert registry.obligation("nope_zzz") == ""
    assert registry.prompt("nope_zzz") == ""
    assert registry.model_for("nope_zzz") == ""
    assert registry.provider_for("nope_zzz") == ""
    assert registry.api_key_for("nope_zzz") == ""
    assert registry.token_for("nope_zzz") == ""
    assert registry.label("nope_zzz") == "nope_zzz"
    assert registry.as_dict("nope_zzz") is None

    # roster accessors used to build the CEO prompt
    assert registry.ceo_prompt()
    block = registry.roster_block()
    assert "developer" in block and "ceo" not in block.split("\n", 1)[1]
    assert "developer" in registry.specialist_slugs()

    print("registry tests: OK")


if __name__ == "__main__":
    main()
