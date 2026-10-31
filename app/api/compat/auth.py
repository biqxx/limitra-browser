from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def verify_internal_secret(
    x_internal_service_secret: str | None = Header(None, alias="X-Internal-Service-Secret"),
) -> None:
    settings = get_settings()
    if not x_internal_service_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing internal service secret"},
        )

    expected = settings.INTERNAL_SERVICE_SECRET.strip().encode("utf-8")
    provided = x_internal_service_secret.strip().encode("utf-8")

    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid internal service secret"},
        )
