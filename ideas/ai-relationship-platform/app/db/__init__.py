"""Database infrastructure."""

from app.db.followups import FollowUpQuestion
from app.db.refund_metadata import configure_refund_metadata

configure_refund_metadata()

__all__ = ["FollowUpQuestion"]
