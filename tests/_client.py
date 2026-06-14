"""Shared test helper: a FastAPI TestClient that boots the app WITHOUT starting
the real Telegram bots (no network). The lifespan still seeds the registry,
scaffolds agent folders, discovers skills, and starts the proactive service —
all local and safe.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@contextmanager
def app_client():
    from fastapi.testclient import TestClient

    from src.web import app as appmod

    async def _noop(*_a, **_k):
        return None

    # Don't bring up Telegram polling in tests.
    appmod.manager.start = _noop  # type: ignore[assignment]
    appmod.manager.stop = _noop  # type: ignore[assignment]

    with TestClient(appmod.app) as client:
        yield client
