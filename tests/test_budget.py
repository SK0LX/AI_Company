"""Unit tests for cost metering + budget hard-stops. No network.

    python tests/test_budget.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import budget
from src.config import settings
from src.registry import registry

AGENT = "_ut_budget"


def _prices() -> None:
    assert budget.price_for("claude-sonnet-4-6") == (3.0, 15.0)
    assert budget.price_for("gemini-2.5-flash-lite") == (0.10, 0.40)  # longest match wins
    assert budget.price_for("openai/gpt-oss-120b:free") == (0.0, 0.0)
    assert budget.price_for("totally-unknown-model") == (0.0, 0.0)


def _usage_extraction() -> None:
    msg = types.SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 50})
    gen = types.SimpleNamespace(message=msg)
    resp = types.SimpleNamespace(generations=[[gen]], llm_output=None)
    assert budget._usage_from_response(resp) == (100, 50)
    # llm_output fallback when no usage_metadata
    resp2 = types.SimpleNamespace(
        generations=[], llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    )
    assert budget._usage_from_response(resp2) == (7, 3)


def _record_and_spend() -> None:
    base = budget.spent(AGENT, "lifetime")
    # 1M input tokens of a $3/1M-in model = $3.00 exactly.
    cost = budget.record_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 0, agent=AGENT)
    assert abs(cost - 3.0) < 1e-9, cost
    after = budget.spent(AGENT, "lifetime")
    assert abs((after - base) - 3.0) < 1e-9, (base, after)


def _callback_records() -> None:
    base = budget.spent(AGENT, "lifetime")
    cb = budget.make_cost_callback("anthropic", "claude-sonnet-4-6")
    budget.set_cost_agent(AGENT)
    msg = types.SimpleNamespace(usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0})
    resp = types.SimpleNamespace(generations=[[types.SimpleNamespace(message=msg)]], llm_output=None)
    cb.on_llm_end(resp)
    budget.set_cost_agent("system")
    assert budget.spent(AGENT, "lifetime") - base >= 3.0 - 1e-9


def _gate_and_block() -> None:
    settings.enable_budget = True
    # Isolation: gate() aggregates global + agent policies, so temporarily disable
    # any OTHER enabled policies (e.g. a real global budget) — restored in finally.
    others = [b for b in budget.list_budgets() if b["scope"] != AGENT and b["enabled"]]
    for b in others:
        budget.set_budget({"scope": b["scope"], "window": b["window"], "enabled": False})
    try:
        spent_now = budget.spent(AGENT, "lifetime")  # >= 6.0 by now
        # hard-stop below current spend -> blocked
        budget.set_budget({"scope": AGENT, "window": "lifetime", "limit_usd": 2.0,
                           "hard_stop": True, "enabled": True})
        assert budget.gate(AGENT)["status"] == "blocked"
        assert budget.blocked(AGENT) is True
        # raise far above spend -> ok (warn_percent default 80)
        budget.set_budget({"scope": AGENT, "window": "lifetime", "limit_usd": spent_now * 100 + 100})
        assert budget.gate(AGENT)["status"] == "ok"
        assert budget.blocked(AGENT) is False
        # warn band: limit just above spend with a low warn threshold
        budget.set_budget({"scope": AGENT, "window": "lifetime",
                           "limit_usd": spent_now + 1.0, "warn_percent": 1, "hard_stop": False})
        assert budget.gate(AGENT)["status"] == "warn"
        # enforcement is gated by the master switch
        settings.enable_budget = False
        budget.set_budget({"scope": AGENT, "window": "lifetime", "limit_usd": 0.01, "hard_stop": True})
        assert budget.blocked(AGENT) is False  # disabled -> never blocks
        settings.enable_budget = True
    finally:
        for b in others:
            budget.set_budget({"scope": b["scope"], "window": b["window"], "enabled": True})


def _crud_and_summary() -> None:
    budget.set_budget({"scope": AGENT, "window": "day", "limit_usd": 5.0})
    rows = budget.list_budgets()
    mine = [r for r in rows if r["scope"] == AGENT and r["window"] == "day"]
    assert mine and mine[0]["limit_usd"] == 5.0
    assert "spent_usd" in mine[0]
    pid = mine[0]["id"]
    assert budget.delete_budget(pid) is True
    assert budget.delete_budget(pid) is False  # already gone

    summary = budget.cost_summary()
    for key in ("total_usd", "today_usd", "month_usd", "by_agent", "by_model", "recent", "budgets"):
        assert key in summary, key
    assert any(a["name"] == AGENT for a in summary["by_agent"])


def main() -> None:
    registry.setup()  # ensures CostEvent/BudgetPolicy tables exist
    enable0 = settings.enable_budget
    try:
        _prices()
        _usage_extraction()
        _record_and_spend()
        _callback_records()
        _gate_and_block()
        _crud_and_summary()
    finally:
        settings.enable_budget = enable0
        # tidy up everything this test wrote so it never pollutes the cost view:
        # the budget policies AND the synthetic cost events (agent == AGENT).
        for r in budget.list_budgets():
            if r["scope"] == AGENT:
                budget.delete_budget(r["id"])
        try:
            from sqlmodel import select

            from src.db.engine import get_session
            from src.db.models import CostEvent

            with get_session() as session:
                for ev in session.exec(select(CostEvent).where(CostEvent.agent == AGENT)).all():
                    session.delete(ev)
                session.commit()
        except Exception:  # noqa: BLE001 - cleanup must never fail the test
            pass
    print("budget tests: OK")


if __name__ == "__main__":
    main()
