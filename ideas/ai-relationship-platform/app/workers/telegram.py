"""Graceful durable Telegram update worker runtime."""

import asyncio
import os
import signal
import socket

from aiogram import Bot

from app.bot.main import close_dispatcher, create_dispatcher
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.deployment import DeploymentSettings, get_deployment_settings
from app.logging import configure_logging
from app.observability.settings import get_observability_settings
from app.services.sensitive_content import AESGCMSensitiveContentCipher, decode_configured_key
from app.services.telegram_update_inbox import TelegramUpdateInboxService
from app.services.telegram_update_worker import TelegramUpdateWorker


async def run(
    settings: Settings | None = None,
    deployment: DeploymentSettings | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    resolved = settings or get_settings()
    runtime = deployment or get_deployment_settings()
    if not resolved.webhook_enabled:
        raise ValueError("Telegram update worker requires webhook mode")
    configure_logging(resolved.log_level)
    engine = create_engine(str(resolved.database_url))
    sessions = create_session_factory(engine)
    cipher = AESGCMSensitiveContentCipher(
        decode_configured_key(resolved.content_encryption_key.get_secret_value())
    )
    bot = Bot(token=resolved.telegram_bot_token.get_secret_value())
    dispatcher = create_dispatcher(resolved, get_observability_settings(), engine)
    inbox = TelegramUpdateInboxService(
        sessions,
        cipher,
        lease_seconds=runtime.telegram_update_lease_seconds,
        retry_base_seconds=runtime.telegram_update_retry_base_seconds,
        max_attempts=runtime.telegram_update_max_attempts,
    )
    worker = TelegramUpdateWorker(inbox, bot, dispatcher)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopped = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if stop is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
    try:
        while not stopped.is_set():
            worked = await worker.run_once(worker_id)
            if worked:
                continue
            try:
                await asyncio.wait_for(
                    stopped.wait(), timeout=runtime.telegram_worker_idle_seconds
                )
            except TimeoutError:
                pass
    finally:
        try:
            await close_dispatcher(dispatcher)
        finally:
            try:
                await bot.session.close()
            finally:
                await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
