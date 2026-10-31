from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Metrics definitions
SUPPLIER_REQUESTS_TOTAL = Counter(
    "supplier_requests_total",
    "Total supplier requests processed",
    ["provider", "operation", "status"],
)

SUPPLIER_REQUEST_DURATION_SECONDS = Histogram(
    "supplier_request_duration_seconds",
    "Duration of supplier requests in seconds",
    ["provider", "operation", "strategy"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

BROWSER_POOL_ACTIVE_CONTEXTS = Gauge(
    "browser_pool_active_contexts",
    "Number of currently active browser contexts in pool",
    ["provider"],
)

BROWSER_POOL_RECYCLES_TOTAL = Counter(
    "browser_pool_recycles_total",
    "Total browser process recycles",
    ["reason"],
)

JOB_QUEUE_DEPTH = Gauge(
    "job_queue_depth",
    "Current number of jobs in queue",
    ["queue_name"],
)

CACHE_OPERATIONS_TOTAL = Counter(
    "cache_operations_total",
    "Total cache lookups and writes",
    ["cache_type", "status"],  # hit, miss, bypass, write
)

SUPPLIER_BLOCKS_TOTAL = Counter(
    "supplier_blocks_total",
    "Total blocked requests by supplier (anti-bot or CAPTCHA)",
    ["provider", "reason"],
)

SELECTOR_FAILURES_TOTAL = Counter(
    "selector_failures_total",
    "Failures during selector extraction",
    ["provider", "field"],
)


def get_latest_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
