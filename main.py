"""Entry point: start the Telegram bots for the AI IT company.

Runs the main team/orchestration bot plus a personal bot for every agent that
has a Telegram token configured in the admin panel (see TelegramManager).
"""
import logging

from src.bot.manager import TelegramManager
from src.registry import registry


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Quiet down the very chatty HTTP client used by python-telegram-bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Load agents from the DB (seed on first run). The team reads roles, prompts
    # and permissions from here, so the admin panel affects the bots.
    registry.setup()

    logging.getLogger(__name__).info("Starting bots (team + per-agent)…")
    TelegramManager().run()


if __name__ == "__main__":
    main()
