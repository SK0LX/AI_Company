"""Permission policy for the Claude-engine agents — the Claude-Code-style model.

Every tool the agent wants to use is sorted into ONE of 4 categories. Then, per
category, it is either auto-allowed (AUTO mode) or sent to the operator as a typed
approval that they resolve in the web dashboard (ASK mode). This is the
``can_use_tool`` callback for the Claude Agent SDK — it replaces the blunt
``bypassPermissions`` (unrestricted root) with a controlled, per-category gate.

The 4 categories:
  read  📖  read/browse files + search        (Read, Glob, Grep, LS, …)
  edit  ✏️  create/modify files               (Write, Edit, MultiEdit, …)
  exec  ⚙️  run shell commands (incl. git/build/test/run)   (Bash, …)
  net   🌐  reach the network                 (WebFetch, WebSearch)
Internal orchestration tools (delegating to a subagent, todo list) are always
allowed — they don't touch the host.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

CATEGORIES = ("read", "edit", "exec", "net")
CATEGORY_LABEL = {
    "read": "📖 чтение файлов",
    "edit": "✏️ правка файлов",
    "exec": "⚙️ команда (bash/git/сборка)",
    "net": "🌐 сеть (web/загрузка)",
}

# tool name -> category. Anything not listed and not in _ALWAYS_ALLOW is treated
# as "exec" (the safe default: ask before it runs).
_TOOL_CATEGORY = {
    "Read": "read", "Glob": "read", "Grep": "read", "LS": "read",
    "NotebookRead": "read", "BashOutput": "read",
    "Write": "edit", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "exec", "KillBash": "exec", "KillShell": "exec",
    "WebFetch": "net", "WebSearch": "net",
}
# Internal/orchestration tools that never touch the host — always allowed.
_ALWAYS_ALLOW = {"Task", "Agent", "TodoWrite", "ExitPlanMode", "Skill", "ToolSearch"}


def categorize(tool_name: str) -> str | None:
    """Return the category for a tool, or None if it's an always-allowed internal tool."""
    if tool_name in _ALWAYS_ALLOW:
        return None
    return _TOOL_CATEGORY.get(tool_name, "exec")


def _short_input(tool_name: str, tool_input: dict) -> str:
    """A compact, human-readable summary of what the tool is about to do."""
    ti = tool_input or {}
    for key in ("command", "file_path", "path", "url", "pattern", "query"):
        if ti.get(key):
            return str(ti[key])[:160]
    try:
        return json.dumps(ti, ensure_ascii=False)[:160]
    except Exception:  # noqa: BLE001
        return str(ti)[:160]


# decider(category, tool_name, summary, agent) -> Awaitable[bool]  (True = allow)
Decider = Callable[[str, str, str, str], Awaitable[bool]]


def make_can_use_tool(agent: str, *, decider: Decider | None = None):
    """Build a Claude-Agent-SDK ``can_use_tool`` callback for one agent run.

    AUTO mode (``settings.claude_auto_approve`` or the per-category flag) → allow.
    ASK mode → ``decider`` decides (e.g. a web approval); default decider routes to
    the typed-approval system so the operator resolves it in the dashboard."""
    from src.config import settings

    async def _default_decider(category: str, tool_name: str, summary: str, who: str) -> bool:
        from src import approvals

        return await approvals.request_approval(
            f"agent_{category}", f"{CATEGORY_LABEL.get(category, category)} · {tool_name}: {summary}",
            agent=who, require_asker=False,  # the dashboard can approve even with no Telegram asker
        )

    resolve = decider or _default_decider
    granted: set[str] = set()   # categories the operator already OK'd THIS run (sticky)
    denied: set[str] = set()    # categories explicitly refused THIS run (don't re-ask)

    async def can_use_tool(tool_name: str, tool_input: dict, context):  # noqa: ANN001
        # SDK passes (tool_name, input, ToolPermissionContext); return Allow/Deny.
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        category = categorize(tool_name)
        if category is None:  # internal orchestration — always fine
            return PermissionResultAllow()
        if _auto_for(settings, category) or category in granted:
            return PermissionResultAllow()
        if category in denied:
            return PermissionResultDeny(message=f"Категория «{CATEGORY_LABEL.get(category, category)}» уже отклонена для этой задачи.")
        summary = _short_input(tool_name, tool_input)
        try:
            ok = await resolve(category, tool_name, summary, agent)
        except Exception:  # noqa: BLE001 - a broken approver denies, never crashes
            logger.exception("permission decider failed")
            ok = False
        if ok:
            granted.add(category)  # approve the category once → rest of the run flows
            return PermissionResultAllow()
        denied.add(category)
        return PermissionResultDeny(message=f"Операция «{CATEGORY_LABEL.get(category, category)}» отклонена оператором.")

    return can_use_tool


def _auto_for(settings, category: str) -> bool:
    """Whether ``category`` is auto-approved (global AUTO, or its own flag)."""
    if getattr(settings, "claude_auto_approve", False):
        return True
    return bool(getattr(settings, f"claude_auto_{category}", False))
