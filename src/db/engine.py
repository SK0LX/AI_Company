"""Database engine + session helpers for the v2 platform.

Synchronous SQLModel is fine here: the registry reads agents into an in-memory
cache at startup (and on admin changes), so the async request path never touches
the DB directly. The file lives next to the existing conversation memory db.
"""
from __future__ import annotations

import os

from sqlmodel import Session, SQLModel, create_engine

from src.config import settings

# Reuse the data/ directory of the conversation memory db.
_DB_DIR = os.path.dirname(settings.db_path) or "data"
DB_PATH = os.path.join(_DB_DIR, "app.sqlite")
os.makedirs(_DB_DIR, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)


def init_db() -> None:
    """Create any missing tables. Safe to call repeatedly."""
    # Importing models registers them on SQLModel.metadata.
    from src.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
