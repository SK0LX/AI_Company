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
import os
import re
import sys
import tempfile

from langchain_core.tools import tool

from src.config import settings

_MAX_OUTPUT = 4000
_MAX_FILE_READ = 20000

# The project subfolder the current specialist writes into. Set per request so
# that every specialist working on the same task shares ONE project folder.
_project_subdir: contextvars.ContextVar[str] = contextvars.ContextVar(
    "project_subdir", default=""
)


def sanitize_project(name: str) -> str:
    """Turn an arbitrary project name into a safe single-folder slug."""
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-._")
    name = name.replace("..", "-")
    return name or "project"


def set_project_subdir(name: str) -> None:
    """Point the file tools at ``<workspace>/<name>`` for the current context."""
    _project_subdir.set(sanitize_project(name) if name else "")


def _workspace_root() -> str:
    root = os.path.abspath(os.path.expanduser(settings.workspace_dir))
    sub = _project_subdir.get()
    if sub:
        root = os.path.join(root, sub)
    os.makedirs(root, exist_ok=True)
    return root


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
    """Resolve ``rel_path`` inside the workspace, rejecting any escape attempt."""
    root = _workspace_root()
    candidate = os.path.abspath(os.path.join(root, rel_path))
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
    os.makedirs(os.path.dirname(target) or _workspace_root(), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return f"[saved {len(content)} chars to {target}]"


@tool
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
    if not os.path.isfile(source):
        return f"[not found: {src}]"
    os.makedirs(os.path.dirname(target) or _workspace_root(), exist_ok=True)
    os.replace(source, target)
    _prune_empty_dirs(source)
    return f"[moved {src} -> {dst}]"


@tool
def delete_file(path: str) -> str:
    """Delete a stray or duplicate file from the CURRENT project. `path` is
    relative to the project root. Use sparingly, only to clean up files that do
    not belong in the agreed structure."""
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"[error: {exc}]"
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


@tool
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
