from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Header, Request
from pydantic import BaseModel

from app.api.errors import ForbiddenException, RateLimitExceededException, UnauthorizedException
from app.config import get_settings


class AuthenticatedClient(BaseModel):
    client_id: str
    name: str
    is_admin: bool = False
    rate_limit_rpm: int = 120
    is_active: bool = True


def hash_api_key(plain_key: str, secret: str | None = None) -> str:
    """
    Computes HMAC-SHA256 hash of API key using secret salt.
    Plaintext keys are never stored in databases or logs.
    """
    settings = get_settings()
    s = (secret or settings.API_KEY_SECRET).encode("utf-8")
    return hmac.new(s, plain_key.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def generate_new_api_key(prefix: str = "sk_live_") -> tuple[str, str]:
    """
    Generates a secure random API key and its hash.
    Returns: (plaintext_key_to_show_once, key_hash_to_store)
    """
    raw_token = secrets.token_urlsafe(32)
    plaintext_key = f"{prefix}{raw_token}"
    key_hash = hash_api_key(plaintext_key)
    return plaintext_key, key_hash


# In-memory token-bucket / sliding window for rate limiting (fallback when Redis is not active)
_IN_MEMORY_RATE_LIMITS: dict[str, list[float]] = {}


def check_sliding_window_rate_limit(key: str, max_requests: int, window_seconds: int = 60) -> bool:
    """
    Sliding window in-memory rate limiter.
    Returns True if request allowed, False if exceeded.
    """
    now = time.time()
    cutoff = now - window_seconds
    timestamps = _IN_MEMORY_RATE_LIMITS.setdefault(key, [])
    # purge old
    _IN_MEMORY_RATE_LIMITS[key] = [ts for ts in timestamps if ts > cutoff]
    if len(_IN_MEMORY_RATE_LIMITS[key]) >= max_requests:
        return False
    _IN_MEMORY_RATE_LIMITS[key].append(now)
    return True


async def authenticate_request(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    require_admin: bool = False,
) -> AuthenticatedClient:
    settings = get_settings()

    # If API keys are not enforced in development mode:
    if not settings.ENFORCE_API_KEYS:
        return AuthenticatedClient(
            client_id="dev-bypass",
            name="Development Bypass Client",
            is_admin=True,
            rate_limit_rpm=1000,
            is_active=True,
        )

    if not x_api_key:
        raise UnauthorizedException("Header 'X-API-Key' is required.")

    # Check for bootstrap admin key
    if settings.BOOTSTRAP_ADMIN_API_KEY and hmac.compare_digest(
        x_api_key.strip(), settings.BOOTSTRAP_ADMIN_API_KEY.strip()
    ):
        client = AuthenticatedClient(
            client_id="bootstrap-admin",
            name="Bootstrap Admin",
            is_admin=True,
            rate_limit_rpm=1000,
            is_active=True,
        )
    else:
        # Validate against database (via request state or db query)
        from sqlalchemy import select

        from app.database.models import ApiClient
        from app.database.session import get_db_session

        key_hash = hash_api_key(x_api_key)

        async with get_db_session() as session:
            stmt = select(ApiClient).where(ApiClient.key_hash == key_hash, ApiClient.is_active)
            res = await session.execute(stmt)
            db_client = res.scalar_one_or_none()

            if not db_client:
                raise UnauthorizedException("Invalid or inactive API key.")

            client = AuthenticatedClient(
                client_id=str(db_client.id),
                name=db_client.name,
                is_admin=db_client.is_admin,
                rate_limit_rpm=db_client.rate_limit_per_minute,
                is_active=db_client.is_active,
            )

    if require_admin and not client.is_admin:
        raise ForbiddenException("Administrator privileges required.")

    # Check client rate limit
    rl_key = f"rl:client:{client.client_id}"
    if not check_sliding_window_rate_limit(
        rl_key, max_requests=client.rate_limit_rpm, window_seconds=60
    ):
        raise RateLimitExceededException(
            f"Rate limit of {client.rate_limit_rpm} requests per minute exceeded."
        )

    # Attach client to request state
    request.state.client = client
    return client
