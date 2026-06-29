"""Cost tracking + budget hard-stops.

Every model call is metered into an append-only ``CostEvent`` ledger and priced
from a small per-model table. ``BudgetPolicy`` rows cap spend per scope (global or
a single agent) over a window (day/month/lifetime); when a hard-stop policy is
exceeded, :func:`gate` reports ``blocked`` and the orchestrator refuses further
work for that scope until the window resets or the limit is raised.

Design notes:
- Recording is ALWAYS on (cheap, useful). Enforcement is gated by
  ``settings.enable_budget`` at the call sites, so cost shows up even when budgets
  are off.
- Spend is attributed via context vars (:func:`set_cost_agent` / :func:`set_cost_task`)
  that the orchestrator sets around model calls, because the LangChain model
  objects are cached and shared across agents.
- Prices are best-effort and easy to edit; unknown or ``:free`` models cost 0, so
  we never invent phantom spend.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from sqlmodel import select

from src.config import settings
from src.db.engine import get_session
from src.db.models import BudgetPolicy, CostEvent

logger = logging.getLogger(__name__)

# Price per 1M tokens, (input, output), in USD. Matched case-insensitively by
# substring (longest match wins). Anything unmatched or ``:free`` costs 0.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-flash": (0.30, 2.50),
    "claude-haiku-4": (1.0, 5.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-fable-5": (3.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
}


def price_for(model: str) -> tuple[float, float]:
    """(input, output) $/1M tokens for ``model``. Free/unknown models → (0, 0)."""
    m = (model or "").strip().lower()
    if not m or m.endswith(":free"):
        return (0.0, 0.0)
    best: tuple[int, tuple[float, float]] = (0, (0.0, 0.0))
    for key, price in MODEL_PRICES.items():
        if key in m and len(key) > best[0]:
            best = (len(key), price)
    return best[1]


# --- attribution context ----------------------------------------------------

_cost_agent: contextvars.ContextVar[str] = contextvars.ContextVar("cost_agent", default="system")
_cost_task: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("cost_task", default=None)


def set_cost_agent(slug: Optional[str]) -> None:
    _cost_agent.set(slug or "system")


def set_cost_task(task_id: Optional[int]) -> None:
    _cost_task.set(task_id)


# --- usage extraction + recording -------------------------------------------

def _usage_from_response(response: Any) -> tuple[int, int]:
    """Best-effort (input_tokens, output_tokens) from a LangChain LLMResult."""
    inp = out = 0
    try:  # 1) standardized usage_metadata on the AIMessage(s)
        for gens in getattr(response, "generations", []) or []:
            for g in gens:
                msg = getattr(g, "message", None)
                um = getattr(msg, "usage_metadata", None) if msg is not None else None
                if um:
                    inp += int(um.get("input_tokens", 0) or 0)
                    out += int(um.get("output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    if inp or out:
        return inp, out
    try:  # 2) provider llm_output token usage
        lo = getattr(response, "llm_output", None) or {}
        tu = lo.get("token_usage") or lo.get("usage") or {}
        inp = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
        out = int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
    except Exception:  # noqa: BLE001
        pass
    return inp, out


def record_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int,
    *, agent: Optional[str] = None, task_id: Optional[int] = None,
) -> float:
    """Price the usage, append a CostEvent, and return the cost in USD."""
    pin, pout = price_for(model)
    cost = input_tokens / 1_000_000 * pin + output_tokens / 1_000_000 * pout
    agent = agent or _cost_agent.get()
    task_id = task_id if task_id is not None else _cost_task.get()
    try:
        with get_session() as session:
            session.add(CostEvent(
                agent=agent, provider=provider, model=model,
                input_tokens=int(input_tokens), output_tokens=int(output_tokens),
                cost_usd=float(cost), task_id=task_id,
            ))
            session.commit()
    except Exception:  # noqa: BLE001 - metering must never break a model call
        logger.exception("failed to record cost event")
        return cost
    try:
        from src import activity

        activity.log(agent, "cost", model,
                     usd=round(cost, 6), in_tok=input_tokens, out_tok=output_tokens)
    except Exception:  # noqa: BLE001
        pass
    return cost


def record_usd(
    model: str, cost_usd: float, *,
    provider: str = "claude_cli", agent: Optional[str] = None,
    task_id: Optional[int] = None,
) -> float:
    """Append a CostEvent for a cost already known in USD.

    The token-based :func:`record_cost` prices usage itself, but the Claude Code
    CLI (`claude -p`) reports ``total_cost_usd`` directly rather than tokens — so
    the Claude engine meters its spend through here. Tokens are stored as 0.
    Best-effort: metering must never break a run."""
    cost = float(cost_usd or 0.0)
    agent = agent or _cost_agent.get()
    task_id = task_id if task_id is not None else _cost_task.get()
    try:
        with get_session() as session:
            session.add(CostEvent(
                agent=agent, provider=provider, model=model,
                input_tokens=0, output_tokens=0,
                cost_usd=cost, task_id=task_id,
            ))
            session.commit()
    except Exception:  # noqa: BLE001 - metering must never break a model call
        logger.exception("failed to record usd cost event")
        return cost
    try:
        from src import activity

        activity.log(agent, "cost", model, usd=round(cost, 6), in_tok=0, out_tok=0)
    except Exception:  # noqa: BLE001
        pass
    return cost


class CostCallback(BaseCallbackHandler):
    """Meters one model's calls. Bound to a (provider, model) at build time;
    the agent/task come from context vars at call time."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        inp, out = _usage_from_response(response)
        if inp or out:
            record_cost(self.provider, self.model, inp, out)


def make_cost_callback(provider: str, model: str) -> CostCallback:
    return CostCallback(provider, model)


# --- spend windows + budget gate --------------------------------------------

def _window_start(window: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None  # lifetime


def spent(scope: str, window: str) -> float:
    """Total USD spent for ``scope`` ('global' = everything) in the window."""
    start = _window_start(window)
    with get_session() as session:
        stmt = select(CostEvent)
        rows = session.exec(stmt).all()
    total = 0.0
    for r in rows:
        if scope != "global" and r.agent != scope:
            continue
        if start is not None:
            ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
            if ts < start:
                continue
        total += r.cost_usd or 0.0
    return total


def _policies() -> list[BudgetPolicy]:
    with get_session() as session:
        return list(session.exec(select(BudgetPolicy).where(BudgetPolicy.enabled)).all())


def gate(agent: Optional[str] = None) -> dict:
    """Evaluate the budget for ``agent`` (and the global scope). Returns
    {status: ok|warn|blocked, spent, limit, scope, window}. ``status`` is the
    worst across applicable policies. Never raises."""
    result = {"status": "ok", "spent": 0.0, "limit": 0.0, "scope": "", "window": ""}
    try:
        applicable = [p for p in _policies() if p.scope == "global" or p.scope == agent]
        rank = {"ok": 0, "warn": 1, "blocked": 2}
        for p in applicable:
            s = spent(p.scope, p.window)
            if p.hard_stop and p.limit_usd > 0 and s >= p.limit_usd:
                status = "blocked"
            elif p.limit_usd > 0 and s >= p.limit_usd * (p.warn_percent / 100.0):
                status = "warn"
            else:
                status = "ok"
            if rank[status] > rank[result["status"]]:
                result = {"status": status, "spent": round(s, 6),
                          "limit": p.limit_usd, "scope": p.scope, "window": p.window}
    except Exception:  # noqa: BLE001 - a budget hiccup must never strand work
        logger.exception("budget gate failed")
        return {"status": "ok", "spent": 0.0, "limit": 0.0, "scope": "", "window": ""}
    return result


def blocked(agent: Optional[str] = None) -> bool:
    """True only when budgets are enabled AND a hard-stop policy is exceeded."""
    return settings.enable_budget and gate(agent)["status"] == "blocked"


# --- reporting (admin API) --------------------------------------------------

def cost_summary(limit: int = 20) -> dict:
    with get_session() as session:
        rows = list(session.exec(select(CostEvent).order_by(CostEvent.id.desc())).all())
    day_start = _window_start("day")
    month_start = _window_start("month")
    total = today = month = 0.0
    by_agent: dict[str, float] = {}
    by_model: dict[str, float] = {}
    for r in rows:
        c = r.cost_usd or 0.0
        total += c
        ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
        if day_start and ts >= day_start:
            today += c
        if month_start and ts >= month_start:
            month += c
        by_agent[r.agent] = by_agent.get(r.agent, 0.0) + c
        by_model[r.model or "?"] = by_model.get(r.model or "?", 0.0) + c
    recent = [
        {"ts": r.ts.isoformat(), "agent": r.agent, "model": r.model,
         "in": r.input_tokens, "out": r.output_tokens, "usd": round(r.cost_usd or 0.0, 6)}
        for r in rows[:limit]
    ]
    top = lambda d: [{"name": k, "usd": round(v, 6)} for k, v in  # noqa: E731
                     sorted(d.items(), key=lambda kv: kv[1], reverse=True)]
    return {
        "total_usd": round(total, 6), "today_usd": round(today, 6),
        "month_usd": round(month, 6), "events": len(rows),
        "by_agent": top(by_agent), "by_model": top(by_model), "recent": recent,
        "budgets": list_budgets(),
    }


def list_budgets() -> list[dict]:
    with get_session() as session:
        rows = list(session.exec(select(BudgetPolicy)).all())
    out = []
    for p in rows:
        out.append({
            "id": p.id, "scope": p.scope, "limit_usd": p.limit_usd, "window": p.window,
            "warn_percent": p.warn_percent, "hard_stop": p.hard_stop, "enabled": p.enabled,
            "spent_usd": round(spent(p.scope, p.window), 6),
        })
    return out


def set_budget(data: dict) -> dict:
    """Upsert a budget policy. Keyed by (scope, window). Returns the saved row."""
    scope = (data.get("scope") or "global").strip()
    window = data.get("window") or "day"
    if window not in ("day", "month", "lifetime"):
        window = "day"
    with get_session() as session:
        row = session.exec(
            select(BudgetPolicy).where(BudgetPolicy.scope == scope, BudgetPolicy.window == window)
        ).first()
        if not row:
            row = BudgetPolicy(scope=scope, window=window)
        if "limit_usd" in data:
            row.limit_usd = float(data["limit_usd"])
        if "warn_percent" in data:
            row.warn_percent = int(data["warn_percent"])
        if "hard_stop" in data:
            row.hard_stop = bool(data["hard_stop"])
        if "enabled" in data:
            row.enabled = bool(data["enabled"])
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        rid = row.id
    return {"id": rid, "scope": scope, "window": window}


def delete_budget(policy_id: int) -> bool:
    with get_session() as session:
        row = session.get(BudgetPolicy, policy_id)
        if not row:
            return False
        session.delete(row)
        session.commit()
    return True
