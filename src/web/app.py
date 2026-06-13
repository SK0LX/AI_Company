"""Admin panel backend (v2, stage 2).

A small FastAPI app that exposes CRUD over the agent registry and serves a
single static HTML page. Runs separately from the polling bot for now (both share
``data/app.sqlite``). Bind to 127.0.0.1 only — no auth yet (added before the
panel is ever exposed).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.registry import registry

app = FastAPI(title="AI IT Company — Admin")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
def _startup() -> None:
    # Create tables, seed the default roles on first run, load the cache.
    registry.setup()


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
def create_agent(payload: AgentIn) -> dict:
    if not payload.slug:
        raise HTTPException(422, "slug is required")
    try:
        agent = registry.create_agent(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return registry.as_dict(agent.slug)


@app.patch("/api/agents/{slug}")
def update_agent(slug: str, payload: AgentIn) -> dict:
    try:
        registry.update_agent(slug, payload.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, f"agent '{slug}' not found")
    return registry.as_dict(slug)


@app.delete("/api/agents/{slug}", status_code=204)
def delete_agent(slug: str) -> None:
    try:
        registry.delete_agent(slug)
    except KeyError:
        raise HTTPException(404, f"agent '{slug}' not found")


# --- static page ------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
