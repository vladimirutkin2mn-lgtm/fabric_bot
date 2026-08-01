"""SQLAlchemy declarative base for future domain models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class imported by Alembic for metadata discovery."""
