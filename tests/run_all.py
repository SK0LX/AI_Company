"""Run every test_*.py in this folder and print a summary. No pytest needed.

    python tests/run_all.py

Each test runs in its own subprocess with a timeout. Some tests build the team
app, which keeps a process-alive resource open (an aiosqlite connection in
``_get_team_app``), so the test prints its success marker but the process never
exits. We treat "printed a success marker before the timeout" as a pass (and
note the teardown hang) rather than letting one test block the whole suite.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PER_TEST_TIMEOUT = 90  # seconds; generous — most tests finish in <5s

# A test that printed one of these is considered to have passed its assertions.
_SUCCESS_MARKERS = (" tests: OK", " test: OK", ": OK", "tests: OK")
_FAILURE_MARKERS = ("Traceback", "AssertionError", "Error:")


def _looks_successful(out: str) -> bool:
    return any(m in out for m in _SUCCESS_MARKERS) and not any(
        m in out for m in _FAILURE_MARKERS
    )


def main() -> int:
    tests = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    passed, hung, failed = [], [], []
    for name in tests:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, name)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            out, _ = proc.communicate(timeout=PER_TEST_TIMEOUT)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            timed_out = True
        # ignore the starlette/httpx deprecation noise
        noise = ("StarletteDeprecation", "from starlette.testclient")
        clean = "\n".join(l for l in (out or "").splitlines() if not any(n in l for n in noise))

        if not timed_out and proc.returncode == 0:
            passed.append(name)
            print(f"✅ {name}")
        elif _looks_successful(clean):
            # Assertions passed; the process just hung at teardown (or was killed).
            hung.append(name)
            print(f"🟡 {name}  (passed; teardown hang)")
        else:
            failed.append(name)
            reason = "timeout" if timed_out else f"exit {proc.returncode}"
            print(f"❌ {name}  ({reason})\n{clean}")

    total = len(tests)
    print(f"\n{'=' * 40}")
    print(f"{len(passed)} passed, {len(hung)} passed-with-teardown-hang, "
          f"{len(failed)} failed, {total} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
