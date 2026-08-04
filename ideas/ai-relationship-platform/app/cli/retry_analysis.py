"""Explicitly requeue one retryable failed analysis."""

import argparse
import asyncio
import logging
from uuid import UUID

from app.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.logging import configure_logging
from app.services.analysis_recovery import AnalysisRetryOutcome, retry_failed_analysis

logger = logging.getLogger(__name__)


async def run(analysis_id: UUID, user_id: UUID) -> AnalysisRetryOutcome:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(str(settings.database_url))
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            return await retry_failed_analysis(session, analysis_id, user_id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-id", required=True, type=UUID)
    parser.add_argument("--user-id", required=True, type=UUID)
    args = parser.parse_args()
    outcome = asyncio.run(run(args.analysis_id, args.user_id))
    logger.info("analysis_retry outcome=%s", outcome.value)
    if outcome is not AnalysisRetryOutcome.REQUEUED:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
