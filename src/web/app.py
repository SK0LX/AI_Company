"""Admin panel backend + bot host (v2, unified process).

A small FastAPI app that exposes CRUD over the agent registry and serves a
single static HTML page. It also OWNS the Telegram bots: on startup it brings up
the team bot plus every agent's personal bot inside this same event loop (see
``TelegramManager``), and shuts them down on exit. One process, one DB
(``data/app.sqlite``). Bind to 127.0.0.1 only — no auth yet (added before the
panel is ever exposed).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.bot.manager import TelegramManager
from src.registry import registry

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# The bots run inside this app's event loop. The admin write endpoints below ask
# it to reconcile immediately so a token change starts/stops a bot at once.
manager = TelegramManager()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Create tables, seed the default roles on first run, load the cache.
    registry.setup()
    # Materialize each agent's folder (agents/<slug>/) so skills can live there,
    # then discover + register the skills found in those folders.
    from src.agent_fs import scaffold_all
    from src.skills import skill_loader

    scaffold_all()
    skill_loader.discover()
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="AI IT Company — Admin", lifespan=_lifespan)


# --- schemas ----------------------------------------------------------------

class AgentIn(BaseModel):
    slug: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_username: Optional[str] = None
    enabled: Optional[bool] = None
    permissions: Optional[dict[str, str]] = None
    obligation: Optional[str] = None


# --- API --------------------------------------------------------------------

@app.get("/api/agents")
def list_agents() -> list[dict]:
    return [registry.as_dict(a.slug) for a in registry.list_agents()]


@app.get("/api/agents/{slug}")
def get_agent(slug: str) -> dict:
    data = registry.as_dict(slug)
    if not data:
        raise HTTPException(404, f"agent '{slug}' not found")
    return data


@app.post("/api/agents", status_code=201)
async def create_agent(payload: AgentIn) -> dict:
    if not payload.slug:
        raise HTTPException(422, "slug is required")
    try:
        agent = registry.create_agent(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await manager.reconcile_now()  # start its personal bot at once if it has a token
    return registry.as_dict(agent.slug)


@app.patch("/api/agents/{slug}")
async def update_agent(slug: str, payload: AgentIn) -> dict:
    try:
        registry.update_agent(slug, payload.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, f"agent '{slug}' not found")
    await manager.reconcile_now()  # apply token/enabled changes to its bot at once
    return registry.as_dict(slug)


@app.delete("/api/agents/{slug}", status_code=204)
async def delete_agent(slug: str) -> None:
    try:
        registry.delete_agent(slug)
    except KeyError:
        raise HTTPException(404, f"agent '{slug}' not found")
    await manager.reconcile_now()  # stop its personal bot at once


# --- static page ------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
