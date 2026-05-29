"""Lightweight daily quota tracking for the OpenRouter free tier.

OpenRouter's free models share ONE per-account daily request cap (50/day on
accounts with < $10 balance, 1000/day once $10 is added). That remaining count
is not exposed by the key endpoint, so we estimate it by counting the LLM calls
this bot makes per UTC day (the cap resets at 00:00 UTC). The count is persisted
to a small JSON file so it survives restarts within the same day.

This is an ESTIMATE (it doesn't see usage from outside this bot), good enough to
warn the user before they hit the wall and to explain the wall when they do.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from src.config import settings

_PATH = os.path.join(os.path.dirname(settings.db_path) or ".", "quota.json")
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if data.get("date") != _today():
        data = {"date": _today(), "count": 0}  # new UTC day -> reset
    return data


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PATH) or ".", exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def record_call(n: int = 1) -> None:
    """Count ``n`` model requests against today's quota."""
    with _lock:
        data = _load()
        data["count"] = int(data.get("count", 0)) + n
        _save(data)


def calls_today() -> int:
    with _lock:
        return int(_load().get("count", 0))


def remaining() -> int:
    """Estimated free requests left today (never negative)."""
    return max(0, settings.free_daily_limit - calls_today())


def tracked() -> bool:
    """Only the OpenRouter free tier has this shared daily cap to warn about."""
    return settings.llm_provider == "openrouter"


def is_low() -> bool:
    return tracked() and remaining() <= settings.free_daily_warn_at


def is_exhausted() -> bool:
    return tracked() and remaining() <= 0


def status_line() -> str:
    """Short Russian status for the user."""
    return (
        f"Осталось ~{remaining()} из {settings.free_daily_limit} бесплатных "
        "запросов OpenRouter на сегодня (сброс в 03:00 МСК)."
    )


def low_warning() -> str:
    return (
        f"⚠️ {status_line()} Чтобы поднять лимит до 1000/день — добавьте $10 "
        "на openrouter.ai (free-модели всё равно остаются бесплатными)."
    )


def exhausted_warning() -> str:
    return (
        "🚫 Дневной лимит бесплатных запросов OpenRouter исчерпан "
        f"({settings.free_daily_limit}/день). Сброс в 03:00 МСК. Чтобы поднять "
        "до 1000/день — добавьте $10 на openrouter.ai."
    )


class QuotaCounter(BaseCallbackHandler):
    """Increments the daily counter on every model invocation (chat or text)."""

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        record_call(1)

    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        record_call(1)


# One shared instance attached to every model the team builds.
counter = QuotaCounter()
