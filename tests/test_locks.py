"""Unit tests for atomic claim + resource locks (v3 pull-coordination) and the
agent coordination tools. Includes a real threaded race: never two winners.
No network.

    python tests/test_locks.py
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select

from src import collab, locks
from src.agents import tools as T
from src.db.engine import get_session
from src.db.models import ResourceLock
from src.registry import registry


def _task_claim() -> None:
    tid = collab.create_task("ut-claim", created_by="ceo")
    try:
        assert locks.claim_task(tid, "developer")            # got it
        assert locks.claim_task(tid, "frontend") is None     # taken by another
        assert locks.claim_task(tid, "developer")            # idempotent for holder
        assert locks.task_holder(tid) == "developer"
        assert locks.release_task(tid, "frontend") is False  # not the holder
        assert locks.release_task(tid, "developer") is True
        assert locks.task_holder(tid) is None
        assert locks.claim_task(tid, "frontend")             # free again
    finally:
        collab.delete_task(tid)


def _resource_lock() -> None:
    key = "ut:res:" + secrets.token_hex(3)
    try:
        tok = locks.acquire_lock(key, "developer", ttl=60)
        assert tok
        assert locks.acquire_lock(key, "frontend") is None   # busy
        assert locks.who_holds(key) == "developer"
        assert locks.renew_lock(key, tok, ttl=120) is True
        # force-expire -> free for takeover
        with get_session() as s:
            row = s.exec(select(ResourceLock).where(ResourceLock.key == key)).first()
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            s.add(row)
            s.commit()
        assert locks.who_holds(key) is None
        assert locks.prune_expired() >= 1
        assert locks.acquire_lock(key, "frontend")           # can take expired
    finally:
        locks.release_lock(key, agent="developer")
        locks.release_lock(key, agent="frontend")


def _race_never_two_winners() -> None:
    """8 threads claim the same task at once — the safety invariant is that at most
    ONE wins (no double-work), ever."""
    tid = collab.create_task("ut-race", created_by="ceo")
    try:
        results: list = []
        lock = threading.Lock()

        def grab(name: str) -> None:
            try:
                r = locks.claim_task(tid, name)
            except Exception:  # noqa: BLE001 - a transient sqlite lock counts as a loss
                r = None
            with lock:
                results.append(r)

        threads = [threading.Thread(target=grab, args=(f"a{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins = [r for r in results if r]
        assert len(wins) <= 1, f"two agents claimed the same task: {len(wins)} winners"
    finally:
        collab.delete_task(tid)


def _tools() -> None:
    registry.setup()
    tid = collab.create_task("ut-tool", created_by="ceo")
    key = "ut:tool:" + secrets.token_hex(3)
    try:
        T.set_current_agent("developer")
        assert "claimed" in T.board_claim.invoke({"task_id": tid})
        T.set_current_agent("frontend")
        assert "busy" in T.board_claim.invoke({"task_id": tid})
        T.set_current_agent("developer")
        assert "released" in T.board_release.invoke({"task_id": tid})

        assert "locked" in T.lock_acquire.invoke({"key": key})
        T.set_current_agent("frontend")
        assert "busy" in T.lock_acquire.invoke({"key": key})
        assert "held by developer" in T.lock_who.invoke({"key": key})
        T.set_current_agent("developer")
        assert "unlocked" in T.lock_release.invoke({"key": key})
        assert "free" in T.lock_who.invoke({"key": key})

        assert T.budget_remaining.invoke({}).startswith("[")
    finally:
        T.set_current_agent("")
        locks.release_lock(key, agent="developer")
        locks.release_lock(key, agent="frontend")
        collab.delete_task(tid)


def _idempotent_claim_token() -> None:
    """Re-claiming a task you already hold returns the SAME token (no orphaning)."""
    tid = collab.create_task("ut-idem", created_by="ceo")
    try:
        tok1 = locks.claim_task(tid, "developer")
        tok2 = locks.claim_task(tid, "developer")
        assert tok1 and tok1 == tok2, "re-claim must return the original token"
        # the original handle still releases it
        assert locks.release_task(tid, "developer", token=tok1) is True
    finally:
        collab.delete_task(tid)


def _release_token() -> None:
    """release_task with a token is compare-and-set: a stale token can't release."""
    tid = collab.create_task("ut-token", created_by="ceo")
    try:
        tok = locks.claim_task(tid, "developer")
        assert tok
        assert locks.release_task(tid, "developer", token="deadbeef") is False  # stale
        assert locks.task_holder(tid) == "developer"  # still held
        assert locks.release_task(tid, "developer", token=tok) is True  # right token
        assert locks.task_holder(tid) is None
    finally:
        collab.delete_task(tid)


def _resource_lock_race_insert() -> None:
    """8 threads racing to first-acquire a brand-new key → exactly one winner."""
    key = "ut-race-lock-" + secrets.token_hex(4)
    winners: list[tuple[int, str]] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()  # maximize real contention on the INSERT
        tok = locks.acquire_lock(key, agent=f"a{i}")
        if tok:
            winners.append((i, tok))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(winners) == 1, f"expected exactly 1 lock winner, got {len(winners)}"
    finally:
        for i, _tok in winners:
            locks.release_lock(key, agent=f"a{i}")


def main() -> None:
    registry.setup()
    _task_claim()
    _idempotent_claim_token()
    _release_token()
    _resource_lock()
    _resource_lock_race_insert()
    _race_never_two_winners()
    _tools()
    print("locks tests: OK")


if __name__ == "__main__":
    main()
