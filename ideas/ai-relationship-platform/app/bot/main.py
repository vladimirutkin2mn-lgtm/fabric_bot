"""aiogram bootstrap for polling locally and webhook registration in deployment."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.config import Settings, get_settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def create_dispatcher() -> Dispatcher:
    """Create the dispatcher; product handlers arrive in later milestones."""
    return Dispatcher()


async def configure_webhook(bot: Bot, settings: Settings) -> None:
    """Register webhook delivery using Telegram's verification secret."""
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret.get_secret_value(),
    )


async def run(settings: Settings | None = None) -> None:
    """Run polling locally or register webhook-ready configuration."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    bot = Bot(token=resolved_settings.telegram_bot_token.get_secret_value())
    dispatcher = create_dispatcher()
    try:
        if resolved_settings.webhook_enabled:
            await configure_webhook(bot, resolved_settings)
            logger.info("Telegram webhook configured; waiting for API webhook transport")
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    """Synchronous console entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
