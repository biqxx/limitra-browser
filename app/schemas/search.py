from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import utc_now


class SearchFilters(BaseModel):
    minimum_price: float | None = None
    maximum_price: float | None = None
    condition: list[str] | None = None
    free_shipping: bool | None = None
    brand: list[str] | None = None
    category: str | None = None


class SearchResultItem(BaseModel):
    provider: str
    external_id: str
    title: str
    url: str
    price: float
    original_price: float | None = None
    currency: str = "USD"
    thumbnail_url: str | None = None
    rating: float | None = None
    review_count: int | None = None
    condition: str = "new"
    seller_name: str | None = None
    free_shipping: bool = False
    in_stock: bool = True


class ProductSearchRequest(BaseModel):
    provider: str
    query: str
    country: str = "US"
    locale: str = "en-US"
    currency: str = "USD"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    filters: SearchFilters | None = None
    include_raw: bool = False
    force_refresh: bool = False


class ProductSearchResponse(BaseModel):
    provider: str
    query: str
    page: int
    page_size: int
    total_results: int | None = None
    items: list[SearchResultItem] = Field(default_factory=list)
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    retrieval_timestamp: datetime = Field(default_factory=utc_now)
    cache_status: str = "miss"  # "hit", "miss", "bypass"
    warnings: list[str] = Field(default_factory=list)
    partial_result: bool = False
