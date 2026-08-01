"""Logging configuration that avoids handling private message content."""

import logging


def configure_logging(level: str) -> None:
    """Configure concise process-level logs."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s",
    )
