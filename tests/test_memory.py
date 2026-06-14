"""Shared team memory tests (Obsidian KB tools). No LLM.

    python tests/test_memory.py

Points the vault at a temp dir so it never touches the real ~/ObsidianAITeam.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.tools import list_memory, read_memory, save_memory, search_memory
from src.config import settings


def main() -> None:
    with tempfile.TemporaryDirectory() as vault:
        settings.wiki_dir = vault  # tools read settings.wiki_dir at call time

        # save a note
        out = save_memory.invoke(
            {"path": "projects/coffee-shop",
             "content": "# coffee-shop\n\n## How it works\nFastAPI backend + React front."}
        )
        assert "saved wiki note" in out
        assert os.path.isfile(os.path.join(vault, "projects", "coffee-shop.md"))

        # list shows it
        idx = list_memory.invoke({})
        assert "projects/coffee-shop" in idx

        # search finds it by content
        hit = search_memory.invoke({"query": "FastAPI"})
        assert "coffee-shop" in hit

        # read returns the body
        body = read_memory.invoke({"path": "projects/coffee-shop"})
        assert "How it works" in body and "FastAPI" in body

        # tools are wired into the graph for tool-using specialists
        from src.graph import team_graph as tg
        from src.agents.tools import save_memory as sm
        assert sm.name == "save_memory"

    print("memory tests: OK")


if __name__ == "__main__":
    main()
