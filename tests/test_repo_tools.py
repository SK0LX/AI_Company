"""Read-only repo tools: URL validation + sandboxed reads. Offline (a fake clone
is seeded into the cache, so no network/git is needed).

    python tests/test_repo_tools.py
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import repo_tools
from src.agents import tools as T
from src.config import settings


def _validate() -> None:
    assert repo_tools._validate("https://github.com/user/proj") == ""
    assert repo_tools._validate("http://github.com/user/proj")        # http rejected
    assert repo_tools._validate("https://169.254.169.254/latest")     # host not allowed
    assert repo_tools._validate("https://evil.internal/x")            # host not allowed
    assert repo_tools._validate("https://github.com/../../etc")       # '..' rejected
    assert repo_tools._validate("")                                   # empty rejected


def _read_seeded() -> None:
    url = "https://github.com/test/fake-repo"
    dest = repo_tools._cache_path(url)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.join(dest, ".git"), exist_ok=True)
    os.makedirs(os.path.join(dest, "src"), exist_ok=True)
    with open(os.path.join(dest, "README.md"), "w") as f:
        f.write("# Fake project\nhello world")
    with open(os.path.join(dest, "src", "a.py"), "w") as f:
        f.write("print('hi from a')")
    try:
        tree = repo_tools.tree(url)
        assert "src/a.py" in tree and "Fake project" in tree

        assert "hi from a" in repo_tools.read_file(url, "src/a.py")
        assert "вне репозитория" in repo_tools.read_file(url, "../../../etc/passwd")
        assert "не найден" in repo_tools.read_file(url, "nope.txt")
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _tool_gate() -> None:
    prev = settings.enable_repo_read
    settings.enable_repo_read = False
    try:
        assert "выключено" in T.repo_tree.invoke({"git_url": "https://github.com/a/b"})
        assert "выключено" in T.repo_file.invoke({"git_url": "https://github.com/a/b", "path": "x"})
    finally:
        settings.enable_repo_read = prev


def main() -> None:
    _validate()
    _read_seeded()
    _tool_gate()
    print("repo tools tests: OK")


if __name__ == "__main__":
    main()
