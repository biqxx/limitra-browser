from __future__ import annotations

import pytest

from app.config import Settings, validate_production_configuration


def test_validation_ignored_in_development():
    settings = Settings(
        ENVIRONMENT="development",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        API_KEY_SECRET="default",
    )
    # Should not raise
    validate_production_configuration(settings)


def test_validation_fails_on_default_secrets_in_production():
    settings = Settings(
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://postgres:pass@localhost:5432/db",
        REDIS_URL="redis://localhost:6379/0",
        API_KEY_SECRET="supplier-service-default-secret-salt-key-32ch",  # Default marker
        BOOTSTRAP_ADMIN_API_KEY="secure_prod_admin_key_1234567890",
        INTERNAL_SERVICE_SECRET="secure_prod_internal_key_1234567890",
        QUICK_BUY_SIGNING_SECRET="secure_prod_quickbuy_key_1234567890",
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_production_configuration(settings)
    assert "API_KEY_SECRET is using an insecure default placeholder" in str(exc_info.value)


def test_validation_fails_on_sqlite_in_production():
    settings = Settings(
        ENVIRONMENT="production",
        DATABASE_URL="sqlite+aiosqlite:///./supplier_browser.db",  # SQLite invalid for prod
        REDIS_URL="redis://localhost:6379/0",
        API_KEY_SECRET="secure_prod_api_key_secret_1234567890",
        BOOTSTRAP_ADMIN_API_KEY="secure_prod_admin_key_1234567890",
        INTERNAL_SERVICE_SECRET="secure_prod_internal_key_1234567890",
        QUICK_BUY_SIGNING_SECRET="secure_prod_quickbuy_key_1234567890",
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_production_configuration(settings)
    assert "DATABASE_URL cannot be SQLite in production" in str(exc_info.value)


def test_validation_passes_with_valid_production_settings():
    settings = Settings(
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://prod_user:strong_password@db-host:5432/supplier_prod",
        REDIS_URL="redis://redis-host:6379/0",
        API_KEY_SECRET="strong_prod_secret_salt_32_characters_long",
        BOOTSTRAP_ADMIN_API_KEY="strong_prod_admin_api_key_32_characters",
        INTERNAL_SERVICE_SECRET="strong_prod_internal_secret_32_chars",
        QUICK_BUY_SIGNING_SECRET="strong_prod_quickbuy_signing_secret_32ch",
    )
    # Valid production settings must pass cleanly
    validate_production_configuration(settings)
