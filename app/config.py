from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General & Service
    ENVIRONMENT: str = "development"
    SERVICE_NAME: str = "supplier-browser-service"
    LOG_LEVEL: str = "INFO"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    RUN_LIVE_SUPPLIER_TESTS: bool = False

    # Security & Authentication
    API_KEY_SECRET: str = "supplier-service-default-secret-salt-key-32ch"
    BOOTSTRAP_ADMIN_API_KEY: str = "adm_secret_bootstrap_key_12345"
    INTERNAL_SERVICE_SECRET: str = "supplier-service-default-secret-salt-key-32ch"
    QUICK_BUY_SIGNING_SECRET: str = "quickbuy-default-secret-signing-key-32ch"
    ENFORCE_API_KEYS: bool = True

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./supplier_browser.db"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_TIMEOUT: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAX_CONNECTIONS: int = 50

    # S3-Compatible Storage
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY_ID: str = "minioadmin"
    S3_SECRET_ACCESS_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "supplier-diagnostics"
    S3_REGION: str = "us-east-1"
    S3_USE_SSL: bool = False
    DIAGNOSTIC_MODE_ENABLED: bool = False
    DIAGNOSTIC_RETENTION_HOURS: int = 24

    # Browser Execution Pool
    BROWSER_HEADLESS: bool = True
    BROWSER_MAX_GLOBAL_CONCURRENCY: int = 8
    BROWSER_MAX_PER_SUPPLIER_CONCURRENCY: int = 3
    BROWSER_NAVIGATION_TIMEOUT_MS: int = 30000
    BROWSER_SELECTOR_TIMEOUT_MS: int = 10000
    BROWSER_TOTAL_TIMEOUT_MS: int = 45000
    BROWSER_RECYCLE_PAGE_LIMIT: int = 100
    BROWSER_RECYCLE_MEMORY_MB: int = 1024
    BROWSER_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    MAX_VARIANT_COMBINATIONS: int = 50

    # Queue & Worker Settings
    LEGACY_QUEUE_FALLBACK_ENABLED: bool = False
    MAX_JOB_ATTEMPTS: int = 3
    JOB_RETRY_BASE_BACKOFF_SECONDS: float = 1.0
    JOB_RETRY_MAX_BACKOFF_SECONDS: float = 30.0
    JOB_STALE_RUNNING_TIMEOUT_SECONDS: int = 180

    # Caching & Rate Limiting
    CACHE_SEARCH_TTL_SECONDS: int = 1800
    CACHE_PRODUCT_TTL_SECONDS: int = 86400
    CACHE_NEGATIVE_TTL_SECONDS: int = 3600
    CACHE_STALE_WHILE_REVALIDATE_SECONDS: int = 300

    RATE_LIMIT_EBAY_RPM: int = 60
    RATE_LIMIT_AMAZON_RPM: int = 30
    RATE_LIMIT_DEFAULT_RPM: int = 60
    CLIENT_DEFAULT_RPM: int = 120

    # Official Supplier API: eBay Browse API
    EBAY_API_ENABLED: bool = True
    EBAY_APP_ID: str = ""
    EBAY_CERT_ID: str = ""
    EBAY_ENVIRONMENT: str = "PRODUCTION"
    # Browser scraping of eBay is only a controlled, opt-in fallback for API *outages*.
    # It is never used after a 401/403 or when credentials are missing.
    EBAY_BROWSER_FALLBACK_ENABLED: bool = False

    # Official Supplier API: Amazon Product Advertising API
    AMAZON_API_ENABLED: bool = False
    AMAZON_ACCESS_KEY: str = ""
    AMAZON_SECRET_KEY: str = ""
    AMAZON_PARTNER_TAG: str = ""
    AMAZON_HOST: str = "webservices.amazon.com"
    AMAZON_REGION: str = "us-east-1"

    # SSRF & Network Protection
    ALLOWED_CUSTOM_DEV_HOSTS: str = "localhost,127.0.0.1,testserver"
    ENFORCE_HTTPS_ONLY: bool = True
    MAX_REDIRECT_HOPS: int = 5
    MAX_URL_LENGTH: int = 2048

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    OTEL_SERVICE_NAME: str = "supplier-browser-service"
    METRICS_ENABLED: bool = True

    @property
    def allowed_dev_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_CUSTOM_DEV_HOSTS.split(",") if h.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def validate_production_configuration(settings: Settings | None = None) -> None:
    """
    Validates that production environment has secure secrets, external database,
    and valid redis url configured. Refuses to run if any default or insecure values exist.
    """
    s = settings or get_settings()
    if s.ENVIRONMENT.lower() != "production":
        return

    errors: list[str] = []

    # Check secrets are not default or insecurely short
    default_markers = ["default", "secret-salt", "adm_secret_bootstrap", "quickbuy-default"]

    for name, val in [
        ("API_KEY_SECRET", s.API_KEY_SECRET),
        ("BOOTSTRAP_ADMIN_API_KEY", s.BOOTSTRAP_ADMIN_API_KEY),
        ("INTERNAL_SERVICE_SECRET", s.INTERNAL_SERVICE_SECRET),
        ("QUICK_BUY_SIGNING_SECRET", s.QUICK_BUY_SIGNING_SECRET),
    ]:
        if not val or len(val.strip()) < 16:
            errors.append(
                f"{name} must be set to a secure string with at least 16 characters in production."
            )
        elif any(marker in val for marker in default_markers):
            errors.append(
                f"{name} is using an insecure default placeholder value in production."
            )

    # Check database URL
    if s.DATABASE_URL.startswith("sqlite"):
        errors.append(
            "DATABASE_URL cannot be SQLite in production. PostgreSQL (postgresql+asyncpg://...) is required."
        )

    # Check Redis URL
    if not (s.REDIS_URL.startswith("redis://") or s.REDIS_URL.startswith("rediss://")):
        errors.append(
            "REDIS_URL must be a valid redis:// or rediss:// connection string in production."
        )

    if errors:
        msg = "PRODUCTION CONFIGURATION FATAL ERRORS:\n" + "\n".join(f"- {e}" for e in errors)
        raise RuntimeError(msg)
