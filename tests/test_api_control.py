"""Integration tests for the control-plane API: costs, budgets, routines,
approvals, system activity. Boots the app via TestClient. No network.

    python tests/test_api_control.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client


def main() -> None:
    with app_client() as c:
        # --- costs summary -------------------------------------------------
        costs = c.get("/api/costs").json()
        assert {"total_usd", "today_usd", "by_agent", "by_model", "budgets"} <= set(costs)

        # --- budgets CRUD --------------------------------------------------
        c.post("/api/budgets", json={"scope": "_ut_api", "window": "day",
                                     "limit_usd": 2.5, "hard_stop": True})
        rows = c.get("/api/budgets").json()
        mine = [x for x in rows if x["scope"] == "_ut_api" and x["window"] == "day"]
        assert mine and mine[0]["limit_usd"] == 2.5 and "spent_usd" in mine[0]
        assert c.delete(f"/api/budgets/{mine[0]['id']}").status_code == 204
        assert c.delete("/api/budgets/99999999").status_code == 404

        # --- routines CRUD + run ------------------------------------------
        r = c.post("/api/routines", json={
            "name": "_ut_apir", "schedule_kind": "interval", "schedule_value": "3600",
            "target": "team", "prompt": "ping",
        }).json()
        rid = r["id"]
        assert r["next_run_at"] is not None
        assert any(x["id"] == rid for x in c.get("/api/routines").json())
        assert c.patch(f"/api/routines/{rid}", json={"enabled": False}).json()["enabled"] is False
        assert c.post(f"/api/routines/{rid}/run").json()["ok"] is True
        assert c.patch("/api/routines/99999999", json={}).status_code == 404
        assert c.delete(f"/api/routines/{rid}").status_code == 204
        assert c.delete("/api/routines/99999999").status_code == 404

        # --- approvals + system activity ----------------------------------
        assert isinstance(c.get("/api/approvals", params={"limit": 10}).json(), list)
        assert isinstance(c.get("/api/activity/system").json(), list)

    print("api control tests: OK")


if __name__ == "__main__":
    main()
