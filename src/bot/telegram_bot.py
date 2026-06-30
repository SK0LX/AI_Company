"""Telegram interface for the AI IT company.

Routing rules:
- Private chat: every text message goes to the CEO-led team.
- Group chat: the team replies when the bot is @mentioned or its message is
  replied to, plus the explicit commands below (commands work regardless of
  Telegram's group privacy mode).

Commands:
  /start, /help   - intro and usage
  /roles          - list the team members
  /team <task>    - send a task to the whole team (useful in groups)
  /ask <role> ... - ask one specialist directly (e.g. /ask developer ...)
  /reset          - start a fresh conversation in this chat
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import approvals, quota
from src.config import settings
from src.registry import registry
from src.graph.team_graph import (
    aquick_reply,
    arun_specialist,
    arun_team,
    atranslate_ru,
)


def _is_rate_limit(exc: BaseException) -> bool:
    """True for an OpenRouter/OpenAI daily-cap 429 (vs. other failures)."""
    text = str(exc).lower()
    return type(exc).__name__ == "RateLimitError" or "rate limit" in text or "429" in text

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096

# Short aliases accepted by /ask in addition to the canonical role keys.
ROLE_ALIASES: dict[str, str] = {
    "ba": "business_analyst",
    "sa": "system_analyst",
    "dev": "developer",
    "backend": "developer",
    "fe": "frontend",
    "front": "frontend",
    "qa": "tester",
    "test": "tester",
    "ux": "designer",
    "ui": "designer",
    "design": "designer",
    "br": "backend_reviewer",
    "bre": "backend_reviewer",
    "backendreview": "backend_reviewer",
    "fr": "frontend_reviewer",
    "fre": "frontend_reviewer",
    "frontreview": "frontend_reviewer",
    "lead": "reviewer",
    "techlead": "reviewer",
    "review": "reviewer",
}

HELP_TEXT = (
    "*AI IT Company* — a team of 7 agents led by a CEO.\n\n"
    "In a private chat just write your task. In a group, mention me "
    "(@{username}) or reply to my message.\n\n"
    "Tip: start a message with a role prefix to talk to one specialist directly, "
    "e.g. `dev: write a binary search` or `qa: test cases for login`.\n\n"
    "*Commands*\n"
    "/team <task> — give a task to the whole team\n"
    "/ask <role> <question> — ask one specialist (roles: ba, sa, dev, fe, qa, ux)\n"
    "/roles — list the team\n"
    "/reset — start a fresh conversation\n"
)


# --- thread management ------------------------------------------------------

def _thread_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Per-chat conversation id. /reset rotates the suffix to wipe history."""
    chat_id = update.effective_chat.id
    suffix = context.chat_data.get("thread_suffix", 0)
    return f"{chat_id}:{suffix}"


def _extract_role(text: str) -> tuple[str | None, str]:
    """Detect a leading 'role:' prefix (e.g. 'dev: ...'). Returns (role, rest)."""
    if ":" not in text:
        return None, text
    prefix, rest = text.split(":", 1)
    key = prefix.strip().lower()
    role = ROLE_ALIASES.get(key, key)
    if registry.is_specialist(role) and rest.strip():
        return role, rest.strip()
    return None, text


# --- message sending --------------------------------------------------------

def _split(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send(bot, chat_id: int, text: str) -> None:
    """Send a (possibly long) message to a chat by id. Try Markdown, fall back to
    plain text. Sending by chat_id (not reply_to) is robust for button callbacks,
    where the originating message can be inaccessible."""
    for chunk in _split(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await bot.send_message(chat_id=chat_id, text=chunk)


async def _reply(update: Update, text: str) -> None:
    """Send a (possibly long) reply to the update's chat."""
    await _send(update.get_bot(), update.effective_chat.id, text)


# Approve / change / cancel buttons shown under a proposed plan.
PLAN_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Делаем", callback_data="plan:go"),
            InlineKeyboardButton("✏️ Изменить", callback_data="plan:edit"),
            InlineKeyboardButton("✖️ Отмена", callback_data="plan:cancel"),
        ]
    ]
)


async def _reply_plan(update: Update, text: str) -> None:
    """Send the CEO's proposed plan with approve/change/cancel buttons attached
    to the last message chunk."""
    bot = update.get_bot()
    chat_id = update.effective_chat.id
    chunks = _split(text)
    for chunk in chunks[:-1]:
        await _send(bot, chat_id, chunk)
    last = chunks[-1]
    try:
        await bot.send_message(
            chat_id=chat_id, text=last,
            parse_mode=ParseMode.MARKDOWN, reply_markup=PLAN_KEYBOARD,
        )
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=last, reply_markup=PLAN_KEYBOARD)


# --- command handlers -------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT.format(username=context.bot.username))


async def roles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    registry.reload()
    lines = ["*Команда*"] + [
        f"• {a.name} (`{a.slug}`)" + ("" if a.enabled else " — выкл.")
        for a in registry.list_agents()
    ]
    await _reply(update, "\n".join(lines))


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["thread_suffix"] = context.chat_data.get("thread_suffix", 0) + 1
    await _reply(update, "Conversation reset. Starting fresh. 🧹")


async def team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = " ".join(context.args).strip()
    if not task:
        await _reply(update, "Usage: /team <your task>")
        return
    await _run_team(update, context, task)


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    registry.reload()
    if not context.args:
        roles = ", ".join(registry.specialist_slugs())
        await _reply(update, f"Usage: /ask <role> <question>\nRoles: {roles}")
        return

    raw_role = context.args[0].lower()
    role = ROLE_ALIASES.get(raw_role, raw_role)
    if not registry.is_specialist(role):
        roles = ", ".join(registry.specialist_slugs())
        await _reply(update, f"Unknown role '{raw_role}'.\nAvailable: {roles}")
        return

    question = " ".join(context.args[1:]).strip()
    if not question:
        await _reply(update, "Please add a question after the role.")
        return

    await _run_specialist(update, role, question)


# --- free-text handler ------------------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    chat = update.effective_chat
    text = message.text

    if chat.type != "private":
        mention = f"@{context.bot.username}"
        is_mention = mention.lower() in text.lower()
        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == context.bot.id
        )
        if not (is_mention or is_reply_to_bot):
            return
        text = text.replace(mention, "").strip()

    if not text:
        return

    # A leading "role:" prefix talks to one specialist directly.
    role, payload = _extract_role(text)
    if role:
        await _run_specialist(update, role, payload)
        return

    await _run_team(update, context, text)


# --- core runners -----------------------------------------------------------

async def _run_team(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    ack: str | None = "🚀 Принял задачу, анализирую…",
    plan_approved: bool = False,
) -> None:
    # Pick up any agent edits made in the admin panel since the last task.
    registry.reload()
    thread_id = _thread_id(update, context)
    # One task at a time per chat (they share a thread_id / checkpointer). While a
    # run is in progress, the bot stays responsive (concurrent_updates) and other
    # chats run in parallel.
    lock: asyncio.Lock = context.chat_data.setdefault("_team_lock", asyncio.Lock())

    # Team is already busy: don't start or queue a NEW task. Let the user talk to
    # the CEO live and fold any addition into the running task.
    if lock.locked():
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            reply = await aquick_reply(text, thread_id=thread_id)
        except Exception:  # noqa: BLE001
            logger.exception("quick reply failed")
            reply = "Принял сообщение — учту в текущей задаче."
        await _reply(update, f"💬 {reply}")
        return

    bot = update.get_bot()
    chat_id = update.effective_chat.id

    # Acquire immediately (no await between the check above and this) so two
    # near-simultaneous messages can't both start a full task.
    await lock.acquire()
    # Wire the per-command approval channel for tools running in THIS run.
    approvals.set_asker(lambda command: _ask_command(bot, chat_id, command))
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
        # Immediate acknowledgement so the user knows the request was received.
        # Skipped on plan-approval resume (the "▶️ выполняю план" already said it).
        if ack:
            await _send(bot, chat_id, ack)

        # A single "live" message that accumulates the CEO's delegation steps and
        # is edited in place ("🧭 подключаю X — делаю Y"). Specialist results are
        # sent as their own messages. `progress` holds the message + its lines.
        progress: dict = {"msg_id": None, "lines": []}

        async def _render_progress() -> None:
            body = "🛠 Команда работает над задачей:\n\n" + "\n".join(progress["lines"])
            # Keep the live message under Telegram's limit: if it grows too long,
            # start a fresh one (keep the most recent lines for context).
            if len(body) > 3500:
                progress["lines"] = progress["lines"][-6:]
                progress["msg_id"] = None
                body = "🛠 Команда работает над задачей:\n\n" + "\n".join(progress["lines"])
            if progress["msg_id"] is None:
                sent = await bot.send_message(chat_id=chat_id, text=body)
                progress["msg_id"] = sent.message_id
            else:
                try:
                    await bot.edit_message_text(
                        text=body, chat_id=chat_id, message_id=progress["msg_id"]
                    )
                except BadRequest:
                    pass  # "not modified" / edit window issues — safe to ignore

        async def on_event(kind: str, text_: str) -> None:
            if kind == "delegate":
                progress["lines"].append(text_)
                await _render_progress()
            else:  # "result": a specialist finished — send it as its own message
                await _send(bot, chat_id, text_)
            await update.effective_chat.send_action(ChatAction.TYPING)

        try:
            answer, awaiting_kind, did_work = await arun_team(
                text,
                thread_id=thread_id,
                on_event=on_event if settings.show_team_chatter else None,
                plan_approved=plan_approved,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc):
                logger.warning("OpenRouter daily free quota exhausted")
                await _send(bot, chat_id, quota.exhausted_warning())
            else:
                logger.exception("team run failed")
                await _send(bot, chat_id, "⚠️ The team hit an error processing that. Try /reset.")
            return

        if awaiting_kind == "plan":
            # CEO proposes a plan and waits for approval (buttons below).
            await _reply_plan(update, f"📋 *План работы*\n\n{answer}")
        elif awaiting_kind == "clarify":
            # CEO needs an answer before it can continue.
            await _reply(update, f"❓ {answer}")
        elif did_work:
            # Real work finished — announce completion explicitly.
            await _reply(update, f"✅ *Готово, работа завершена!*\n\n{answer}")
        else:
            # Small talk / a direct answer — no completion banner needed.
            await _reply(update, answer)

        # Proactively warn when the free daily quota is running low.
        if quota.is_low():
            await _reply(update, quota.low_warning())
    finally:
        approvals.clear_asker()
        lock.release()


# --- shell command approval (human-in-the-loop) -----------------------------

# Pending command approvals: request id -> Future resolved by the user's button.
_cmd_pending: dict[str, asyncio.Future] = {}


async def _ask_command(bot, chat_id: int, command: str) -> bool:
    """Show a command with approve/skip buttons and wait for the user's tap.
    Returns True if approved, False if skipped or not confirmed in time."""
    rid = uuid.uuid4().hex[:10]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _cmd_pending[rid] = fut
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Выполнить", callback_data=f"cmd:{rid}:ok"),
            InlineKeyboardButton("✖️ Пропустить", callback_data=f"cmd:{rid}:no"),
        ]]
    )
    text = (
        "🔧 Команда требует подтверждения. Выполнить её в папке проекта?\n\n"
        f"{command}"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001
        logger.exception("failed to send command approval prompt")
        _cmd_pending.pop(rid, None)
        return False
    try:
        return await asyncio.wait_for(
            fut, timeout=settings.command_approval_timeout
        )
    except asyncio.TimeoutError:
        await _send(bot, chat_id, "⌛️ Команда не подтверждена вовремя — пропускаю.")
        return False
    finally:
        _cmd_pending.pop(rid, None)


async def on_appr_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Resolve a Claude-engine permission approval from its Telegram button.

    callback_data is ``appr:{approval_id}:ok|no``. Unlike the shell path (which
    owns its own _cmd_pending future), this resolves the typed-approval system via
    ``approvals.decide`` — the SAME future the web dashboard would, so Telegram and
    the dashboard race and whichever taps first wins."""
    from src import approvals

    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:  # noqa: BLE001
        pass
    parts = (query.data or "").split(":")
    try:
        approval_id = int(parts[1])
    except (IndexError, ValueError):
        return
    approved = len(parts) > 2 and parts[2] == "ok"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    resolved = approvals.decide(approval_id, approved, reason="telegram")
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return
    if resolved:
        await context.bot.send_message(
            chat_id=chat_id, text="✅ Разрешено." if approved else "⛔ Запрещено."
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text="⏳ Это согласование уже неактуально."
        )


async def on_cmd_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Resolve a pending shell-command approval from its button."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:  # noqa: BLE001
        pass
    parts = (query.data or "").split(":")
    rid = parts[1] if len(parts) > 1 else ""
    approved = len(parts) > 2 and parts[2] == "ok"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    chat_id = update.effective_chat.id if update.effective_chat else None
    fut = _cmd_pending.get(rid)
    if fut and not fut.done():
        fut.set_result(approved)
        if chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id, text="▶️ Выполняю…" if approved else "⏭️ Пропущено."
            )
    elif chat_id is not None:
        await context.bot.send_message(
            chat_id=chat_id, text="⏳ Это подтверждение уже неактуально."
        )


async def on_plan_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the approve/change/cancel buttons under a proposed plan."""
    query = update.callback_query
    if not query:
        return
    action = (query.data or "").split(":", 1)[-1]
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info("plan button pressed: %s (chat %s)", action, chat_id)

    try:
        await query.answer()  # stop Telegram's loading spinner
    except Exception:  # noqa: BLE001 - stale/expired query, keep going
        logger.warning("callback answer failed", exc_info=True)
    # Drop the buttons so the plan can't be actioned twice.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    # Send via the chat id directly (more robust than replying to the button
    # message, which can be inaccessible) and surface any failure to the user.
    async def say(text: str) -> None:
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text)

    try:
        if action == "go":
            await say("▶️ Отлично, план утверждён — приступаю к работе.")
            await _run_team(
                update,
                context,
                "Да, план утверждён — приступай к выполнению (не предлагай план заново).",
                ack=None,  # the line above already acknowledged
                plan_approved=True,
            )
        elif action == "edit":
            await say("✏️ Напишите, что изменить в плане — переработаю его.")
        elif action == "cancel":
            await say("✖️ Отменено. Напишите новую задачу, когда будете готовы.")
    except Exception:  # noqa: BLE001
        logger.exception("plan callback failed for action=%s", action)
        try:
            await say("⚠️ Не получилось запустить выполнение плана. Попробуйте ещё раз или напишите задачу заново.")
        except Exception:  # noqa: BLE001
            pass


async def _run_specialist(update: Update, role: str, question: str) -> None:
    registry.reload()
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        answer = await arun_specialist(role, question)
        if settings.translate_chatter:
            answer = await atranslate_ru(answer)
    except Exception:  # noqa: BLE001
        logger.exception("specialist run failed")
        await _reply(update, "⚠️ Something went wrong while reaching the specialist.")
        return
    label = registry.label(role)
    await _reply(update, f"*{label}*\n\n{answer}")


# --- error handling ---------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors gracefully. Network/timeout blips on the long-poll connection
    are transient — python-telegram-bot reconnects automatically — so we note
    them in one line instead of dumping a full traceback."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient Telegram network issue (auto-retrying): %s", err)
        return
    logger.error("Unhandled error while processing an update", exc_info=err)


# --- application factory ----------------------------------------------------

def build_application() -> Application:
    # concurrent_updates: process updates concurrently so the bot stays
    # responsive (commands, other chats) while a long team run is in progress,
    # instead of blocking until it finishes. Same-chat team runs are still
    # serialized by a per-chat lock in _run_team.
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("roles", roles_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("team", team_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CallbackQueryHandler(on_plan_callback, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_cmd_callback, pattern=r"^cmd:"))
    app.add_handler(CallbackQueryHandler(on_appr_callback, pattern=r"^appr:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app
