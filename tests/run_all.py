"""Run every test_*.py in this folder and print a summary. No pytest needed.

    python tests/run_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    tests = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    passed, failed = [], []
    for name in tests:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, name)],
            capture_output=True, text=True,
        )
        # ignore the starlette/httpx deprecation noise on stderr
        noise = ("StarletteDeprecation", "from starlette.testclient")
        err = "\n".join(l for l in proc.stderr.splitlines() if not any(n in l for n in noise))
        if proc.returncode == 0:
            passed.append(name)
            print(f"✅ {name}")
        else:
            failed.append(name)
            print(f"❌ {name}\n{proc.stdout}\n{err}")

    print(f"\n{'=' * 40}\n{len(passed)} passed, {len(failed)} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
