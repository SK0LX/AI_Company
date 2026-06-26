"""Live per-agent activity — "what each agent is doing right now" — for the office
map. In-memory + best-effort: each change is pushed to the event hub so the
dashboard updates live, and :func:`snapshot` folds it into the office_state
refetch. Entries auto-expire so a crashed/abandoned turn doesn't leave an agent
stuck "working".
"""
from __future__ import annotations

import time

from src.events import hub

# Seconds before a stale activity is treated as idle (a turn should finish well
# within this; the cap just prevents a ghost "working" state after a crash).
_TTL = 180.0

# slug -> {"status": str, "note": str, "ts": float}. status: working|talking|handoff|idle.
_state: dict[str, dict] = {}


def set_activity(slug: str, status: str, note: str = "") -> None:
    """Mark what ``slug`` is doing now and push it live to the dashboard."""
    if not slug:
        return
    note = (note or "")[:160]
    _state[slug] = {"status": status, "note": note, "ts": time.time()}
    hub.publish({"event": "office", "slug": slug, "status": status, "note": note})


def clear_activity(slug: str) -> None:
    """Mark ``slug`` idle (done with its turn)."""
    if not slug:
        return
    _state.pop(slug, None)
    hub.publish({"event": "office", "slug": slug, "status": "idle", "note": ""})


def snapshot() -> dict:
    """Current activity per agent, dropping anything past the TTL."""
    now = time.time()
    out: dict[str, dict] = {}
    for slug, v in list(_state.items()):
        if now - v["ts"] > _TTL:
            _state.pop(slug, None)
            continue
        out[slug] = {"status": v["status"], "note": v["note"]}
    return out
