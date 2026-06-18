"""Unit tests for the OpenRouter daily-quota tracker. No network.

    python tests/test_quota.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import quota
from src.config import settings


def main() -> None:
    prev_path = quota._PATH
    prev_provider = settings.llm_provider
    prev_limit = settings.free_daily_limit
    prev_warn = settings.free_daily_warn_at
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(tmp)  # start with no file
    quota._PATH = tmp
    try:
        # fresh day starts at 0
        assert quota.calls_today() == 0
        quota.record_call()
        quota.record_call(4)
        assert quota.calls_today() == 5

        # remaining respects the configured limit, never negative
        settings.free_daily_limit = 6
        assert quota.remaining() == 1
        quota.record_call(10)
        assert quota.remaining() == 0

        # tracked / is_low / is_exhausted depend on the provider
        settings.llm_provider = "openrouter"
        settings.free_daily_warn_at = 100
        assert quota.tracked() and quota.is_low() and quota.is_exhausted()
        settings.llm_provider = "anthropic"
        assert not quota.tracked() and not quota.is_low() and not quota.is_exhausted()

        # message helpers produce non-empty Russian strings
        assert quota.status_line() and "OpenRouter" in quota.low_warning()
        assert "лимит" in quota.exhausted_warning()

        # a stale (yesterday) file resets on load
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"date": "2000-01-01", "count": 999}, fh)
        assert quota.calls_today() == 0

        # the LangChain callback increments the counter
        quota.counter.on_chat_model_start()
        quota.counter.on_llm_start()
        assert quota.calls_today() == 2
    finally:
        quota._PATH = prev_path
        settings.llm_provider = prev_provider
        settings.free_daily_limit = prev_limit
        settings.free_daily_warn_at = prev_warn
        if os.path.exists(tmp):
            os.remove(tmp)

    print("quota tests: OK")


if __name__ == "__main__":
    main()
