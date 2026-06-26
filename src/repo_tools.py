"""Read-only access to PUBLIC git repositories for the agents.

Lets a specialist actually look at a project's code (analyst → "what's in this
repo?", developer → "read src/app.py") WITHOUT shell access and without writing
anything. Safety:
- HTTPS only, host allow-list (no SSRF to internal services / metadata IPs).
- Shallow single-branch clone, no submodules, credential prompts disabled, with a
  wall-clock timeout and a post-clone size cap.
- Clones into a private cache dir (outside the agent workspace) and reuses it.
- File reads are path-sandboxed to the clone and size-capped; binaries are skipped.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from urllib.parse import urlparse

from src.config import settings

logger = logging.getLogger(__name__)

# Public git hosts we'll clone from. Anything else (incl. raw IPs / internal
# names) is refused so a URL can't be used to probe internal services.
_ALLOWED_HOSTS = {
    "github.com", "www.github.com", "gitlab.com", "bitbucket.org",
    "codeberg.org", "git.sr.ht", "sourceforge.net",
}
_CLONE_TIMEOUT = 90          # seconds
_MAX_REPO_BYTES = 150 * 1024 * 1024  # refuse a clone larger than this
_FILE_CAP = 12000            # chars returned from a single file
_TREE_CAP = 500              # files listed in the tree


def _cache_root() -> str:
    root = os.path.join(os.path.dirname(settings.db_path) or "data", "repo_cache")
    os.makedirs(root, exist_ok=True)
    return root


def _cache_path(url: str) -> str:
    return os.path.join(_cache_root(), hashlib.sha1(url.strip().encode()).hexdigest()[:16])


def _validate(url: str) -> str:
    """Return "" if ``url`` is an acceptable public https git URL, else a reason."""
    url = (url or "").strip()
    if not url or ".." in url:
        return "нужен корректный https git-URL"
    p = urlparse(url)
    if p.scheme != "https":
        return "только https git-URL"
    if (p.hostname or "").lower() not in _ALLOWED_HOSTS:
        return f"хост не разрешён (можно: {', '.join(sorted(_ALLOWED_HOSTS))})"
    return ""


def _dir_size(path: str) -> int:
    total = 0
    for dp, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
        if total > _MAX_REPO_BYTES:
            break
    return total


def fetch(url: str) -> tuple[str, str]:
    """Clone (or reuse a cached clone of) ``url``. Returns (local_path, error)."""
    err = _validate(url)
    if err:
        return "", err
    dest = _cache_path(url)
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest, ""  # already cloned — reuse
    shutil.rmtree(dest, ignore_errors=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", url, dest],
            capture_output=True, text=True, timeout=_CLONE_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return "", "клонирование заняло слишком долго (таймаут)"
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        return "", f"не удалось запустить git: {exc}"
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return "", f"git clone не удался: {(r.stderr or '').strip()[:200]}"
    if _dir_size(dest) > _MAX_REPO_BYTES:
        shutil.rmtree(dest, ignore_errors=True)
        return "", "репозиторий слишком большой"
    return dest, ""


def _read_capped(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read(_FILE_CAP + 1)
    except (UnicodeDecodeError, ValueError):
        return "[бинарный файл — не текст]"
    except OSError as exc:
        return f"[не прочитать: {exc}]"
    return data[:_FILE_CAP] + ("\n…(обрезано)" if len(data) > _FILE_CAP else "")


def tree(url: str) -> str:
    """File tree + README excerpt for a public repo."""
    dest, err = fetch(url)
    if err:
        return f"[{err}]"
    files: list[str] = []
    for dp, dirs, fs in os.walk(dest):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in fs:
            files.append(os.path.relpath(os.path.join(dp, f), dest))
        if len(files) > 5000:
            break
    files.sort()
    readme = ""
    for cand in ("README.md", "README.rst", "README.txt", "readme.md", "README"):
        rp = os.path.join(dest, cand)
        if os.path.isfile(rp):
            readme = _read_capped(rp)
            break
    out = (f"Репозиторий: {url}\nВсего файлов: {len(files)}\n\n"
           f"=== Структура (до {_TREE_CAP}) ===\n" + "\n".join(files[:_TREE_CAP]))
    if readme:
        out += f"\n\n=== README ===\n{readme}"
    return out[:8000]


def read_file(url: str, path: str) -> str:
    """Contents of ONE file in a public repo (path-sandboxed, size-capped)."""
    dest, err = fetch(url)
    if err:
        return f"[{err}]"
    root = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(dest, path or ""))
    if target != root and not target.startswith(root + os.sep):
        return "[путь вне репозитория]"
    if not os.path.isfile(target):
        return f"[файл не найден: {path}]"
    return _read_capped(target)
