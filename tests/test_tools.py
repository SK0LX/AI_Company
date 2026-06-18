"""Unit tests for agent tools: file ops, wiki, run_python, @requires. No network.

    python tests/test_tools.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents import tools as T
from src.config import settings
from src.registry import registry


def _file_ops() -> None:
    # sanitize_project
    assert T.sanitize_project("My Project!") == "my-project"
    assert T.sanitize_project("") == "project"
    assert T.sanitize_project("../../etc") == "etc"

    registry.setup()  # so @requires can resolve permissions
    T.set_current_agent("")  # no agent -> @requires passes through
    T.set_project_subdir("proj")

    # write + read + list
    assert T.write_file.invoke({"path": "backend/main.py", "content": "print(1)"}).startswith("[saved")
    assert "print(1)" in T.read_file.invoke({"path": "backend/main.py"})
    listing = T.list_files.invoke({})
    assert "backend/main.py" in listing
    assert "backend/main.py" in T.current_project_files()

    # read/list missing
    assert "not found" in T.read_file.invoke({"path": "nope.py"})
    assert "no such folder" in T.list_files.invoke({"subdir": "ghost"})

    # move + delete (+ prune empty dirs)
    assert T.move_file.invoke({"src": "backend/main.py", "dst": "app/main.py"}).startswith("[moved")
    assert "not found" in T.move_file.invoke({"src": "backend/main.py", "dst": "x.py"})
    assert T.delete_file.invoke({"path": "app/main.py"}).startswith("[deleted")
    assert "not found" in T.delete_file.invoke({"path": "app/main.py"})

    # path escape is rejected
    try:
        T._safe_path("../escape.txt")
        raise AssertionError("expected ValueError for path escape")
    except ValueError:
        pass
    assert "escapes the workspace" in T.write_file.invoke({"path": "../evil", "content": "x"})


def _wiki_ops() -> None:
    assert T.wiki_write_note("projects/alpha", "# Alpha\nUses FastAPI.").startswith("[saved wiki")
    assert "FastAPI" in T.wiki_read_note("projects/alpha")
    assert "not found" in T.wiki_read_note("projects/ghost")
    assert "alpha" in T.wiki_search_notes("FastAPI")
    assert T.wiki_search_notes("zzzznomatch").startswith("[")
    assert "projects/alpha" in T.wiki_index()
    # wiki path escape
    try:
        T._safe_wiki_path("../escape")
        raise AssertionError("expected ValueError for wiki path escape")
    except ValueError:
        pass


def _run_python() -> None:
    out = asyncio.run(T.run_python.ainvoke({"code": "print(6 * 7)"}))
    assert "42" in out
    err = asyncio.run(T.run_python.ainvoke({"code": "raise ValueError('boom')"}))
    assert "boom" in err  # traceback captured
    quiet = asyncio.run(T.run_python.ainvoke({"code": "x = 1"}))
    assert "no output" in quiet


def _permissions_and_shell() -> None:
    # @requires denies when the current agent lacks the permission
    T.set_current_agent("ceo")  # ceo has no can_edit_files
    assert T.write_file.invoke({"path": "p.txt", "content": "x"}).startswith("[denied")
    # run_shell: developer has can_run_shell but execution is disabled by config
    settings.enable_shell_execution = False
    T.set_current_agent("developer")
    assert "disabled" in asyncio.run(T.run_shell.ainvoke({"command": "echo hi"}))
    # ceo lacks can_run_shell -> denied before reaching the body
    T.set_current_agent("ceo")
    assert asyncio.run(T.run_shell.ainvoke({"command": "echo hi"})).startswith("[denied")
    T.set_current_agent("")


def main() -> None:
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as wiki:
        settings.workspace_dir = ws
        settings.wiki_dir = wiki
        _file_ops()
        _wiki_ops()
        _run_python()
        _permissions_and_shell()
    print("tools tests: OK")


if __name__ == "__main__":
    main()
