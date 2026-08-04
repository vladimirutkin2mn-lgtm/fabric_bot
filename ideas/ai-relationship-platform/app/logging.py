"""Logging configuration that avoids handling private message content."""

import logging

from app.observability.context import current_correlation_id


class CorrelationIdFilter(logging.Filter):
    """Attach the ambient correlation ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id()
        return True


def configure_logging(level: str) -> None:
    """Configure concise process-level logs with a safe correlation field."""
    logging.basicConfig(
        level=level.upper(),
        format=(
            "%(asctime)s level=%(levelname)s logger=%(name)s "
            "correlation_id=%(correlation_id)s message=%(message)s"
        ),
    )
    root = logging.getLogger()
    if not any(isinstance(item, CorrelationIdFilter) for item in root.filters):
        root.addFilter(CorrelationIdFilter())
    for handler in root.handlers:
        if not any(isinstance(item, CorrelationIdFilter) for item in handler.filters):
            handler.addFilter(CorrelationIdFilter())
