"""Telegram Mini App auth — validate WebApp ``initData`` (v2 packaging).

When the dashboard runs inside Telegram as a Mini App, the client sends a signed
``initData`` string. We verify the HMAC with the bot token (per Telegram's spec)
so a forged request can't reach the dashboard, and optionally restrict access to
an allow-list of user IDs.

Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from src.config import settings

logger = logging.getLogger(__name__)

# Reject initData older than this. A Mini App launch uses its initData right away,
# so a day-long window is safe while still capping replay of a captured payload.
_MAX_AGE_SECONDS = 24 * 3600


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Return the parsed fields (incl. ``user`` dict) if ``init_data`` is a valid,
    untampered Telegram WebApp payload signed by ``bot_token``; else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:  # noqa: BLE001
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    # Anti-replay: a valid-but-stale captured payload must not authenticate forever.
    try:
        if abs(time.time() - int(pairs.get("auth_date", 0))) > _MAX_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except Exception:  # noqa: BLE001
            pass
    return pairs


def is_allowed(user_id: Optional[int]) -> bool:
    """Whether ``user_id`` may use the Mini App. Empty allow-list = anyone with a
    valid signature."""
    allow = [s.strip() for s in (settings.webapp_allowed_user_ids or "").split(",") if s.strip()]
    if not allow:
        return True
    return str(user_id) in allow


def authenticate(init_data: str) -> Optional[dict]:
    """Validate + authorize in one step. Returns the user dict on success, else None."""
    fields = validate_init_data(init_data, settings.telegram_bot_token)
    if not fields:
        return None
    user = fields.get("user") or {}
    if not is_allowed(user.get("id")):
        return None
    return user
