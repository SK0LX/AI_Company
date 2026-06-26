"""Tools the agents can call.

- File tools (``write_file`` / ``read_file`` / ``list_files``): let the Developer
  and Frontend agents save real project files into the shared workspace
  (``settings.workspace_dir``). All paths are sandboxed to that directory.
- ``run_python``: a Python code runner so the Developer can verify its code.

SECURITY: ``run_python`` executes model-generated Python on the host machine. It
runs in an isolated subprocess (``python -I``) with a timeout and a scratch
working directory, but it is NOT a real sandbox. It is gated behind
``settings.enable_code_execution`` and disabled by default. The file tools can
only touch paths inside ``settings.workspace_dir``.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import os
import re
import sys
import tempfile

from langchain_core.tools import tool

from src.config import settings

logger = logging.getLogger(__name__)

_MAX_OUTPUT = 4000
_MAX_FILE_READ = 20000

# The project subfolder the current specialist writes into. Set per request so
# that every specialist working on the same task shares ONE project folder.
_project_subdir: contextvars.ContextVar[str] = contextvars.ContextVar(
    "project_subdir", default=""
)

# Slug of the agent currently running a tool. Set per specialist run so the
# @requires guard can check that agent's permissions and attribute audit entries.
_current_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_agent", default=""
)

# Self-modification mode. When True, the file/shell tools operate on the app's
# OWN source repository (so an agent can change the running bot itself) instead
# of the sandboxed workspace. Off by default; the orchestrator flips it on only
# for a self-editing agent, and only when settings.enable_self_modify is set.
_self_edit: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "self_edit", default=False
)

# Optional explicit self-edit root (a git worktree). When set, the file/shell
# tools operate there instead of the live repo, isolating self-modification.
_self_edit_root: contextvars.ContextVar[str] = contextvars.ContextVar(
    "self_edit_root", default=""
)


def set_current_agent(slug: str) -> None:
    """Mark which agent is about to use the tools (for @requires + auditing)."""
    _current_agent.set(slug or "")


def set_self_edit(on: bool, root: str = "") -> None:
    """Point the file/shell tools at the app's OWN code (self-modification) for
    the current async context. ``root`` optionally pins them to a specific
    directory (e.g. an isolated git worktree) instead of the live repo root."""
    _self_edit.set(bool(on))
    _self_edit_root.set(root or "")


def self_edit_on() -> bool:
    return _self_edit.get()


# --- permission enforcement (v2 stage 4) ------------------------------------

def _agent_has(slug: str, permission: str) -> bool:
    from src.registry import registry

    return registry.permissions(slug).get(permission) == "true"


def _audit(actor: str, action: str, target: str, **details: object) -> None:
    """Append a security-relevant action to the audit_log table. Best-effort."""
    try:
        from src.db.engine import get_session
        from src.db.models import AuditLog

        with get_session() as session:
            session.add(
                AuditLog(
                    actor=actor or "system",
                    action=action,
                    target=target,
                    details_json=json.dumps(details, default=str),
                )
            )
            session.commit()
        try:
            from src.events import hub

            hub.publish({"event": "audit", "actor": actor or "system",
                         "action": action, "target": target})
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - auditing must never break a tool call
        logger.exception("failed to write audit log (%s/%s)", action, target)


def requires(permission: str):
    """Decorator: block a tool unless the CURRENT agent holds ``permission``.

    Enforced only when an agent context is set (:func:`set_current_agent`);
    standalone calls with no agent attached pass through. Denials return a clear
    string the model can read, and are recorded in the audit log. Apply BELOW
    ``@tool`` so langchain still introspects the real signature/docstring."""

    def decorator(func):
        denial = f"[denied: this agent lacks the '{permission}' permission]"

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                slug = _current_agent.get()
                if slug and not _agent_has(slug, permission):
                    _audit(slug, "denied", func.__name__, permission=permission)
                    return denial
                return await func(*args, **kwargs)

            return awrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            slug = _current_agent.get()
            if slug and not _agent_has(slug, permission):
                _audit(slug, "denied", func.__name__, permission=permission)
                return denial
            return func(*args, **kwargs)

        return wrapper

    return decorator


def sanitize_project(name: str) -> str:
    """Turn an arbitrary project name into a safe single-folder slug."""
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-._")
    name = name.replace("..", "-")
    return name or "project"


def set_project_subdir(name: str) -> None:
    """Point the file tools at ``<workspace>/<name>`` for the current context."""
    _project_subdir.set(sanitize_project(name) if name else "")


def _repo_root() -> str:
    """The app's OWN source repository root (target of self-modification)."""
    configured = (settings.self_repo_dir or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    # Default: the checkout that contains this package (src/agents/tools.py).
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _workspace_root() -> str:
    # Self-edit mode swaps the sandbox root from the workspace to the real repo,
    # so every existing file/shell tool transparently operates on the app's own
    # code — still confined to that root by _safe_path.
    if _self_edit.get():
        override = _self_edit_root.get()
        root = os.path.abspath(os.path.expanduser(override)) if override else _repo_root()
        os.makedirs(root, exist_ok=True)
        return root
    root = os.path.abspath(os.path.expanduser(settings.workspace_dir))
    sub = _project_subdir.get()
    if sub:
        root = os.path.join(root, sub)
    os.makedirs(root, exist_ok=True)
    return root


# Paths that the mutating file tools must never touch in self-edit mode, even
# though they live inside the repo root: VCS internals, env/secrets and runtime
# state (the DB, keys). Reading is fine; writing/moving/deleting is blocked.
def _self_edit_protected(rel_path: str) -> bool:
    # normpath collapses any leading "./"; do NOT lstrip("./") here — that would
    # also eat the leading dot of ".git"/".env" and defeat the guard.
    norm = os.path.normpath(rel_path).replace(os.sep, "/").lower()
    if not norm or norm == ".":
        return False
    parts = norm.split("/")
    # The runtime DB dir is the repo-root `data/` only (a source folder named
    # `data` deeper in the tree is legit, e.g. src/data/).
    if parts[0] == "data":
        return True
    # Everything below is checked on EVERY path component — not just the leaf —
    # so a protected name used as a *directory* (.env/x, .env/.git/hooks/x) can't
    # smuggle a write past a basename-only check.
    for part in parts:
        if part == ".git":  # VCS internals at any depth
            return True
        if part == ".env" or part.startswith(".env."):  # env files/dirs at any depth
            return True
        # secret material by file name / extension (not a broad substring, so legit
        # sources like password_hashing.py / token_service.py still pass).
        if part.endswith((".key", ".pem", ".crt", ".p12", ".pfx")) or part in ("id_rsa", "credentials.json"):
            return True
    if "secret" in norm:
        return True
    return False


def _deny_protected(target: str) -> str:
    """Return a denial string if ``target`` is protected in self-edit mode, else ""."""
    if not _self_edit.get():
        return ""
    # Resolve symlinks first: a link inside the repo must not smuggle a write into
    # .git/.env/secrets via a path that *looks* innocent.
    root = os.path.realpath(_workspace_root())
    rel = os.path.relpath(os.path.realpath(target), root)
    if _self_edit_protected(rel):
        return f"[denied: '{rel}' is protected in self-edit mode]"
    return ""


def current_project_files() -> list[str]:
    """Files actually saved in the active project folder (paths relative to it).

    Used to ground the team in reality: what's truly on disk, not what an agent
    *claims* it saved."""
    root = _workspace_root()
    found: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def _safe_path(rel_path: str) -> str:
    """Resolve ``rel_path`` inside the workspace, rejecting any escape attempt.

    Uses ``realpath`` so a symlink (one the agent itself may have created) that
    points outside the workspace is rejected — ``abspath`` alone would keep the
    link's innocent-looking literal path and let the write land outside."""
    root = os.path.realpath(_workspace_root())
    candidate = os.path.realpath(os.path.join(root, rel_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(
            f"path {rel_path!r} escapes the workspace; use a path inside it"
        )
    return candidate


def _prune_empty_dirs(start: str) -> None:
    """Remove now-empty folders from ``start`` up toward the project root.

    Used after moving/deleting files so the layout doesn't keep hollow folders
    (e.g. an empty ``app/`` left behind after relocating its files)."""
    root = _workspace_root()
    current = os.path.dirname(start)
    while current and current.startswith(root + os.sep) and current != root:
        try:
            os.rmdir(current)  # only succeeds when the directory is empty
        except OSError:
            break
        current = os.path.dirname(current)


@tool
@requires("can_edit_files")
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file in the CURRENT project's folder.

    `path` is RELATIVE to this project's root; parent folders are created
    automatically. Organize parts in subfolders (e.g. "backend/main.py",
    "frontend/index.html", "docs/architecture.md"). Do NOT prepend the project
    name yourself — you are already inside the project folder. Returns a
    confirmation with the saved location."""
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
    if denial := _deny_protected(target):
        return denial
    os.makedirs(os.path.dirname(target) or _workspace_root(), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"[saved {len(content)} chars to {target}]"


@tool
@requires("can_edit_files")
def move_file(src: str, dst: str) -> str:
    """Move or rename a file inside the CURRENT project. Both paths are relative
    to the project root; parent folders for `dst` are created automatically. Use
    this to fix a file that was saved in the wrong place (e.g. move 'app/main.py'
    to 'backend/app/main.py')."""
    try:
        source = _safe_path(src)
        target = _safe_path(dst)
    except ValueError as exc:
        return f"[error: {exc}]"
    if denial := (_deny_protected(source) or _deny_protected(target)):
        return denial
    if not os.path.isfile(source):
        return f"[not found: {src}]"
    os.makedirs(os.path.dirname(target) or _workspace_root(), exist_ok=True)
    os.replace(source, target)
    _prune_empty_dirs(source)
    return f"[moved {src} -> {dst}]"


@tool
@requires("can_edit_files")
def delete_file(path: str) -> str:
    """Delete a stray or duplicate file from the CURRENT project. `path` is
    relative to the project root. Use sparingly, only to clean up files that do
    not belong in the agreed structure."""
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
    if denial := _deny_protected(target):
        return denial
    if not os.path.isfile(target):
        return f"[not found: {path}]"
    os.remove(target)
    _prune_empty_dirs(target)
    return f"[deleted {path}]"


@tool
def read_file(path: str) -> str:
    """Read a file from the shared team workspace. `path` is relative to the
    workspace root. Use this to inspect files a teammate already created."""
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
    if not os.path.isfile(target):
        return f"[not found: {path}]"
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        data = fh.read(_MAX_FILE_READ + 1)
    if len(data) > _MAX_FILE_READ:
        data = data[:_MAX_FILE_READ] + "\n... [truncated]"
    return data or "[empty file]"


@tool
def list_files(subdir: str = "") -> str:
    """List files in the shared team workspace (optionally within `subdir`).
    Use this to see what projects/files already exist before writing."""
    try:
        base = _safe_path(subdir) if subdir else _workspace_root()
    except ValueError as exc:
        return f"[error: {exc}]"
    if not os.path.isdir(base):
        return f"[no such folder: {subdir}]"
    root = _workspace_root()
    found: list[str] = []
    for dirpath, _dirs, files in os.walk(base):
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    if not found:
        return "[workspace is empty]"
    return "\n".join(sorted(found))


# --- task board (the kanban the user sees) ----------------------------------
#
# These let an agent inspect and tidy the team's task board. Reading is open;
# mutations require the ``can_manage_board`` permission.

@tool
def board_overview() -> str:
    """See the team's TASK BOARD (the kanban the user sees): how many tasks are in
    each column and the total. Columns: new, in_progress, blocked, review, done,
    cancelled. Call this first when asked about or to tidy the board."""
    from src import collab

    o = collab.board_overview()
    lines = [f"Доска задач — всего {o['total']}:"]
    lines += [f"  {st}: {n}" for st, n in o["by_status"].items()]
    return "\n".join(lines)


@tool
@requires("can_manage_board")
def board_set_status(task_id: int, status: str) -> str:
    """Move ONE task to a column. status ∈ new|in_progress|blocked|review|done|
    cancelled."""
    from src.db.models import TASK_STATUSES

    if status not in TASK_STATUSES:
        return f"[bad status {status!r}; use one of {TASK_STATUSES}]"
    from src import collab

    collab.set_task_status(task_id, status, actor=_current_agent.get() or None)
    return f"[task {task_id} -> {status}]"


@tool
@requires("can_manage_board")
def board_delete(task_id: int) -> str:
    """Permanently delete ONE task (and its timeline) from the board."""
    from src import collab

    return f"[deleted task {task_id}]" if collab.delete_task(task_id) else f"[not found: {task_id}]"


@tool
@requires("can_manage_board")
def board_clear(status: str = "", mode: str = "cancel") -> str:
    """Bulk-tidy the board when asked. ``mode='cancel'`` moves tasks to the
    'cancelled' column (reversible); ``mode='delete'`` removes them permanently —
    use delete when the user wants the board emptied / tasks gone. ``status``
    limits to one column (e.g. 'done'); empty = ALL columns."""
    from src import collab

    st = (status or "").strip() or None
    mode = "delete" if mode == "delete" else "cancel"
    n = collab.clear_board(status=st, mode=mode)
    verb = "удалено" if mode == "delete" else "перенесено в «Отменено»"
    where = f" в колонке «{st}»" if st else ""
    return f"[{verb} задач: {n}{where}]"


# --- coordination: atomic claim + resource locks (v3 pull-model) ------------
#
# Before working on a shared task/project, an agent CLAIMS it so no one else
# double-works. "Ask the CEO if anyone's on it" == call board_claim and read the
# result. Locks are advisory keys ('repo:x', 'file:y', 'area:frontend').

@tool
def board_claim(task_id: int) -> str:
    """Atomically claim a task BEFORE working on it, so no other agent works it in
    parallel. Returns whether you got it; if busy, pick another task or wait."""
    from src import locks

    agent = _current_agent.get() or "system"
    if locks.claim_task(task_id, agent):
        return f"[claimed task {task_id} — it's yours, go]"
    holder = locks.task_holder(task_id)
    return f"[busy: task {task_id} is held by {holder or 'someone'} — take another or wait]"


@tool
def board_release(task_id: int) -> str:
    """Release a task you claimed (when done or handing off)."""
    from src import locks

    agent = _current_agent.get() or "system"
    return (f"[released task {task_id}]" if locks.release_task(task_id, agent)
            else f"[you don't hold task {task_id}]")


@tool
def lock_acquire(key: str) -> str:
    """Acquire an advisory lock on a resource before editing it, so parallel agents
    don't clash. Examples: 'repo:my-project', 'file:src/app.py', 'area:frontend'."""
    from src import locks

    agent = _current_agent.get() or "system"
    if locks.acquire_lock(key, agent):
        return f"[locked {key} — safe to edit]"
    return f"[busy: {key} held by {locks.who_holds(key) or 'someone'} — wait]"


@tool
def lock_release(key: str) -> str:
    """Release a resource lock you hold."""
    from src import locks

    agent = _current_agent.get() or "system"
    return f"[unlocked {key}]" if locks.release_lock(key, agent=agent) else f"[not held: {key}]"


@tool
def lock_who(key: str) -> str:
    """Who, if anyone, holds the lock on a resource (read-only check)."""
    from src import locks

    holder = locks.who_holds(key)
    return f"[{key}: held by {holder}]" if holder else f"[{key}: free]"


@tool
def say(text: str) -> str:
    """Speak in the TEAM CHAT as YOURSELF (your own Telegram bot), mid-work. Use it
    to tell the user what you're doing, or to address a teammate by name — e.g.
    '@developer, нужен endpoint /react на :3006'. Delivered to Telegram from your
    bot, so it shows up as you."""
    from src import outbox

    agent = _current_agent.get()
    if not agent:
        return "[say недоступен: нет текущего агента]"
    return "[отправлено в чат]" if outbox.enqueue_say(agent, text) else "[пустое сообщение]"


@tool
def budget_remaining() -> str:
    """Your current budget status (spent vs limit). Check before expensive work and
    self-throttle when near the limit."""
    from src import budget

    agent = _current_agent.get() or "system"
    g = budget.gate(agent)
    if not g.get("limit"):
        return "[no budget limit set — spend responsibly]"
    return (f"[budget {g['scope']}/{g['window']}: spent ${g['spent']:.2f} / "
            f"${g['limit']:.2f} — status {g['status']}]")


# --- Obsidian-style wiki (long-term team knowledge base) --------------------
#
# A separate Markdown vault (``settings.wiki_dir``) the team uses as long-term
# memory: project cards, architectural decisions, glossary. It is just a folder
# of ``.md`` files, so you can open it directly in Obsidian. All access is
# sandboxed to the vault, exactly like the project workspace.

_WIKI_SEARCH_SNIPPET = 240
_WIKI_MAX_RESULTS = 8


def _wiki_root() -> str:
    root = os.path.abspath(os.path.expanduser(settings.wiki_dir))
    os.makedirs(root, exist_ok=True)
    return root


def _safe_wiki_path(rel_path: str) -> str:
    """Resolve ``rel_path`` inside the wiki vault, rejecting any escape attempt."""
    root = _wiki_root()
    if not rel_path.endswith(".md"):
        rel_path = f"{rel_path}.md"
    candidate = os.path.abspath(os.path.join(root, rel_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(
            f"path {rel_path!r} escapes the wiki vault; use a path inside it"
        )
    return candidate


def wiki_write_note(path: str, content: str) -> str:
    """Create or overwrite a Markdown note in the wiki vault (``.md`` enforced)."""
    try:
        target = _safe_wiki_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
    os.makedirs(os.path.dirname(target) or _wiki_root(), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"[saved wiki note {os.path.relpath(target, _wiki_root())}]"


def wiki_read_note(path: str) -> str:
    """Read a Markdown note from the wiki vault. Returns a marker if missing."""
    try:
        target = _safe_wiki_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
    if not os.path.isfile(target):
        return f"[not found: {path}]"
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        data = fh.read(_MAX_FILE_READ + 1)
    if len(data) > _MAX_FILE_READ:
        data = data[:_MAX_FILE_READ] + "\n... [truncated]"
    return data or "[empty note]"


def wiki_search_notes(query: str, max_results: int = _WIKI_MAX_RESULTS) -> str:
    """Full-text search across the vault. Returns matching notes with snippets.

    Case-insensitive substring match on note path and body. Used to surface
    relevant past projects/decisions before the team starts new work."""
    root = _wiki_root()
    terms = [t for t in re.split(r"\s+", (query or "").lower()) if t]
    hits: list[tuple[int, str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read(_MAX_FILE_READ)
            except OSError:
                continue
            haystack = f"{rel}\n{body}".lower()
            score = sum(haystack.count(t) for t in terms) if terms else 1
            if score <= 0:
                continue
            idx = -1
            for t in terms:
                idx = body.lower().find(t)
                if idx >= 0:
                    break
            if idx < 0:
                idx = 0
            start = max(0, idx - 60)
            snippet = body[start : start + _WIKI_SEARCH_SNIPPET].strip().replace("\n", " ")
            hits.append((score, rel, snippet))
    if not hits:
        return "[no matching wiki notes]"
    hits.sort(key=lambda h: h[0], reverse=True)
    lines = [f"- {rel}: {snippet}" for _score, rel, snippet in hits[:max_results]]
    return "\n".join(lines)


def wiki_index(max_notes: int = 30) -> str:
    """A compact listing of every note in the vault (path + first heading)."""
    root = _wiki_root()
    entries: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            title = ""
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("#"):
                            title = line.lstrip("# ").strip()
                            break
                        if line and not title:
                            title = line[:80]
            except OSError:
                continue
            entries.append(f"- {rel}" + (f" — {title}" if title else ""))
    if not entries:
        return "[wiki is empty]"
    entries.sort()
    if len(entries) > max_notes:
        entries = entries[:max_notes] + [f"... (+{len(entries) - max_notes} more)"]
    return "\n".join(entries)


# --- shared team memory (the Obsidian vault) exposed as agent tools ----------
#
# Every agent reads from and writes to ONE shared knowledge base, so the team
# accumulates and reuses what it learns across tasks. These thin @tool wrappers
# let agents call the vault during a run (the plain functions above are used by
# the orchestrator for priming/the end-of-task note).

@tool
def search_memory(query: str) -> str:
    """Search the team's SHARED long-term memory — an Obsidian Markdown vault of
    past projects, architecture, decisions and how-tos. Call this BEFORE starting
    work to reuse what the team already knows. Returns matching notes + snippets."""
    return wiki_search_notes(query)


@tool
def read_memory(path: str) -> str:
    """Read a full note from the team's shared memory by path (e.g.
    'projects/coffee-shop' or 'decisions/auth'). Use after search_memory to get
    the details of a relevant note."""
    return wiki_read_note(path)


@tool
def list_memory() -> str:
    """List every note in the team's shared memory (path + title) so you can see
    what knowledge already exists before adding more."""
    return wiki_index()


@tool
def save_memory(path: str, content: str) -> str:
    """Save or update a Markdown note in the team's SHARED memory so the whole
    team — now and in the future — can reuse it. Record DURABLE knowledge: what a
    thing is and HOW it works, key decisions and WHY, the file/folder map (path —
    purpose), setup/run/test steps, and gotchas. `path` is a slug like
    'decisions/db-choice' or 'projects/<name>'; it overwrites the note there.
    Prefer updating an existing note (read it first) over creating duplicates."""
    return wiki_write_note(path, content)


# A best-effort denylist of obviously-catastrophic / secret-exfil command shapes.
# IMPORTANT: this is a SPEED BUMP, not a security boundary — a determined shell can
# always obfuscate (more encoders, interpreters, syntax). The real protections are:
# shell execution is OFF by default, every command needs explicit human approval
# showing the literal command, and in the Docker deploy the shell runs inside the
# container. This list just blocks the easy/accidental footguns. (label, regex):
_NET = r"curl|wget|fetch|nc|ncat|netcat|socat|telnet|ftp|tftp|scp|rsync|ssh|sftp|mail|sendmail|aws|gsutil|rclone|az"
_SHELL_DENY: list[tuple[str, "re.Pattern[str]"]] = [
    ("recursive delete of an absolute/home path", re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\b[^|;&]*\s(/|~|\$HOME)", re.I)),
    ("destructive rm via a variable or substitution", re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\b[^|;&]*(\$\(|`|\$\{?[A-Za-z_])", re.I)),
    ("find with -delete / -exec rm", re.compile(r"\bfind\b[^|;&]*-(delete|exec\s+rm)\b", re.I)),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    ("filesystem format", re.compile(r"\bmkfs|\bmke2fs\b", re.I)),
    ("raw write to a disk device", re.compile(r"\bdd\b[^|;&]*\bof=/dev/|>\s*/dev/(sd|nvme|disk|vd)", re.I)),
    ("write to a system file", re.compile(r"(>>?\s*|\btee\b[^|;&]*\s)/etc/(passwd|shadow|sudoers|hosts)\b", re.I)),
    ("reference to a sensitive system file", re.compile(r"/etc/(shadow|gshadow|sudoers)\b", re.I)),
    ("host shutdown/reboot", re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b", re.I)),
    ("world-writable chmod on root", re.compile(r"\bchmod\s+-[a-z]*\s*777\s+/(\s|$)", re.I)),
    ("remote/encoded script piped to a shell",
     re.compile(r"\b(curl|wget|fetch|base64|xxd|od|hexdump|openssl)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|python\d?|perl|ruby|node|php)\b", re.I)),
    ("a shell reading its script from a redirect", re.compile(r"\b(sh|bash|zsh|dash)\b\s*<\s*\S", re.I)),
    ("interpreter one-liner running OS/shell commands",
     re.compile(r"\b(python\d?|perl|ruby|node|php)\b[^|;&]*\s-(c|e|r)\b[^|;&]*(system|exec|popen|spawn|child_process|subprocess|os\.|rmtree|unlink|rm\s+-rf)", re.I)),
    ("eval of dynamic content", re.compile(r"\beval\b", re.I)),
    ("IFS field-separator tampering", re.compile(r"\bIFS=")),
    ("secret exfiltration over the network",
     re.compile(rf"\b({_NET})\b[^|;&]*(\.env\b|id_rsa\b|secret\.key\b|\.ssh\b|credentials)", re.I)),
    ("secret read piped/redirected to the network",
     re.compile(rf"(\.env\b|id_rsa\b|secret\.key\b|\.ssh\b|credentials).*[|<].*\b({_NET})\b", re.I)),
]


def _shell_danger(command: str) -> str:
    """Return a reason if ``command`` matches a hard-blocked pattern, else "".

    Matches the raw command AND a normalized copy (shell quotes + backslashes
    stripped, whitespace collapsed) so `rm -rf '/'`, `rm -rf "/"` and `rm -rf \\/`
    can't hide the path behind quoting/escaping."""
    norm = re.sub(r"\s+", " ", command.replace("\\", "").replace("'", "").replace('"', ""))
    for label, pat in _SHELL_DENY:
        if pat.search(command) or pat.search(norm):
            return label
    return ""


@tool
@requires("can_run_shell")
async def run_shell(command: str) -> str:
    """Run a shell command in the CURRENT project's folder and return its combined
    output. Use this for real installs/builds/tests: `npm install`, `npm test`,
    `pytest`, `pip install -r requirements.txt`, `docker compose up --build`, etc.

    The command runs on the host machine, so EVERY command must be approved by
    the user first (they see it and tap a button). If they decline, you get
    "[skipped by user]" — adapt accordingly. Prefer one clear command per call and
    explain what you expect it to do."""
    if not settings.enable_shell_execution:
        return "[shell execution is disabled by the operator]"

    command = (command or "").strip()
    if not command:
        return "[empty command]"

    # Defense-in-depth: refuse catastrophic / secret-exfil commands OUTRIGHT, before
    # they can even be approved (a mis-tap must not be able to wipe the host or pipe
    # tokens out). The approval step below is the second gate, not the only one.
    danger = _shell_danger(command)
    if danger:
        return f"[blocked by safety policy: {danger}]"

    # Human-in-the-loop: block until the user approves via Telegram.
    from src.approvals import request_command_approval

    if not await request_command_approval(command):
        return "[skipped by user]"

    root = _workspace_root()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=settings.shell_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"$ {command}\n[timed out after {settings.shell_timeout}s]"
    except Exception as exc:  # noqa: BLE001
        return f"$ {command}\n[failed to start: {exc}]"

    output = stdout.decode("utf-8", errors="replace").strip()
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n... [output truncated]"
    rc = proc.returncode
    head = f"$ {command}  (exit {rc})\n"
    return head + (output or "[no output]")


@tool
async def run_python(code: str) -> str:
    """Execute a snippet of Python 3 in an isolated subprocess and return its
    combined stdout/stderr. Use this to verify code works or to compute a quick
    result. Only the Python standard library is guaranteed to be available, and
    there is no network access. Print what you want to inspect."""
    with tempfile.TemporaryDirectory() as workdir:
        script = os.path.join(workdir, "snippet.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(code)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # isolated mode: ignore env/user site, safer defaults
                script,
                cwd=workdir,
                env={"PATH": os.environ.get("PATH", "")},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.code_exec_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"[timed out after {settings.code_exec_timeout}s]"
        except Exception as exc:  # noqa: BLE001
            return f"[failed to execute: {exc}]"

    output = stdout.decode("utf-8", errors="replace").strip()
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n... [output truncated]"
    return output or "[no output]"
