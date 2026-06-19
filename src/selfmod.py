"""Isolated self-modification via git worktrees.

When the ``maintainer`` edits the bot's own code, we don't touch the live working
tree. Instead the orchestrator creates a dedicated ``git worktree`` on a fresh
branch in a temp directory, points the self-edit tools at THAT path, lets the
maintainer edit + run the tests there, and reports the branch + ``git diff
--stat``. The running bot is never disturbed and the change is trivially
reviewable (``git worktree list`` / open the path / merge the branch).

All git calls are best-effort and synchronous (call from a thread). If git or
worktrees are unavailable, callers fall back to in-place self-editing.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import tempfile

from src.config import settings

logger = logging.getLogger(__name__)


def repo_root() -> str:
    """The app's OWN repository root (configurable; defaults to this checkout)."""
    configured = (settings.self_repo_dir or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    # selfmod.py lives in src/, so its parent's parent is the repo root.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _git(cwd: str, args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"[git failed: {exc}]"


def is_git_repo(path: str) -> bool:
    rc, out = _git(path, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def _worktree_base() -> str:
    base = (settings.self_worktree_dir or "").strip() or os.path.join(
        tempfile.gettempdir(), "aiagents-worktrees")
    os.makedirs(base, exist_ok=True)
    return base


def create_worktree(role: str) -> dict | None:
    """Create an isolated worktree on a fresh branch for ``role``. Returns
    ``{path, branch, repo}`` or None if git/worktrees are unavailable."""
    repo = repo_root()
    if not is_git_repo(repo):
        logger.info("self-modify: %s is not a git repo; using in-place editing", repo)
        return None
    branch = f"self-edit/{role}-{secrets.token_hex(3)}"
    path = os.path.join(_worktree_base(), branch.replace("/", "-"))
    rc, out = _git(repo, ["worktree", "add", path, "-b", branch])
    if rc != 0:
        logger.warning("self-modify: worktree add failed: %s", out)
        return None
    logger.info("self-modify: created worktree %s on branch %s", path, branch)
    return {"path": path, "branch": branch, "repo": repo}


def diffstat(path: str) -> str:
    """A human-readable summary of the changes in a worktree (working tree)."""
    rc, out = _git(path, ["--no-pager", "diff", "--stat"])
    if rc != 0:
        return "[diff unavailable]"
    return out or "(нет изменений)"


def diff(path: str, max_chars: int = 6000) -> str:
    rc, out = _git(path, ["--no-pager", "diff"])
    if rc != 0:
        return "[diff unavailable]"
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [diff truncated]"
    return out or "(нет изменений)"


def remove_worktree(path: str) -> bool:
    """Tear down a worktree (does NOT delete its branch). Best-effort."""
    repo = repo_root()
    rc, _ = _git(repo, ["worktree", "remove", "--force", path])
    _git(repo, ["worktree", "prune"])
    return rc == 0


def list_worktrees() -> list[str]:
    rc, out = _git(repo_root(), ["worktree", "list"])
    return out.splitlines() if rc == 0 else []
