"""Unit tests for git-worktree-isolated self-modification. No network.

Spins up a throwaway git repo so the real one is never touched.

    python tests/test_selfmod.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import selfmod
from src.agents import tools as T
from src.config import settings


def _init_repo(repo: str) -> None:
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Tester"], check=True)
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("# demo\n")
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    with open(os.path.join(repo, "src", "app.py"), "w") as fh:
        fh.write("x = 1\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "init"], check=True)


def _worktree_flow(repo: str, wt_base: str) -> None:
    settings.self_repo_dir = repo
    settings.self_worktree_dir = wt_base

    assert selfmod.repo_root() == os.path.abspath(repo)
    assert selfmod.is_git_repo(repo) is True
    with tempfile.TemporaryDirectory() as nongit:
        assert selfmod.is_git_repo(nongit) is False

    wt = selfmod.create_worktree("maintainer")
    assert wt is not None
    assert os.path.isdir(wt["path"])
    assert wt["branch"].startswith("self-edit/maintainer-")

    # modifying a TRACKED file in the worktree shows up in the diffstat
    with open(os.path.join(wt["path"], "README.md"), "a") as fh:
        fh.write("\nchanged by maintainer\n")
    stat = selfmod.diffstat(wt["path"])
    assert "README" in stat, stat
    assert "README" in selfmod.diff(wt["path"])
    assert any("self-edit/maintainer-" in line for line in selfmod.list_worktrees())

    # the file tools, pointed at the worktree, write THERE and still protect .git
    T.set_current_agent("")  # no agent -> @requires passes
    T.set_self_edit(True, root=wt["path"])
    try:
        assert T._workspace_root() == os.path.abspath(wt["path"])
        assert T.write_file.invoke({"path": "src/new.py", "content": "y = 2"}).startswith("[saved")
        assert os.path.isfile(os.path.join(wt["path"], "src", "new.py"))
        assert T.write_file.invoke({"path": ".git/config", "content": "x"}).startswith("[denied")
        assert "escapes the workspace" in T.write_file.invoke({"path": "../escape", "content": "x"})
    finally:
        T.set_self_edit(False)

    # teardown removes the worktree (branch is left behind for review)
    assert selfmod.remove_worktree(wt["path"]) is True
    assert not os.path.isdir(wt["path"])


def main() -> None:
    repo0, wt0 = settings.self_repo_dir, settings.self_worktree_dir
    try:
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as wt_base:
            _init_repo(repo)
            _worktree_flow(repo, wt_base)
    finally:
        settings.self_repo_dir, settings.self_worktree_dir = repo0, wt0
        T.set_self_edit(False)
    print("selfmod tests: OK")


if __name__ == "__main__":
    main()
