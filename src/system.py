"""Host/runtime resource snapshot for the dashboard's system monitor.

Mirrors the "SYSTEM" panel in the AI-Office UI: RAM, disk and uptime. Uses
``psutil`` when available for full host stats; degrades gracefully (disk via
``shutil``, app uptime, process RSS, load average) when it isn't installed.
"""
from __future__ import annotations

import os
import shutil
import time

# Module import ≈ process start, so this doubles as the app's uptime origin.
_START = time.time()


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_uptime(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}д {h}ч {m}м"
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"


def snapshot() -> dict:
    """Best-effort resource snapshot. Never raises."""
    out: dict = {"uptime_seconds": int(time.time() - _START)}
    out["uptime"] = _fmt_uptime(out["uptime_seconds"])

    # Disk (cross-platform, no deps).
    try:
        du = shutil.disk_usage(os.path.abspath("."))
        out.update(disk_used=du.used, disk_total=du.total,
                   disk_percent=round(100 * du.used / du.total, 1),
                   disk_label=f"{_fmt_bytes(du.used)} / {_fmt_bytes(du.total)}")
    except Exception:  # noqa: BLE001
        pass

    # RAM + CPU via psutil when present.
    try:
        import psutil

        vm = psutil.virtual_memory()
        out.update(mem_used=vm.used, mem_total=vm.total, mem_percent=round(vm.percent, 1),
                   mem_label=f"{_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)}",
                   cpu_percent=round(psutil.cpu_percent(interval=0.0), 1))
        return out
    except Exception:  # noqa: BLE001
        pass

    # Fallback: process RSS + load average.
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss = rss if rss > 10 ** 9 else rss * 1024  # macOS bytes vs Linux KB
        out.update(mem_used=rss, mem_label=f"{_fmt_bytes(rss)} (процесс)")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except Exception:  # noqa: BLE001
        pass
    return out
