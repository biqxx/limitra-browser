from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import utc_now


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class JobType(str, Enum):
    SEARCH = "search"
    PRODUCT = "product"


class JobProgress(BaseModel):
    stage: str = "queued"
    percentage: int = 0
    message: str | None = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: JobType
    status: JobStatus
    provider: str
    progress: JobProgress = Field(default_factory=JobProgress)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    idempotency_key: str | None = None
