"""Human-in-the-loop approval bridge for shell commands.

The ``run_shell`` tool runs deep inside a specialist's ReAct agent, with no
direct access to Telegram. Before a command executes it must get the user's
approval. This module bridges the two:

- The bot installs an "asker" (via :func:`set_asker`) for the current run — a
  coroutine that shows the command to the user with approve/skip buttons and
  returns ``True``/``False``. It's stored in a ``ContextVar`` so concurrent runs
  in different chats each carry their own asker.
- The tool calls :func:`request_command_approval`, which invokes that asker.

If no asker is installed (e.g. a direct ``/ask`` outside the team flow), the
command is denied by default — better safe than sorry.
"""
from __future__ import annotations

import contextvars
from typing import Awaitable, Callable, Optional

Asker = Callable[[str], Awaitable[bool]]

_asker: contextvars.ContextVar[Optional[Asker]] = contextvars.ContextVar(
    "command_asker", default=None
)


def set_asker(fn: Optional[Asker]) -> None:
    """Install the approval function for the current execution context."""
    _asker.set(fn)


def clear_asker() -> None:
    _asker.set(None)


async def request_command_approval(command: str) -> bool:
    """Ask the user to approve ``command``. Denies if no asker is installed."""
    fn = _asker.get()
    if fn is None:
        return False
    try:
        return bool(await fn(command))
    except Exception:  # noqa: BLE001 - a broken approval channel must not run cmds
        return False
