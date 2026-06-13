"""Multi-bot manager (v2 stage 3).

Runs the main "team" bot (orchestration, unchanged) plus ONE personal bot per
agent that has a Telegram token in the registry. A personal bot is a direct
conversation with that single agent (its persona + short history), with no team
orchestration and no file/shell tools — see :func:`aagent_reply`.

Polling mode (no public HTTPS needed). Per-agent bots are loaded at startup;
adding a token in the admin panel takes effect after a restart (live hot-add and
webhook mode come when the bot + admin share one process / move to a server).
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict, deque

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from src.bot.telegram_bot import build_application, on_error
from src.graph.team_graph import aagent_reply
from src.registry import registry

logger = logging.getLogger(__name__)

_HISTORY_TURNS = 8  # how many (user+agent) messages to keep per personal chat


class TelegramManager:
    """Builds and runs the team bot + every agent's personal bot together."""

    def __init__(self) -> None:
        # (slug, chat_id) -> recent conversation as (who, text) pairs.
        self._history: dict[tuple[str, int], deque] = defaultdict(
            lambda: deque(maxlen=_HISTORY_TURNS * 2)
        )

    # --- per-agent personal bot --------------------------------------------

    def _build_agent_app(self, slug: str, name: str, token: str) -> Application:
        app = Application.builder().token(token).concurrent_updates(True).build()

        async def on_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            if not msg or not msg.text or not chat:
                return
            # Personal bots answer in private chats only (keep groups for the team).
            if chat.type != "private":
                return
            await chat.send_action(ChatAction.TYPING)
            registry.reload()
            key = (slug, chat.id)
            history = list(self._history[key])
            try:
                reply = await aagent_reply(slug, msg.text, history)
            except Exception:  # noqa: BLE001
                logger.exception("agent DM failed for %s", slug)
                await context.bot.send_message(
                    chat_id=chat.id, text="⚠️ Что-то пошло не так. Попробуй ещё раз."
                )
                return
            self._history[key].append(("user", msg.text))
            self._history[key].append(("agent", reply))
            await context.bot.send_message(chat_id=chat.id, text=reply)

        async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat = update.effective_chat
            if chat:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"Привет! Я {name}. Пиши мне напрямую — отвечу по своей роли.",
                )

        from telegram.ext import CommandHandler

        app.add_handler(CommandHandler(["start", "help"], on_start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_dm))
        app.add_error_handler(on_error)
        return app

    # --- lifecycle ----------------------------------------------------------

    async def _run_async(self) -> None:
        registry.reload()
        apps: list[tuple[str, Application]] = [("команда", build_application())]
        for agent in registry.list_agents(enabled_only=True):
            if agent.telegram_token:
                apps.append(
                    (
                        agent.slug,
                        self._build_agent_app(agent.slug, agent.name, agent.telegram_token),
                    )
                )

        started: list[tuple[str, Application]] = []
        for label, app in apps:
            try:
                await app.initialize()
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
                )
                started.append((label, app))
                logger.info("bot online: %s", label)
            except Exception:  # noqa: BLE001 - one bad token must not kill the rest
                logger.exception("failed to start bot '%s' (bad token?)", label)

        if not started:
            logger.error("no bots started — check tokens")
            return
        logger.info("%d bot(s) running. Ctrl+C to stop.", len(started))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # e.g. on Windows
                pass
        await stop.wait()

        logger.info("shutting down %d bot(s)…", len(started))
        for label, app in reversed(started):
            try:
                if app.updater:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("error stopping bot '%s'", label)

    def run(self) -> None:
        asyncio.run(self._run_async())
