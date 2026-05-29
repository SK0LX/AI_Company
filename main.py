"""Entry point: start the Telegram bot for the AI IT company."""
import logging

from telegram import Update

from src.bot.telegram_bot import build_application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Quiet down the very chatty HTTP client used by python-telegram-bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = build_application()
    logging.getLogger(__name__).info("Bot starting (polling)...")
    # Receive ALL update types — crucially callback_query, so the plan
    # approve/change/cancel BUTTONS work. (Limiting to ["message"] silently
    # dropped button presses.)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
