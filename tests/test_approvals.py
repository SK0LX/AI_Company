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


def main() -> None:
    asyncio.run(_run())
    print("approvals tests: OK")


if __name__ == "__main__":
    main()
