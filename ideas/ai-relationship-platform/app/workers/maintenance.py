"""Graceful periodic maintenance for retention and interrupted analyses."""

import asyncio
import logging
import signal
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.deployment import DeploymentSettings, get_deployment_settings
from app.logging import configure_logging
from app.services.analysis_recovery import AnalysisRecoveryResult, requeue_stale_processing
from app.services.retention import RetentionResult, cleanup_expired_source

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaintenanceResult:
    analysis_recovery: AnalysisRecoveryResult
    retention: RetentionResult


async def run_once(
    sessions: async_sessionmaker[AsyncSession], deployment: DeploymentSettings
) -> MaintenanceResult:
    """Run one bounded iteration using separate transactions per maintenance concern."""
    async with sessions() as session:
        recovery = await requeue_stale_processing(
            session,
            stale_after_seconds=deployment.analysis_processing_stale_seconds,
            batch_size=deployment.maintenance_batch_size,
        )
    async with sessions() as session:
        retention = await cleanup_expired_source(
            session,
            batch_size=deployment.maintenance_batch_size,
        )
    return MaintenanceResult(recovery, retention)


async def run(
    settings: Settings | None = None,
    deployment: DeploymentSettings | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Run maintenance until SIGTERM/SIGINT and finish the active iteration before exit."""
    resolved = settings or get_settings()
    runtime = deployment or get_deployment_settings()
    configure_logging(resolved.log_level)
    engine = create_engine(str(resolved.database_url))
    sessions = create_session_factory(engine)
    stopped = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if stop is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
    try:
        while not stopped.is_set():
            try:
                result = await run_once(sessions, runtime)
                logger.info(
                    "maintenance_iteration stale_examined=%s stale_requeued=%s "
                    "stale_financially_closed=%s retention_examined=%s retention_cleared=%s",
                    result.analysis_recovery.examined,
                    result.analysis_recovery.requeued,
                    result.analysis_recovery.financially_closed,
                    result.retention.examined,
                    result.retention.cleared,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance_iteration_failed")
            try:
                await asyncio.wait_for(
                    stopped.wait(), timeout=runtime.maintenance_interval_seconds
                )
            except TimeoutError:
                pass
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
