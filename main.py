"""Entry point: run the whole platform as ONE process.

A single FastAPI app (``src.web.app``) serves the admin panel AND hosts the
Telegram bots in the same event loop: the team/orchestration bot plus a personal
bot for every agent that has a token configured in the panel. Changing a token
in the admin panel starts/stops that agent's bot immediately — no restart.

    python main.py            # then open http://127.0.0.1:8100
"""
import logging

import uvicorn

from src.config import settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Quiet down the very chatty HTTP client used by python-telegram-bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Starting platform (admin + team + per-agent bots) on http://%s:%d",
        settings.admin_host,
        settings.admin_port,
    )
    # The app's lifespan seeds the registry and brings up the bots.
    uvicorn.run("src.web.app:app", host=settings.admin_host, port=settings.admin_port)


if __name__ == "__main__":
    main()
