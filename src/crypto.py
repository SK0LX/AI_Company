"""Symmetric encryption for secrets at rest (per-agent Telegram tokens).

The key comes from ``settings.app_secret`` if set, otherwise a stable key is
generated once in ``data/secret.key`` (gitignored). Any string works as
``app_secret`` — it is normalized into a valid Fernet key. ``decrypt`` tolerates
legacy plaintext values so older rows keep working.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from src.config import settings

_KEY_FILE = os.path.join(os.path.dirname(settings.db_path) or "data", "secret.key")


def _normalize(raw: bytes) -> bytes:
    """Return a valid 32-byte url-safe base64 Fernet key from arbitrary input."""
    try:
        Fernet(raw)
        return raw
    except Exception:  # noqa: BLE001
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _load_key() -> bytes:
    if settings.app_secret:
        return _normalize(settings.app_secret.encode())
    os.makedirs(os.path.dirname(_KEY_FILE) or "data", exist_ok=True)
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    # The root secret for all per-agent tokens — never world-readable. Create with
    # 0600 exclusively (O_EXCL avoids a TOCTOU race on the just-checked path).
    fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


_fernet = Fernet(_load_key())


def encrypt(text: str) -> str:
    if not text:
        return ""
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return token  # legacy plaintext value — return as-is
