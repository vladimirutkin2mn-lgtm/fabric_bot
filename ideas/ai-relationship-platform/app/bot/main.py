"""aiogram bootstrap for polling locally and webhook registration in deployment."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.dependencies import OnboardingDependencyMiddleware
from app.bot.handlers import router
from app.bot.rate_limit import FixedWindowRateLimiter, RateLimitMiddleware
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.logging import configure_logging
from app.providers.analytics import NoOpAnalyticsClient
from app.providers.llm.factory import create_llm_client

logger = logging.getLogger(__name__)


def create_dispatcher(settings: Settings) -> Dispatcher:
    """Create a dispatcher with explicit, per-update dependencies."""
    dispatcher = Dispatcher()
    engine = create_engine(str(settings.database_url))
    dependency_middleware = OnboardingDependencyMiddleware(
        create_session_factory(engine), NoOpAnalyticsClient(), settings, create_llm_client(settings)
    )
    rate_middleware = RateLimitMiddleware(FixedWindowRateLimiter())
    dispatcher.message.outer_middleware(rate_middleware)
    dispatcher.callback_query.outer_middleware(rate_middleware)
    dispatcher.update.outer_middleware(dependency_middleware)
    dispatcher.include_router(router)
    dispatcher["database_engine"] = engine
    return dispatcher


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
    dispatcher = create_dispatcher(resolved_settings)
    try:
        if resolved_settings.webhook_enabled:
            await configure_webhook(bot, resolved_settings)
            logger.info("Telegram webhook configured; waiting for API webhook transport")
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(bot)
    finally:
        await dispatcher["database_engine"].dispose()
        await bot.session.close()


def main() -> None:
    """Synchronous console entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
