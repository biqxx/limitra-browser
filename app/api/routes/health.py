from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.browser.manager import get_browser_manager
from app.cache.service import get_redis_client
from app.config import get_settings
from app.database.session import get_db_session
from app.observability.metrics import get_latest_metrics
from app.workers.queue import get_job_manager

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["Health & Monitoring"])


def get_expected_alembic_head() -> str | None:
    """Programmatically determine the current Alembic head revision."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception as e:
        logger.warning("failed_to_resolve_alembic_head", error=str(e))
        return None


@router.get("/health/live", summary="Liveness Probe")
async def health_live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness Probe")
async def health_ready(response: Response) -> dict[str, Any]:
    settings = get_settings()
    checks: dict[str, Any] = {
        "database": False,
        "migration": False,
        "redis": False,
        "queue": False,
        "browser_pool": False,
    }
    is_ready = True

    # 1. Check PostgreSQL and Alembic Migration Revision
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
            checks["database"] = True

            expected_head = get_expected_alembic_head()
            try:
                res = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
                current_rev = res.scalar()
                checks["migration_revision"] = current_rev
                checks["expected_revision"] = expected_head

                if expected_head and current_rev != expected_head:
                    logger.error(
                        "readiness_check_migration_mismatch",
                        expected=expected_head,
                        current=current_rev,
                    )
                    is_ready = False
                    checks["migration"] = False
                else:
                    checks["migration"] = True
            except Exception as mig_err:
                logger.warning("readiness_check_migration_query_failed", error=str(mig_err))
                if settings.ENVIRONMENT == "production":
                    is_ready = False
                    checks["migration"] = False
                else:
                    checks["migration"] = "skipped_non_prod"
    except Exception as e:
        logger.error("readiness_check_database_failed", error=str(e))
        is_ready = False

    # 2. Check Redis
    try:
        redis = get_redis_client()
        if redis:
            await redis.ping()
            checks["redis"] = True
        else:
            if settings.ENVIRONMENT == "production":
                checks["redis"] = False
                is_ready = False
            else:
                checks["redis"] = "in_memory_fallback"
    except Exception as e:
        logger.warning("readiness_check_redis_warning", error=str(e))
        if settings.ENVIRONMENT == "production":
            checks["redis"] = False
            is_ready = False
        else:
            checks["redis"] = "in_memory_fallback"

    # 3. Check Queue Availability
    try:
        get_job_manager()
        checks["queue"] = True
    except Exception as e:
        logger.error("readiness_check_queue_failed", error=str(e))
        is_ready = False

    # 4. Check Browser Pool
    try:
        get_browser_manager()
        checks["browser_pool"] = True
    except Exception as e:
        logger.error("readiness_check_browser_failed", error=str(e))
        is_ready = False

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if is_ready else "not_ready",
        "components": checks,
    }


@router.get("/metrics", summary="Prometheus Metrics")
async def metrics_endpoint() -> Response:
    data, content_type = get_latest_metrics()
    return Response(content=data, media_type=content_type)

