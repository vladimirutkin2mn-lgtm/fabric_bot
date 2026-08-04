"""Run one bounded source-retention cleanup batch."""

import argparse
import asyncio
import logging

from app.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.services.retention import cleanup_expired_source

logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine(str(settings.database_url))
    sessions = create_session_factory(engine)
    async with sessions() as session:
        result = await cleanup_expired_source(
            session, batch_size=args.batch_size, dry_run=args.dry_run
        )
    logger.info("retention_cleanup examined=%s cleared=%s", result.examined, result.cleared)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
