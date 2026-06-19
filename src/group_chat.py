"""Group-chat presence brain (live, multi-human group).

Keeps the per-chat conversation context and the guardrails that make a roomful of
agent bots behave like colleagues instead of a reply-bot avalanche:

* one shared transcript per chat (humans + agents),
* a cheap heuristic gate that decides whether a moment even warrants thinking
  (so we don't spend an LLM call on every "ок"),
* cooldown + duplicate suppression,
* helpers to detect who's addressed and whether real work is being asked.

The actual "who speaks" decision and the reply text are LLM calls that live in
``src.graph.team_graph`` (``aroute_group_speaker`` / ``agroup_reply``); the turn
loop that ties it together lives in ``src.bot.manager``. Loops are impossible by
construction: only HUMAN messages start a turn-burst, and the burst is hard-capped.
"""
from __future__ import annotations

import random
import re
import time
from collections import deque
from typing import Optional

from src.config import settings

# Whole-word English hints + Russian stems that suggest a concrete work request.
# Matched on word boundaries / token stems so "prefix" doesn't read as "fix" and
# Russian first-person forms ("сделаю", "добавлю") still count.
_WORK_HINTS_EN = {
    "code", "build", "create", "write", "implement", "fix", "add", "refactor",
    "run", "make", "generate", "deploy", "test",
    # board / housekeeping verbs
    "clear", "clean", "remove", "delete", "tidy", "empty", "cancel", "close",
}
_WORK_STEMS_RU = (
    "созда", "сдела", "напиш", "напис", "реализ", "добав", "исправ", "почин",
    "поправ", "перепиш", "перепис", "сгенер", "собер", "собир", "запус", "провер",
    "постро", "набро",
)
# Housekeeping / board verbs whose prefix varies (по-/вы-/при-/у-), so we match
# them as a SUBSTRING: "чист" hits почистите/очистить/вычистить, "удал" hits
# удали/поудаляй, "убер"/"убра" hits убери/убрать, "отмен" hits отмени, etc.
_HOUSEKEEP_SUB_RU = (
    "чист", "удал", "убер", "убра", "отмен", "закро", "разгреб", "разбер", "снеси",
)

# Greetings — worth a friendly one-line reply from a single teammate.
_GREETING_WORDS = {
    "привет", "приветик", "приветствую", "здаров", "здарова", "здорово", "хай",
    "хеллоу", "дратути", "ку", "йо", "салют", "hello", "hi", "hey", "yo", "hiya",
}
_GREETING_PHRASES = ("всем привет", "доброе утро", "добрый день", "добрый вечер",
                     "good morning", "good evening", "what's up", "whats up")


def is_greeting(text: str) -> bool:
    low = (text or "").lower().strip()
    if any(p in low for p in _GREETING_PHRASES):
        return True
    tokens = re.findall(r"[a-zа-яё]+", low)
    return bool(tokens) and any(t in _GREETING_WORDS for t in tokens)


# Question / request words — so a natural question without a "?" still counts.
_QUESTION_WORDS = {
    "что", "чего", "чём", "чем", "как", "какой", "какая", "какие", "каком", "каков",
    "почему", "зачем", "где", "когда", "куда", "откуда", "кто", "кого", "кому",
    "сколько", "разве", "неужели", "what", "how", "why", "who", "whom", "where",
    "when", "which", "whose",
}
_ASK_STEMS = ("расскаж", "раскаж", "скаж", "покаж", "объясн", "подскаж", "перечисл",
              "опиш", "tell", "explain", "show", "describe", "list")
# "to the whole room" words — these invite several teammates to chime in.
_EVERYONE_WORDS = {"все", "всех", "всем", "вы", "каждый", "каждого", "ребят", "ребята",
                   "народ", "коллеги", "everyone", "guys", "all"}


def is_question(text: str) -> bool:
    low = (text or "").lower()
    if "?" in low:
        return True
    tokens = re.findall(r"[a-zа-яё]+", low)
    if any(t in _QUESTION_WORDS for t in tokens):
        return True
    return any(tok.startswith(stem) for tok in tokens for stem in _ASK_STEMS)


def is_to_everyone(text: str) -> bool:
    tokens = re.findall(r"[a-zа-яё]+", (text or "").lower())
    return any(t in _EVERYONE_WORDS for t in tokens)


class GroupState:
    def __init__(self) -> None:
        self.transcript: deque[tuple[str, Optional[str], str]] = deque(
            maxlen=max(4, settings.group_history_turns)
        )  # (display_name, agent_slug_or_None, text)
        self.last_post: float = 0.0  # monotonic ts of the last AGENT post
        self.last_speaker: Optional[str] = None
        self.recent_hashes: deque[int] = deque(maxlen=12)


_states: dict[int, GroupState] = {}


def state(chat_id: int) -> GroupState:
    st = _states.get(chat_id)
    if st is None:
        st = _states[chat_id] = GroupState()
    return st


def observe(chat_id: int, *, name: str, text: str, slug: Optional[str] = None) -> None:
    """Record a message (human or agent) into the chat transcript."""
    state(chat_id).transcript.append((name or "?", slug, text or ""))


def transcript_text(chat_id: int) -> str:
    return "\n".join(f"{name}: {text}" for name, _slug, text in state(chat_id).transcript)


def last_speaker(chat_id: int) -> Optional[str]:
    return state(chat_id).last_speaker


def cooldown_ok(chat_id: int, *, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    return now - state(chat_id).last_post >= settings.group_cooldown


def mark_post(chat_id: int, slug: str, label: str, text: str, *, now: Optional[float] = None) -> None:
    """Record an agent's own post: update cooldown/last-speaker, transcript, dedup."""
    st = state(chat_id)
    st.last_post = time.monotonic() if now is None else now
    st.last_speaker = slug
    st.transcript.append((label, slug, text))
    st.recent_hashes.append(_norm_hash(text))


def is_duplicate(chat_id: int, text: str) -> bool:
    return _norm_hash(text) in state(chat_id).recent_hashes


def _norm_hash(text: str) -> int:
    return hash(re.sub(r"\s+", " ", (text or "").strip().lower())[:200])


def work_intent(text: str) -> bool:
    """Whether the message asks for concrete work (so a permitted agent may use
    its tools). Whole-word English, stem-prefix Russian, plus substring matching
    for housekeeping verbs whose prefix varies (почистите / удалите / убери)."""
    tokens = re.findall(r"[a-zа-яё]+", (text or "").lower())
    if any(t in _WORK_HINTS_EN for t in tokens):
        return True
    if any(tok.startswith(stem) for tok in tokens for stem in _WORK_STEMS_RU):
        return True
    return any(sub in tok for tok in tokens for sub in _HOUSEKEEP_SUB_RU)


def detect_addressed(text: str, roster: list[tuple[str, str, str]]) -> Optional[str]:
    """If a teammate is named/addressed in ``text``, return their slug. Matches the
    slug, role word, or a display-name token — on WORD BOUNDARIES, so 'protesters'
    doesn't read as 'tester'."""
    low = (text or "").lower()
    for slug, name, role in roster:
        needles = {slug.lower(), (role or "").lower()}
        needles |= {w for w in re.split(r"[\s/_-]+", (name or "").lower()) if len(w) >= 4}
        for n in needles:
            if n and re.search(rf"\b{re.escape(n)}\b", low):
                return slug
    return None


def should_consider(
    chat_id: int, text: str, *, addressed: bool, is_followup: bool,
    rng: Optional[random.Random] = None,
) -> bool:
    """Cheap gate: is this moment worth an LLM 'who speaks?' call at all?

    Follow-up turns (agent↔agent within one burst) are paced by the inter-turn
    sleep and the hard turn cap, NOT the post cooldown — otherwise the second turn
    would always be killed by the cooldown and the colleague banter never happens."""
    if not settings.enable_group_chat:
        return False
    r = (rng or random).random()
    if is_followup:
        return r < 0.5  # mild chance to continue the thread (bounded by the turn cap)
    if not cooldown_ok(chat_id):
        return False
    if addressed or is_question(text) or work_intent(text) or is_greeting(text):
        return True
    return r < settings.group_ambient_prob  # ambient chatter: usually stay out
