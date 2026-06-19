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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
proactive = None  # ProactiveService, created at startup
routine_scheduler = None  # RoutineScheduler, created at startup


async def _routine_runner(routine: dict) -> str:
    """Execute a routine's prompt: wake the whole team, or one agent."""
    from src.graph.team_graph import aagent_reply, arun_team

    prompt = routine.get("prompt") or ""
    target = routine.get("target") or "team"
    if target == "team":
        answer, _kind, _did = await arun_team(prompt, thread_id=f"routine-{routine.get('id')}")
        return answer
    if registry.is_specialist(target):
        return await aagent_reply(target, prompt)
    return f"[рутина: неизвестная цель '{target}']"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global proactive, routine_scheduler
    # Create tables, seed the default roles on first run, load the cache.
    registry.setup()
    # Materialize each agent's folder (agents/<slug>/) so skills can live there,
    # then discover + register the skills found in those folders.
    from src.agent_fs import scaffold_all
    from src.proactive import ProactiveService
    from src.routines import RoutineScheduler
    from src.skills import skill_loader

    scaffold_all()
    skill_loader.discover()
    await manager.start()
    # Agents post to the team chat on events (guardrailed; off unless configured).
    proactive = ProactiveService(manager.post_to_team)
    await proactive.start()
    # Recurring jobs that wake the team/agents on a schedule (off unless enabled).
    routine_scheduler = RoutineScheduler(_routine_runner, manager.post_to_team)
    await routine_scheduler.start()
    try:
        yield
    finally:
        if routine_scheduler is not None:
            await routine_scheduler.stop()
        if proactive is not None:
            await proactive.stop()
        await manager.stop()


app = FastAPI(title="AI IT Company — Admin", lifespan=_lifespan)


# --- schemas ----------------------------------------------------------------

class AgentIn(BaseModel):
    slug: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    system_prompt: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
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


# --- skills (stage 5) -------------------------------------------------------

class SkillLink(BaseModel):
    enabled: Optional[bool] = None


@app.get("/api/skills")
def list_skills() -> list[dict]:
    from src import skill_registry

    return skill_registry.list_skills()


@app.get("/api/skills/catalog")
def skills_catalog(exclude: Optional[str] = None) -> list[dict]:
    from src import skill_registry

    return skill_registry.public_catalog(exclude_owner=exclude)


@app.post("/api/skills/discover")
def discover_skills() -> dict:
    from src.agent_fs import scaffold_all
    from src.skills import skill_loader

    scaffold_all()
    return {"discovered": len(skill_loader.discover())}


@app.get("/api/agents/{slug}/skills")
def agent_skills(slug: str) -> list[dict]:
    from src import skill_registry

    if not registry.get(slug):
        raise HTTPException(404, f"agent '{slug}' not found")
    return skill_registry.agent_skills(slug)


@app.post("/api/agents/{slug}/skills/{skill_id}/adopt", status_code=201)
def adopt_skill(slug: str, skill_id: int) -> dict:
    from src import skill_registry

    try:
        return skill_registry.adopt_skill(slug, skill_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.patch("/api/agents/{slug}/skills/{skill_id}")
def update_agent_skill(slug: str, skill_id: int, payload: SkillLink) -> dict:
    from src import skill_registry

    if payload.enabled is not None:
        try:
            skill_registry.set_enabled(slug, skill_id, payload.enabled)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
    return {"slug": slug, "skill_id": skill_id, "enabled": payload.enabled}


@app.delete("/api/agents/{slug}/skills/{skill_id}", status_code=204)
def drop_skill(slug: str, skill_id: int) -> None:
    from src import skill_registry

    try:
        skill_registry.drop_skill(slug, skill_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# --- board: tasks / events / messages / graph (stage 6) ---------------------

@app.get("/api/tasks")
def list_tasks() -> list[dict]:
    from src import collab

    return collab.list_tasks()


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    from src import collab

    task = collab.get_task(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} not found")
    return task


@app.get("/api/tasks/{task_id}/events")
def task_events(task_id: int) -> list[dict]:
    from src import collab

    return collab.task_timeline(task_id)


@app.get("/api/messages")
def list_messages(limit: int = 50) -> list[dict]:
    from src import collab

    return collab.recent_messages(limit=max(1, min(limit, 500)))


@app.get("/api/graph")
def interaction_graph() -> dict:
    from src import collab

    return collab.interaction_graph()


@app.get("/api/activity")
def activity_feed(category: str = "all", limit: int = 80) -> list[dict]:
    from src import collab

    if category not in ("all", "tasks", "thoughts", "system"):
        category = "all"
    return collab.activity_feed(category, limit=max(1, min(limit, 300)))


@app.get("/api/office")
def office_state() -> dict:
    from src import collab

    return collab.office_state()


@app.get("/api/webapp")
def webapp_config() -> dict:
    """Tells the page whether the Mini App is configured (URL set)."""
    from src.config import settings

    return {"enabled": bool(settings.webapp_url)}


@app.post("/api/tg/auth")
def tg_auth(payload: dict) -> dict:
    """Validate a Telegram Mini App initData payload and authorize the user."""
    from src import tg_auth as auth

    user = auth.authenticate((payload or {}).get("init_data", ""))
    if not user:
        raise HTTPException(403, "Telegram authentication failed")
    return {"ok": True, "user": {"id": user.get("id"),
                                 "name": user.get("first_name") or user.get("username") or "user"}}


@app.get("/api/proactive")
def proactive_status() -> dict:
    from src.config import settings

    return {
        "enabled": settings.enable_proactive,
        "muted": proactive.muted if proactive else True,
        "team_chat_id": settings.team_chat_id,
    }


@app.post("/api/proactive/mute")
def proactive_mute(payload: dict) -> dict:
    if proactive is None:
        raise HTTPException(503, "proactive service not running")
    if payload.get("muted"):
        proactive.mute()
    else:
        proactive.unmute()
    return {"muted": proactive.muted}


# --- costs & budgets --------------------------------------------------------

@app.get("/api/costs")
def costs() -> dict:
    from src import budget

    return budget.cost_summary()


@app.get("/api/budgets")
def list_budgets() -> list[dict]:
    from src import budget

    return budget.list_budgets()


@app.post("/api/budgets")
def upsert_budget(payload: dict) -> dict:
    from src import budget

    return budget.set_budget(payload or {})


@app.delete("/api/budgets/{policy_id}", status_code=204)
def delete_budget(policy_id: int) -> None:
    from src import budget

    if not budget.delete_budget(policy_id):
        raise HTTPException(404, "policy not found")


# --- routines / heartbeats --------------------------------------------------

@app.get("/api/routines")
def list_routines() -> list[dict]:
    from src import routines

    return routines.list_routines()


@app.post("/api/routines", status_code=201)
def create_routine(payload: dict) -> dict:
    from src import routines

    return routines.create_routine(payload or {})


@app.patch("/api/routines/{routine_id}")
def update_routine(routine_id: int, payload: dict) -> dict:
    from src import routines

    row = routines.update_routine(routine_id, payload or {})
    if row is None:
        raise HTTPException(404, "routine not found")
    return row


@app.delete("/api/routines/{routine_id}", status_code=204)
def delete_routine(routine_id: int) -> None:
    from src import routines

    if not routines.delete_routine(routine_id):
        raise HTTPException(404, "routine not found")


@app.post("/api/routines/{routine_id}/run")
def run_routine(routine_id: int) -> dict:
    from src import routines

    if not routines.trigger_now(routine_id):
        raise HTTPException(404, "routine not found")
    return {"ok": True, "note": "запущу на ближайшем тике планировщика"}


# --- approvals (typed, audited) ---------------------------------------------

@app.get("/api/approvals")
def list_approvals(limit: int = 50) -> list[dict]:
    from src import approvals

    return approvals.recent(limit)


@app.get("/api/approvals/pending")
def pending_approvals() -> list[dict]:
    from src import approvals

    return approvals.pending()


@app.post("/api/approvals/{approval_id}/decide")
async def decide_approval(approval_id: int, payload: dict) -> dict:
    """Approve or deny a pending approval from the dashboard (races Telegram)."""
    from src import approvals

    approvals.decide(approval_id, bool((payload or {}).get("approved")), reason="web")
    return {"ok": True}


@app.get("/api/activity/system")
def system_activity(limit: int = 80) -> list[dict]:
    """The raw audit/activity log (control-plane events) for the dashboard."""
    from src import activity

    return activity.recent(limit)


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Stream task events to the admin board as they happen."""
    from src.events import hub

    await ws.accept()
    queue = hub.subscribe()
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never let one socket crash the server
        pass
    finally:
        hub.unsubscribe(queue)


# --- static page ------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
