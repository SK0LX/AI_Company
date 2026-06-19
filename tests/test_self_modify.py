"""Unit tests for self-modification mode: the maintainer edits the app's OWN
repo through the same file/shell tools, but rooted at the repo and with VCS/
secret paths protected. No network.

    python tests/test_self_modify.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings

# Enable the feature BEFORE importing team_graph so its ROUTING_INSTRUCTIONS,
# built at import time, includes the self-modification guidance.
settings.enable_self_modify = True

from src.agents import tools as T  # noqa: E402
from src.graph import team_graph as G  # noqa: E402
from src.registry import registry  # noqa: E402


def _self_edit_root_and_guard(repo: str) -> None:
    registry.setup()  # so @requires can resolve permissions
    T.set_current_agent("")  # no agent attached -> @requires passes through
    settings.self_repo_dir = repo

    # Off by default: tools target the sandboxed workspace, not the repo.
    assert T.self_edit_on() is False

    T.set_self_edit(True)
    try:
        assert T.self_edit_on() is True
        # The sandbox root is now the repo root, not the workspace.
        assert T._workspace_root() == os.path.abspath(repo)

        # A normal source file is editable, and lands in the repo.
        assert T.write_file.invoke(
            {"path": "src/demo_x.py", "content": "x = 1"}
        ).startswith("[saved")
        assert os.path.isfile(os.path.join(repo, "src", "demo_x.py"))

        # Protected paths are denied even though they live inside the repo root.
        for p in (".git/config", "data/memory.sqlite", ".env", "secret.key", "src/app.key"):
            res = T.write_file.invoke({"path": p, "content": "x"})
            assert res.startswith("[denied"), f"{p!r} should be protected, got {res!r}"
        assert T.delete_file.invoke({"path": ".git/config"}).startswith("[denied")
        assert T.move_file.invoke(
            {"src": "src/demo_x.py", "dst": "data/x.py"}
        ).startswith("[denied")

        # Escaping the repo root is still rejected.
        assert "escapes the workspace" in T.write_file.invoke(
            {"path": "../evil", "content": "x"}
        )
    finally:
        T.set_self_edit(False)

    # Back to the sandbox after reset.
    assert T.self_edit_on() is False
    assert T._workspace_root() != os.path.abspath(repo)


def _registry_seed() -> None:
    registry.setup()
    # The built-in maintainer is present (seeded, or ensured into an older DB).
    assert registry.is_specialist("maintainer")
    perms = registry.permissions("maintainer")
    assert perms.get("can_self_modify") == "true"
    assert perms.get("can_edit_files") == "true"
    assert perms.get("can_run_shell") == "true"
    assert "maintainer" in registry.roster_block()


def _graph_helpers() -> None:
    registry.setup()
    assert G._is_self_editor("maintainer") is True
    assert G._is_self_editor("developer") is False  # no can_self_modify perm
    # Routing guidance is present because the feature is enabled in this process.
    assert "Self-modification" in G.ROUTING_INSTRUCTIONS


def main() -> None:
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as repo:
        settings.workspace_dir = ws
        _self_edit_root_and_guard(repo)
        _registry_seed()
        _graph_helpers()
    print("self-modify tests: OK")


if __name__ == "__main__":
    main()
