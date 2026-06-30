"""Unit tests for the shell-command approval bridge. No network.

    python tests/test_approvals.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approvals


async def _run() -> None:
    approvals.clear_asker()
    # no asker installed -> denied
    assert await approvals.request_command_approval("rm -rf /") is False

    # asker that approves
    async def yes(cmd: str) -> bool:
        return True

    approvals.set_asker(yes)
    assert await approvals.request_command_approval("ls") is True

    # asker that declines
    async def no(cmd: str) -> bool:
        return False

    approvals.set_asker(no)
    assert await approvals.request_command_approval("ls") is False

    # a broken asker must not run the command
    async def boom(cmd: str) -> bool:
        raise RuntimeError("channel down")

    approvals.set_asker(boom)
    assert await approvals.request_command_approval("ls") is False

    approvals.clear_asker()
    assert await approvals.request_command_approval("ls") is False


async def _typed() -> None:
    """The generalized, audited approval path + recent() readback."""
    from src.registry import registry

    registry.setup()  # ensure the Approval table exists

    async def yes(prompt: str) -> bool:
        # non-shell kinds are shown with a label
        assert prompt.startswith("[")
        return True

    approvals.set_asker(yes)
    assert await approvals.request_approval("self_modify", "edit team_graph.py", agent="maintainer") is True
    approvals.clear_asker()
    assert await approvals.request_approval("budget_override", "raise cap") is False  # no asker

    rows = approvals.recent(20)
    kinds = {r["kind"] for r in rows}
    assert "self_modify" in kinds and "budget_override" in kinds
    decided = [r for r in rows if r["kind"] == "self_modify"]
    assert decided and decided[0]["status"] == "approved" and decided[0]["decided_by"] == "user"


async def _web_decision() -> None:
    """The dashboard can resolve a pending approval, beating a slow Telegram asker."""
    from src.registry import registry

    registry.setup()

    async def hang(prompt: str) -> bool:
        await asyncio.sleep(3600)  # never answers within the test
        return True

    approvals.set_asker(hang)
    task = asyncio.create_task(approvals.request_approval("self_modify", "web race", agent="maintainer"))
    await asyncio.sleep(0.1)
    mine = [p for p in approvals.pending() if p["summary"] == "web race"]
    assert mine, "approval should be pending while the asker hangs"
    assert approvals.decide(mine[0]["id"], True) is True  # decide from the 'dashboard'
    result = await asyncio.wait_for(task, timeout=2)
    assert result is True
    # the resolved approval is no longer pending
    assert all(p["summary"] != "web race" for p in approvals.pending())
    approvals.clear_asker()


async def _telegram_wins() -> None:
    """When the Telegram asker answers first, it wins the race and the decision is
    recorded as coming from telegram (the dashboard never had to act)."""
    from src.registry import registry

    registry.setup()

    async def quick_yes(prompt: str) -> bool:
        return True  # answers immediately

    approvals.set_asker(quick_yes)
    try:
        ok = await approvals.request_approval("shell", "ls -la /tmp", agent="developer")
        assert ok is True
        rec = [r for r in approvals.recent(20) if r["summary"] == "ls -la /tmp"]
        assert rec and rec[0]["status"] == "approved" and rec[0]["reason"] == "telegram"
        assert all(p["summary"] != "ls -la /tmp" for p in approvals.pending())
    finally:
        approvals.clear_asker()


async def _agent_notifier() -> None:
    """A Claude-engine permission gate (require_asker=False, no asker) fires the
    Telegram notifier and is resolved by a button tap via decide()."""
    from src.registry import registry

    registry.setup()
    approvals.clear_asker()
    seen: list[tuple[int, str, str]] = []

    def notifier(approval_id: int, kind: str, summary: str, agent: str) -> None:
        seen.append((approval_id, kind, agent))

    approvals.set_approval_notifier(notifier)
    try:
        task = asyncio.create_task(
            approvals.request_approval("agent_exec", "Bash: git clone x", agent="developer", require_asker=False)
        )
        await asyncio.sleep(0.1)
        assert seen, "notifier should fire for an agent approval with no asker"
        aid, kind, agent = seen[-1]
        assert kind == "agent_exec" and agent == "developer"
        # the 'Telegram button tap' resolves it
        assert approvals.decide(aid, True, reason="telegram") is True
        assert await asyncio.wait_for(task, timeout=2) is True

        # with an asker installed (shell path), the notifier must NOT double-fire
        seen.clear()

        async def yes(prompt: str) -> bool:
            return True

        approvals.set_asker(yes)
        assert await approvals.request_approval("shell", "ls", agent="developer") is True
        assert not seen, "notifier must not fire when a contextvar asker is present"
    finally:
        approvals.set_approval_notifier(None)
        approvals.clear_asker()


def main() -> None:
    asyncio.run(_run())
    asyncio.run(_typed())
    asyncio.run(_web_decision())
    asyncio.run(_telegram_wins())
    asyncio.run(_agent_notifier())
    print("approvals tests: OK")


if __name__ == "__main__":
    main()
