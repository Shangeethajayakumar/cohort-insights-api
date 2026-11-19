"""Application entry point: uvicorn backend.app:app."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError
from redis.exceptions import RedisError

from backend.config.settings import Settings
from backend.scripts.common.logging_config import configure_logging
from backend.scripts.core.handlers.documents import router
from backend.scripts.core.services.documents import DocumentService
from backend.scripts.db.connections import connect_service

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, service: DocumentService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.settings = settings or Settings()
        configure_logging()
        if service is not None:
            application.state.service = service
            yield
        else:
            async with connect_service(application.state.settings) as connected:
                application.state.service = connected
                yield

    application = FastAPI(title="Cohort Insights API", version="1.0.0", lifespan=lifespan)
    application.state.settings = settings
    application.include_router(router)

    @application.exception_handler(PyMongoError)
    @application.exception_handler(RedisError)
    async def dependency_unavailable(request: Request, error: Exception):
        logger.error("dependency_unavailable", exc_info=(type(error), error, error.__traceback__))
        return JSONResponse(status_code=503, content={"detail": "Storage temporarily unavailable"})

    @application.middleware("http")
    async def log_request(request: Request, call_next):
        response = await call_next(request)
        logger.info(
            "http_request", extra={"method": request.method, "status_code": response.status_code}
        )
        return response

    return application


app = create_app()