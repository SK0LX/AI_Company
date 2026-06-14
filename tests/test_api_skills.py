"""Integration tests for the skills API (FastAPI TestClient). No network.

    python tests/test_api_skills.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client import app_client


def main() -> None:
    with app_client() as c:
        # discovery registers the example skill
        disc = c.post("/api/skills/discover").json()
        assert disc["discovered"] >= 1

        skills = c.get("/api/skills").json()
        jf = next((s for s in skills if s["name"] == "json_format"), None)
        assert jf and jf["owner"] == "developer" and jf["is_public"]
        sid = jf["id"]

        # public catalog excludes the tester's own (it owns none) -> still lists json_format
        catalog = c.get("/api/skills/catalog", params={"exclude": "tester"}).json()
        assert any(s["id"] == sid for s in catalog)

        # owner already has it
        dev_skills = c.get("/api/agents/developer/skills").json()
        assert any(s["id"] == sid and s["owned"] for s in dev_skills)

        # adopt to tester
        r = c.post(f"/api/agents/tester/skills/{sid}/adopt")
        assert r.status_code == 201, r.text
        tester_skills = c.get("/api/agents/tester/skills").json()
        adopted = next(s for s in tester_skills if s["id"] == sid)
        assert adopted["adopted_from"] == "developer" and not adopted["owned"]

        # disable then drop
        assert c.patch(f"/api/agents/tester/skills/{sid}", json={"enabled": False}).status_code == 200
        assert not next(s for s in c.get("/api/agents/tester/skills").json() if s["id"] == sid)["enabled"]
        assert c.delete(f"/api/agents/tester/skills/{sid}").status_code == 204
        assert all(s["id"] != sid for s in c.get("/api/agents/tester/skills").json())

        # dropping an owned skill -> 409
        assert c.delete(f"/api/agents/developer/skills/{sid}").status_code == 409
        # unknown agent -> 404
        assert c.get("/api/agents/nope_zzz/skills").status_code == 404

    print("api skills tests: OK")


if __name__ == "__main__":
    main()
