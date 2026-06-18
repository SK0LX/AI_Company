"""Unit tests for TelegramManager offline paths (no network).

Covers application construction, the config-gated webapp/post-to-team early
returns, and reconcile when no agent has a token configured.

    python tests/test_manager.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot.manager import TelegramManager
from src.config import settings
from src.registry import registry


async def _run() -> None:
    registry.setup()
    mgr = TelegramManager()

    # building an agent's personal bot is offline (no polling started)
    app = mgr._build_agent_app("developer", "Backend", "123456:fake-token")
    from telegram.ext import Application

    assert isinstance(app, Application)

    # post_to_team is a no-op without a configured team chat / running team bot
    settings.team_chat_id = 0
    await mgr.post_to_team("hello")  # must not raise

    # _configure_webapp is a no-op when WEBAPP_URL is unset
    prev_url = settings.webapp_url
    settings.webapp_url = ""
    await mgr._configure_webapp("команда", app)  # no-op, no network
    settings.webapp_url = prev_url

    # reconcile_now must never touch the network in tests: stub the bot
    # start/stop so it works whether or not agents have tokens in the DB.
    async def _no_start(_label, _app):
        return False

    async def _no_stop(_label, _app):
        return None

    mgr._start_app = _no_start  # type: ignore[assignment]
    mgr._stop_app = _no_stop  # type: ignore[assignment]
    await mgr.reconcile_now()
    assert mgr._agent_apps == {}  # nothing "started" because _start_app returns False

    # stop() is safe even when nothing was started
    await mgr.stop()


def main() -> None:
    asyncio.run(_run())
    print("manager tests: OK")


if __name__ == "__main__":
    main()
