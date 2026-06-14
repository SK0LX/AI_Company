"""Database engine + session helpers for the v2 platform.

Synchronous SQLModel is fine here: the registry reads agents into an in-memory
cache at startup (and on admin changes), so the async request path never touches
the DB directly. The file lives next to the existing conversation memory db.
"""
from __future__ import annotations

import logging
import os

from sqlmodel import Session, SQLModel, create_engine

from src.config import settings

logger = logging.getLogger(__name__)

# Reuse the data/ directory of the conversation memory db.
_DB_DIR = os.path.dirname(settings.db_path) or "data"
DB_PATH = os.path.join(_DB_DIR, "app.sqlite")
os.makedirs(_DB_DIR, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)


def init_db() -> None:
    """Create any missing tables + columns. Safe to call repeatedly.

    A lightweight stand-in for migrations: ``create_all`` adds new tables but not
    new columns on existing ones, so we ALTER in any column the models declare
    that the table is missing (SQLite needs a default for NOT NULL columns)."""
    # Importing models registers them on SQLModel.metadata.
    from src.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _ensure_columns()


def _ensure_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
                if not col.nullable:
                    try:
                        pt = col.type.python_type
                    except Exception:  # noqa: BLE001
                        pt = str
                    if pt is bool or pt is int or pt is float:
                        ddl += " NOT NULL DEFAULT 0"
                    else:
                        ddl += " NOT NULL DEFAULT ''"
                conn.execute(text(ddl))
                logger.info("migrated: added column %s.%s", table.name, col.name)


def get_session() -> Session:
    return Session(engine)
