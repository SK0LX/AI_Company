"""File-tool sandbox guards: symlink escape + self-edit protected paths. No network.

    python tests/test_sandbox.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents import tools as T
from src.config import settings


def main() -> None:
    prev_ws = settings.workspace_dir
    ws = tempfile.mkdtemp()
    settings.workspace_dir = ws
    T.set_project_subdir("")
    try:
        # --- symlink escape: a link inside the workspace pointing OUT is rejected ---
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(ws, "escape"))
        try:
            T._safe_path("escape/evil.txt")
            raise AssertionError("symlink escape was NOT rejected")
        except ValueError:
            pass  # expected

        # a normal path stays inside the (real) workspace root
        inside = T._safe_path("sub/ok.txt")
        assert inside.startswith(os.path.realpath(ws) + os.sep)

        # --- self-edit protected paths -----------------------------------------
        assert T._self_edit_protected(".env")
        assert T._self_edit_protected("config/.env")          # .env at depth
        assert T._self_edit_protected("services/.env.prod")   # .env.* at depth
        assert T._self_edit_protected("deploy/server.key")    # key material
        assert T._self_edit_protected("certs/tls.pem")
        assert T._self_edit_protected("id_rsa")
        assert T._self_edit_protected(".git/config")
        assert T._self_edit_protected("data/app.sqlite")
        # legit source must NOT be blocked (no broad 'password'/'token' substring)
        assert not T._self_edit_protected("src/password_hashing.py")
        assert not T._self_edit_protected("src/auth/token_service.py")
        assert not T._self_edit_protected("README.md")

        # _deny_protected only bites in self-edit mode
        assert T._deny_protected(os.path.join(ws, ".env")) == ""  # mode off
        T.set_self_edit(True, root="")
        try:
            assert T._deny_protected(os.path.join(ws, ".env")) != ""
            assert T._deny_protected(os.path.join(ws, "main.py")) == ""
        finally:
            T.set_self_edit(False, root="")
    finally:
        settings.workspace_dir = prev_ws
    print("sandbox tests: OK")


if __name__ == "__main__":
    main()
