"""Runnable payment jobs, reconciliation, and outbox worker."""

import asyncio
import logging
import signal
import socket
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.logging import configure_logging
from app.providers.analytics import DiscardingAnalyticsClient
from app.providers.payments.composition import create_payment_components
from app.services.billing_job_worker import BillingJobWorker
from app.services.billing_outbox_service import BillingOutboxWorker
from app.services.checkout_service import ReceiptContactCipher
from app.services.payment_completion_service import PaymentCompletionService
from app.services.payment_reconciliation_service import PaymentReconciliationSweeper

logger = logging.getLogger(__name__)


async def run(settings: Settings | None = None, stop: asyncio.Event | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    engine = create_engine(str(resolved.database_url))
    sessions = create_session_factory(engine)
    components = create_payment_components(resolved)
    gateways = {name.value: gateway for name, gateway in components.gateways.items()}
    completion = PaymentCompletionService(sessions, resolved.app_env == "production")
    jobs = BillingJobWorker(
        sessions,
        gateways,
        completion,
        resolved.billing_worker_lease_seconds,
        resolved.billing_retry_base_seconds,
        resolved.billing_worker_max_attempts,
        resolved.payment_public_base_url,
        ReceiptContactCipher(resolved.content_encryption_key.get_secret_value()),
    )
    outbox = BillingOutboxWorker(
        sessions,
        DiscardingAnalyticsClient(),
        resolved.billing_worker_lease_seconds,
        resolved.billing_retry_base_seconds,
        resolved.billing_worker_max_attempts,
    )
    sweeper = PaymentReconciliationSweeper(
        sessions, resolved.billing_pending_reconciliation_seconds, set(gateways)
    )
    stopped = stop or asyncio.Event()
    worker_id = f"{socket.gethostname()}:{id(stopped)}"
    loop = asyncio.get_running_loop()
    if stop is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
    next_sweep = datetime.now(UTC)
    try:
        while not stopped.is_set():
            now = datetime.now(UTC)
            if now >= next_sweep:
                await sweeper.enqueue_stale()
                next_sweep = now + timedelta(
                    seconds=resolved.billing_reconciliation_interval_seconds
                )
            try:
                worked = await jobs.run_once(worker_id)
                worked = await outbox.run_once(worker_id) or worked
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("billing_worker_iteration_failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=1.0)
                except TimeoutError:
                    pass
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
