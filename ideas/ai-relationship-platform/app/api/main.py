"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.db.session import create_engine
from app.logging import configure_logging


def create_app(settings: Settings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
    """Build an application with injectable configuration and database engine."""
    resolved_settings = settings or get_settings()
    resolved_engine = engine or create_engine(str(resolved_settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        yield
        await resolved_engine.dispose()

    application = FastAPI(title="HeartSignal API", version="0.1.0", lifespan=lifespan)
    application.state.db_engine = resolved_engine
    application.include_router(health_router)
    return application


app = create_app()
