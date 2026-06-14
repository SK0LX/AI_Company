"""Multi-bot manager (v2 stage 3 → unified process).

Runs the main "team" bot (orchestration, unchanged) plus ONE personal bot per
agent that has a Telegram token in the registry. A personal bot is a direct
conversation with that single agent (its persona + short history), with no team
orchestration and no file/shell tools — see :func:`aagent_reply`.

Polling mode (no public HTTPS needed). The manager exposes :meth:`start` /
:meth:`stop` so the bots can run *inside* the admin's FastAPI event loop (single
process). A background loop reconciles the running bots with the registry, and
the admin can call :meth:`reconcile_now` for an instant hot-add/-remove when an
agent's token changes. :meth:`run` keeps the standalone (bot-only) mode working.
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
_RECONCILE_EVERY = 15  # seconds between checks for newly added/removed agent bots


class TelegramManager:
    """Builds and runs the team bot + every agent's personal bot together.

    A background loop reconciles the running agent bots with the registry every
    few seconds, so adding/removing a token in the admin panel starts/stops that
    agent's bot WITHOUT restarting the process."""

    def __init__(self) -> None:
        # (slug, chat_id) -> recent conversation as (who, text) pairs.
        self._history: dict[tuple[str, int], deque] = defaultdict(
            lambda: deque(maxlen=_HISTORY_TURNS * 2)
        )
        # slug -> (decrypted_token, Application) for currently-running agent bots.
        self._agent_apps: dict[str, tuple[str, Application]] = {}
        # Filled in by start(); used to drive the background reconcile + shutdown.
        self._team_app: Application | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

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

    @staticmethod
    async def _start_app(label: str, app: Application) -> bool:
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
            )
            logger.info("bot online: %s", label)
            return True
        except Exception:  # noqa: BLE001 - one bad token must not kill the rest
            logger.exception("failed to start bot '%s' (bad token?)", label)
            try:
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
            return False

    @staticmethod
    async def _stop_app(label: str, app: Application) -> None:
        try:
            if app.updater:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("bot stopped: %s", label)
        except Exception:  # noqa: BLE001
            logger.exception("error stopping bot '%s'", label)

    async def _reconcile_agents(self) -> None:
        """Start bots for newly-added tokens, stop bots for removed/changed ones."""
        registry.reload()
        desired: dict[str, str] = {}
        for agent in registry.list_agents(enabled_only=True):
            token = registry.token_for(agent.slug)
            if token:
                desired[agent.slug] = token

        # Stop bots that are gone, disabled, or whose token changed.
        for slug in list(self._agent_apps):
            token, app = self._agent_apps[slug]
            if desired.get(slug) != token:
                await self._stop_app(slug, app)
                del self._agent_apps[slug]

        # Start bots for new/changed tokens.
        for slug, token in desired.items():
            if slug not in self._agent_apps:
                app = self._build_agent_app(slug, registry.label(slug), token)
                if await self._start_app(slug, app):
                    self._agent_apps[slug] = (token, app)

    async def post_to_team(self, text: str) -> None:
        """Send a message to the configured team chat via the team bot. Used by the
        proactive service; no-ops if there is no team chat or the bot is down."""
        from src.config import settings

        chat_id = settings.team_chat_id
        if not chat_id or self._team_app is None:
            return
        try:
            await self._team_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001 - a failed proactive post must not crash anything
            logger.exception("failed to post to team chat")

    async def reconcile_now(self) -> None:
        """Reconcile the running bots with the registry right now.

        Called by the admin panel after an agent is created/updated/deleted so a
        token change takes effect immediately instead of on the next tick."""
        try:
            await self._reconcile_agents()
        except Exception:  # noqa: BLE001
            logger.exception("reconcile failed")

    async def _reconcile_loop(self) -> None:
        """Background reconcile so panel token changes take effect without restart."""
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RECONCILE_EVERY)
            except asyncio.TimeoutError:
                await self.reconcile_now()

    async def start(self) -> None:
        """Start the team bot + agent bots inside the CURRENT event loop.

        Safe to run alongside an existing asyncio server (e.g. uvicorn). Does not
        install signal handlers or block — use :meth:`stop` to shut down."""
        registry.reload()
        self._team_app = build_application()
        if not await self._start_app("команда", self._team_app):
            logger.error("team bot failed to start — check TELEGRAM_BOT_TOKEN")
            self._team_app = None
            return
        await self._reconcile_agents()
        self._stop = asyncio.Event()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("Running: team + %d agent bot(s).", len(self._agent_apps))

    async def stop(self) -> None:
        """Stop the reconcile loop and every running bot."""
        if self._stop is not None:
            self._stop.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        if self._team_app is not None:
            await self._stop_app("команда", self._team_app)
            self._team_app = None
        for slug, (_token, app) in list(self._agent_apps.items()):
            await self._stop_app(slug, app)
        self._agent_apps.clear()

    async def _run_async(self) -> None:
        await self.start()
        if self._team_app is None:
            return  # team bot failed to start; nothing to wait on

        # Standalone mode owns the process, so it handles Ctrl+C / SIGTERM here.
        wait_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, wait_stop.set)
            except NotImplementedError:  # e.g. on Windows
                pass

        logger.info("Ctrl+C to stop.")
        await wait_stop.wait()
        logger.info("shutting down…")
        await self.stop()

    def run(self) -> None:
        """Standalone entry point (bots only, owns the event loop)."""
        asyncio.run(self._run_async())
