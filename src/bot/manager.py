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

# A short, role-flavoured "what am I doing" line for the live office map.
_WORK_NOTES = [
    ("analyst", "собирает требования…"), ("business", "уточняет задачу…"),
    ("system", "проектирует решение…"), ("frontend", "верстает интерфейс…"),
    ("backend", "пишет backend…"), ("developer", "пишет код…"),
    ("tester", "проверяет…"), ("qa", "тестирует…"), ("review", "ревьюит…"),
    ("design", "готовит макеты…"),
]


def _work_note(slug: str) -> str:
    from src.registry import registry

    a = registry.get(slug)
    hay = f"{slug} {(a.role if a else '')}".lower()
    for key, note in _WORK_NOTES:
        if key in hay:
            return note
    return "работает над задачей…"


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
        # Group-chat presence: ids of OUR bots (so their posts don't trigger us),
        # dedup of processed messages (one message reaches all bots), per-chat lock.
        self._bot_ids: set[int] = set()
        self._seen_msgs: deque = deque(maxlen=400)
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._group_pending: dict[int, str] = {}  # newest human msg seen during a burst

    # --- per-agent personal bot --------------------------------------------

    def _build_agent_app(self, slug: str, name: str, token: str) -> Application:
        app = Application.builder().token(token).concurrent_updates(True).build()

        async def on_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            if not msg or not msg.text or not chat:
                return
            # Group chats are handled centrally (one brain for all agent bots);
            # private chats are this agent's own 1:1 conversation.
            if chat.type != "private":
                await self._handle_group(msg, chat)
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

    # --- group chat presence -----------------------------------------------

    async def _handle_group(self, msg, chat) -> None:
        """Central brain for a group chat shared by the agent bots + real people.

        Every agent bot delivers the SAME group message, so we dedup by message id
        and process it once. Our own bots' posts are recorded for context but never
        trigger a turn — only HUMAN messages do, and the resulting turn-burst is
        hard-capped, so an infinite agent↔agent loop is impossible by construction."""
        from src import group_chat as gc
        from src.config import settings as _s

        if not _s.enable_group_chat:
            return
        key = (chat.id, msg.message_id)
        if key in self._seen_msgs:
            return
        self._seen_msgs.append(key)

        text = msg.text or ""
        sender = msg.from_user
        sender_name = (sender.first_name or sender.username or "user") if sender else "user"

        # One of OUR bots talking (an agent or the team/CEO bot): record team/CEO
        # posts for context; agent posts are already recorded via mark_post. Never
        # let a bot message start a turn.
        if sender and sender.id in self._bot_ids:
            is_agent_bot = any(sender.id == app.bot.id for _t, app in self._agent_apps.values())
            if not is_agent_bot:
                gc.observe(chat.id, name=sender_name, text=text)
            return

        # Human message: record + run a bounded turn-burst. If a burst is already
        # running for this chat, remember THIS message and let the running burst
        # pick it up when it finishes (so an addressed message is never lost).
        logger.info("group: received from %s in chat %s: %r", sender_name, chat.id, text[:60])
        gc.observe(chat.id, name=sender_name, text=text)
        lock = self._group_locks.setdefault(chat.id, asyncio.Lock())
        if lock.locked():
            self._group_pending[chat.id] = text
            return
        async with lock:
            trigger: str | None = text
            while trigger is not None:
                await self._run_group_burst(chat, trigger)
                # A human message that arrived during the burst (newest wins). This
                # is human-driven, so it can't loop on its own (bot posts never set
                # pending — they're filtered by _bot_ids and the message dedup).
                trigger = self._group_pending.pop(chat.id, None)

    async def _run_group_burst(self, chat, text: str) -> None:
        """Fully-independent mode: EVERY present agent runs its OWN Claude call and
        decides for itself whether to reply. Those who opt in post (a few may, or
        none). A direct work request to one agent goes through the tool path."""
        from src import group_chat as gc
        from src.graph.team_graph import agroup_decide, agroup_reply, aroute_group_speaker

        registry.reload()
        present = [
            s for s in list(self._agent_apps)
            if registry.get(s) and registry.get(s).enabled
        ]
        if not present:
            logger.info("group: no agent bots present — staying silent")
            return
        roster = [(s, registry.label(s), registry.get(s).role) for s in present]
        transcript = gc.transcript_text(chat.id)

        # Claude-CLI engine (subscription, zero per-token API money): EVERY message
        # goes through the lead ROUTER — the tech-lead itself decides whether to reply
        # (chit-chat) or delegate to NAMED agents (real work), instead of a brittle
        # keyword gate. Work always flows to a named agent (own claude session +
        # permission gate + own bot), never a lone claude doing it all.
        from src.config import settings as _eng
        if _eng.team_engine == "claude_cli":
            await self._run_group_team_claude(chat, text)
            return

        # A work request -> ONE agent actually does it (with tools). If it's
        # addressed to someone, that's the worker; otherwise pick the most fitting
        # teammate, so an unaddressed "почистите доску" doesn't fall into the
        # everyone-stays-silent path.
        addressed = gc.detect_addressed(text, roster)
        if gc.work_intent(text):
            worker = addressed if (addressed and addressed in self._agent_apps) else None
            if worker is None:
                pick, _why = await aroute_group_speaker(
                    transcript, roster, gc.last_speaker(chat.id)
                )
                worker = pick if (pick and pick in self._agent_apps) else present[0]
            logger.info("group: work request -> %s", worker)
            await self._run_work_chain(chat, worker, transcript, agroup_reply)
            return

        # Relevance gate v2 (0-token heuristic): only plausibly-relevant agents
        # incur an LLM "do you respond?" call — don't ask all 6 for every line.
        candidates = gc.relevance_prefilter(text, roster, addressed=addressed)
        deciders = [s for s in present if s in candidates]
        if not deciders:
            logger.info("group: relevance gate -> nobody (cheap skip)")
            return
        logger.info("group: %d/%d agents pass relevance gate on %r",
                    len(deciders), len(present), text[:50])
        decisions = await asyncio.gather(
            *[agroup_decide(s, transcript) for s in deciders], return_exceptions=True
        )
        responders = [
            (s, d[1]) for s, d in zip(deciders, decisions)
            if isinstance(d, tuple) and d[0] and d[1]
        ]
        logger.info("group: responders = %s", [s for s, _ in responders] or "(nobody)")
        from src import presence
        for i, (slug, reply) in enumerate(responders):
            if i:
                await asyncio.sleep(0.9)  # natural stagger so they don't land at once
            presence.set_activity(slug, "talking", "отвечает в чате…")  # live on the office map
            await self._group_post(chat, slug, reply)
            presence.clear_activity(slug)

    async def _run_work_chain(self, chat, starter: str, transcript: str, agroup_reply) -> None:
        """Flow a work request across the team: the first agent does its part, then
        may handoff('<teammate>', task) to pass the rest on (analyst → developer →
        tester …). Each agent posts as itself; bounded by group_handoff_max_depth so
        it can't loop or fan out forever."""
        from src import collab, presence
        from src.agents import tools as agent_tools
        from src.config import settings

        worker = starter
        task = transcript
        depth = 0
        try:
            while worker:
                agent_tools.clear_handoff(worker)
                presence.set_activity(worker, "working", _work_note(worker))  # live "thought"
                reply = await agroup_reply(
                    worker, task, work_intent=True, project=f"group-{chat.id}"
                )
                await self._group_post(chat, worker, reply)
                nxt = agent_tools.take_handoff(worker)
                if not nxt or depth >= settings.group_handoff_max_depth:
                    break
                to_slug, to_task = nxt
                if to_slug == worker or to_slug not in self._agent_apps:
                    break  # no self-handoff; the target must be a present agent
                # record the edge for the graph + show the hand-off live, then carry
                # the chat context + the ask to the next agent.
                collab.record_handoff(worker, to_slug, to_task)
                presence.set_activity(worker, "handoff", f"↪️ {registry.label(to_slug)}")
                await self._group_post(chat, worker, f"↪️ передаю @{registry.label(to_slug)}: {to_task[:160]}")
                task = f"{transcript}\n\n[Передано тебе от {registry.label(worker)}]: {to_task}"
                presence.clear_activity(worker)  # previous agent goes idle as the baton passes
                worker = to_slug
                depth += 1
        finally:
            presence.clear_activity(worker)

    async def _run_group_claude(self, chat, transcript: str) -> None:
        """All group responses via the Claude Code CLI (subscription) — one claude
        session decides + replies (and delegates + does real file work for actual
        tasks). Steps stream to the office; the answer is posted via the team bot.
        Empty answer (SILENT) = stay quiet."""
        from src.graph.team_graph import arun_group_claude

        async def on_event(_kind: str, line: str) -> None:
            try:
                from src.events import hub
                hub.publish({"event": "step", "actor": "claude", "text": line[:160]})
            except Exception:  # noqa: BLE001
                pass

        try:
            answer = await arun_group_claude(transcript, str(chat.id), on_event)
        except Exception:  # noqa: BLE001 - never let the engine crash the handler
            logger.exception("group claude engine crashed")
            return
        if answer:
            await self.post_to_chat(chat.id, answer[:3900])

    async def _run_group_team_claude(self, chat, text: str) -> None:
        """Lead-and-workers on the Claude engine: a lead splits a WORK request into
        subtasks for NAMED agents; each agent runs its subtask as its OWN claude
        session (subscription) and reports via its OWN Telegram bot. Multiple real
        agents do the work — the original AI-office vision, with zero API money."""
        from src import presence
        from src.graph.team_graph import arun_group_route, arun_group_summary, arun_specialist

        self._approval_chat_id = chat.id  # permission buttons go to THIS chat
        project = f"group-{chat.id}"

        # The lead router decides: reply itself (chit-chat) or delegate (work).
        try:
            kind, payload = await arun_group_route(text)
        except Exception:  # noqa: BLE001
            logger.exception("group route failed")
            kind, payload = "work", []

        if kind == "chat":
            reply = (payload or "").strip() if isinstance(payload, str) else ""
            if reply:
                await self.post_to_chat(chat.id, reply)  # the lead's own reply
            return

        plan = list(payload) if isinstance(payload, list) else []
        if not plan:
            # The lead flagged work but couldn't split it — hand the WHOLE task to the
            # best-fit NAMED agent so a real teammate (own bot) does it, never a lone
            # claude session.
            default_slug = self._default_group_agent()
            if not default_slug:
                return
            plan = [(default_slug, text)]

        await self.post_to_chat(chat.id, "🧠 Тех-лид раздаёт задачу команде…")
        await self.post_to_chat(
            chat.id,
            "📋 План:\n" + "\n".join(f"• {registry.label(s)} — {t[:90]}" for s, t in plan),
        )
        results: list[tuple[str, str]] = []
        for slug, subtask in plan:
            # Visible hand-off: the agent itself announces it took the subtask, via
            # its OWN bot — so you SEE "разраб взялся за …" in the chat, not just the
            # office ticker.
            await self.post_as(slug, chat.id, f"🛠 Взялся за: {subtask[:300]}")
            presence.set_activity(slug, "working", _work_note(slug))
            try:
                result = await arun_specialist(slug, subtask, project)
            except Exception:  # noqa: BLE001
                logger.exception("group team agent %s failed", slug)
                result = "не получилось выполнить подзадачу — гляну ещё раз."
            presence.clear_activity(slug)
            out = (result or "").strip()[:3500] or "готово."
            results.append((slug, out))
            if slug in self._agent_apps:
                await self.post_as(slug, chat.id, out)  # the agent's OWN bot
            else:
                await self.post_to_chat(chat.id, f"{registry.label(slug)}: {out}")

        # Lead wrap-up: the tech-lead summarizes what the team produced.
        try:
            summary = await arun_group_summary(text, results)
        except Exception:  # noqa: BLE001
            logger.exception("group summary failed")
            summary = ""
        if summary:
            await self.post_to_chat(chat.id, "✅ Итог тех-лида:\n" + summary[:3500])

    async def _group_post(self, chat, slug: str, reply: str) -> None:
        """Send one agent's group message via its own bot (with dedup + echo guard)."""
        from src import group_chat as gc

        reply = (reply or "").strip()
        if not reply or slug not in self._agent_apps or gc.is_duplicate(chat.id, reply):
            return
        bot = self._agent_apps[slug][1].bot
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass
        try:
            sent = await bot.send_message(chat_id=chat.id, text=reply)
            self._seen_msgs.append((chat.id, sent.message_id))  # ignore our own echo
        except Exception:  # noqa: BLE001
            logger.exception("group post failed for %s", slug)
            return
        gc.mark_post(chat.id, slug, registry.label(slug), reply)

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
                    await self._configure_webapp(slug, app)
                    try:
                        self._bot_ids.add(app.bot.id)
                    except Exception:  # noqa: BLE001
                        pass

    @staticmethod
    async def _configure_webapp(label: str, app: Application) -> None:
        """Set the bot's menu button to open the dashboard as a Telegram Mini App
        (only when WEBAPP_URL is a public HTTPS URL). Best-effort."""
        from src.config import settings

        if not settings.webapp_url:
            return
        try:
            from telegram import MenuButtonWebApp, WebAppInfo

            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Дашборд", web_app=WebAppInfo(url=settings.webapp_url)
                )
            )
        except Exception:  # noqa: BLE001 - a bad URL must not break the bot
            logger.exception("failed to set Mini App menu button for '%s'", label)

    async def post_to_chat(self, chat_id: int, text: str) -> None:
        """Send a message to any chat via the team bot. No-ops if the chat id is
        unset or the team bot is down. A failed post never crashes anything."""
        if not chat_id or self._team_app is None:
            return
        try:
            await self._team_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            logger.exception("failed to post to chat %s", chat_id)

    async def post_to_team(self, text: str) -> None:
        """Send a message to the configured team chat (used by the proactive service)."""
        from src.config import settings

        await self.post_to_chat(settings.team_chat_id, text)

    async def post_as(self, slug: str, chat_id: int, text: str) -> None:
        """Send a message AS a specific agent — via its OWN bot, so it shows up as
        that agent in the chat. Falls back to the team bot if the agent has no
        personal bot. Used by the outbox to deliver the `say` tool."""
        if not chat_id:
            return
        entry = self._agent_apps.get(slug)
        bot = entry[1].bot if entry else (self._team_app.bot if self._team_app else None)
        if bot is None:
            return
        try:
            sent = await bot.send_message(chat_id=chat_id, text=text)
            self._seen_msgs.append((chat_id, sent.message_id))  # ignore our own echo
        except Exception:  # noqa: BLE001 - a failed agent post must not crash anything
            logger.exception("post_as %s failed", slug)

    def notify_approval(self, approval_id: int, kind: str, summary: str, agent: str) -> None:
        """approvals.set_approval_notifier hook: push an Allow/Deny button into the
        chat where the current run is happening, so the user can approve a Claude-
        engine permission from the phone (not only the web dashboard). Best-effort,
        fire-and-forget — it never blocks the permission gate."""
        from src.config import settings

        chat_id = getattr(self, "_approval_chat_id", 0) or settings.team_chat_id
        if not chat_id or self._team_app is None:
            return
        try:
            asyncio.create_task(
                self._send_approval_button(chat_id, approval_id, summary, agent)
            )
        except RuntimeError:  # no running loop (shouldn't happen in the bot)
            logger.warning("notify_approval: no running loop")

    async def _send_approval_button(
        self, chat_id: int, approval_id: int, summary: str, agent: str
    ) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        who = registry.label(agent) if agent else "Агент"
        text = f"🔐 {who} просит доступ — разрешить?\n\n{summary}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Разрешить", callback_data=f"appr:{approval_id}:ok"),
            InlineKeyboardButton("⛔ Запретить", callback_data=f"appr:{approval_id}:no"),
        ]])
        try:
            await self._team_app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            logger.exception("failed to send approval button to chat %s", chat_id)

    def _default_group_agent(self) -> str:
        """The NAMED agent to hand a WORK task to when the lead couldn't split it —
        so a real teammate (own bot) does it, never a lone 'claude' session. Prefers
        a generalist, falls back to the first enabled specialist."""
        slugs = registry.specialist_slugs(enabled_only=True)
        for pref in ("developer", "reviewer", "system_analyst", "business_analyst"):
            if pref in slugs:
                return pref
        return slugs[0] if slugs else ""

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
        # Route Claude-engine permission approvals to Telegram (Allow/Deny buttons)
        from src import approvals
        approvals.set_approval_notifier(self.notify_approval)
        await self._configure_webapp("команда", self._team_app)
        try:
            self._bot_ids.add(self._team_app.bot.id)
        except Exception:  # noqa: BLE001
            pass
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
