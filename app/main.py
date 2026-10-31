from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.compat.routes import router as compat_router
from app.api.errors import (
    SupplierServiceException,
    generic_exception_handler,
    supplier_exception_handler,
    validation_exception_handler,
)
from app.api.routes import health, jobs, products, providers, search
from app.browser.manager import get_browser_manager
from app.config import get_settings
from app.database.session import init_db
from app.observability.logging import setup_logging
from app.suppliers.registry import get_supplier_registry

settings = get_settings()
setup_logging(settings.LOG_LEVEL)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    logger.info(
        "application_starting", service=settings.SERVICE_NAME, environment=settings.ENVIRONMENT
    )

    # 1. Validate production configuration secrets
    from app.config import validate_production_configuration

    validate_production_configuration(settings)

    # 2. In production, verify Alembic migration head; in other environments, init db
    if settings.ENVIRONMENT.lower() == "production":
        from app.database.session import check_alembic_head_revision, get_expected_alembic_head

        is_current, rev = await check_alembic_head_revision()
        if not is_current:
            expected = get_expected_alembic_head()
            msg = (
                f"FATAL: Database revision '{rev}' does not match expected Alembic head '{expected}'. "
                "Run 'alembic upgrade head' before starting the service in production."
            )
            logger.error("alembic_migration_check_failed", revision=rev, expected=expected)
            raise RuntimeError(msg)
        logger.info("alembic_head_revision_verified", revision=rev)

    else:
        await init_db()

    # 3. Supplier Registry
    registry = get_supplier_registry()
    logger.info("registered_providers", providers=registry.list_providers())

    yield

    # Shutdown
    logger.info("application_shutting_down")
    browser_manager = get_browser_manager()
    await browser_manager.shutdown()


app = FastAPI(
    title="Supplier Browser Service",
    description="Production-ready cloud product discovery and extraction API for ecommerce suppliers.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: Any) -> Response:

    # Correlation ID
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
    request.state.request_id = request_id

    start_time = time.time()
    response: Response = await call_next(request)
    duration_ms = round((time.time() - start_time) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = str(duration_ms)

    # Structured request log (excluding /health/live and /metrics from spamming logs)
    if request.url.path not in ("/health/live", "/metrics"):
        logger.info(
            "http_request_processed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

    return response


# Exception Handlers
app.add_exception_handler(SupplierServiceException, cast(Any, supplier_exception_handler))
app.add_exception_handler(RequestValidationError, cast(Any, validation_exception_handler))
app.add_exception_handler(Exception, cast(Any, generic_exception_handler))


# Routers
app.include_router(compat_router)
app.include_router(health.router)
app.include_router(search.router)
app.include_router(products.router)
app.include_router(providers.router)
app.include_router(jobs.router)
