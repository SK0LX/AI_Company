"""Unit tests for per-agent persistent sessions + shared team board. No network.

    python tests/test_agent_memory.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_sessions, team_memory


def _sessions() -> None:
    agent_sessions.forget("developer", "p1")
    assert agent_sessions.get("developer", "p1") is None  # nothing stored yet

    agent_sessions.remember("developer", "p1", "sess-1")
    assert agent_sessions.get("developer", "p1") == "sess-1"
    # keyed by (agent, project) — a different project / agent is independent
    assert agent_sessions.get("developer", "p2") is None
    assert agent_sessions.get("frontend", "p1") is None

    agent_sessions.remember("developer", "p1", None)  # no-op, don't clobber
    assert agent_sessions.get("developer", "p1") == "sess-1"

    agent_sessions.remember("developer", "p1", "sess-2")  # overwrite
    assert agent_sessions.get("developer", "p1") == "sess-2"

    agent_sessions.forget("developer", "p1")
    assert agent_sessions.get("developer", "p1") is None


def _board() -> None:
    d = tempfile.mkdtemp()
    assert team_memory.read(d) == ""  # nothing yet

    team_memory.seed(d, "сделать лендинг кофейни")
    board = team_memory.read(d)
    assert "сделать лендинг кофейни" in board and "Ход работы" in board

    team_memory.append(d, "Аналитик", "собрал требования: 3 секции, форма заявки")
    team_memory.append(d, "Backend-разработчик", "написал app.py (Flask)")
    board = team_memory.read(d)
    assert "Аналитик" in board and "app.py" in board

    team_memory.seed(d, "новая задача")  # a new task starts a clean board
    board = team_memory.read(d)
    assert "новая задача" in board and "app.py" not in board


def _decision_parse() -> None:
    from src.graph.team_graph import _parse_decision

    assert _parse_decision("РАБОТА: склонировать и проверить репо") == (
        "work", "склонировать и проверить репо")
    assert _parse_decision("ОТВЕТ: привет, я на связи")[0] == "chat"
    assert _parse_decision("НЕТ") == ("no", "")
    assert _parse_decision("") == ("no", "")
    assert _parse_decision("- НЕТ")[0] == "no"
    # markdown/bullet noise is stripped
    assert _parse_decision("**РАБОТА:** написать app.py")[0] == "work"
    # a bare non-empty answer falls back to a chat reply (better than silence)
    assert _parse_decision("могу глянуть фронтенд")[0] == "chat"


def main() -> None:
    _sessions()
    _board()
    _decision_parse()
    print("agent-memory tests: OK")


if __name__ == "__main__":
    main()
