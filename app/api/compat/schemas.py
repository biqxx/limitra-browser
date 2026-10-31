from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)



class LegacyVariant(BaseModel):
    id: str | None = None
    label: str
    value: str
    price: float | None = None
    available: bool | None = None


class LegacyProduct(BaseModel):
    id: int | None = None
    supplier_id: str
    supplier: str
    title: str
    description: str = ""
    price: float
    currency: str = "USD"
    image_url: str = ""
    product_url: str = ""
    available: bool = True
    variants: list[LegacyVariant] = Field(default_factory=list)
    variant_retrieval: bool = False
    fetched_at: datetime = Field(default_factory=utc_now)

    # Normalized supplier facts used by the catalog's pricing workflow (nullable when missing)
    weight_lb: float | None = None
    weight_kg: float | None = None
    us_delivery_days_min: int | None = None
    us_delivery_days_max: int | None = None


class LegacySearchResult(BaseModel):
    products: list[LegacyProduct] = Field(default_factory=list)
    total: int = 0
    page: int = 1


class LegacyUnifiedSearchResult(BaseModel):
    products: list[LegacyProduct] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    supplier: str = "all"
    warnings: list[str] | None = None


class AvailabilityRequest(BaseModel):
    supplier: str
    supplier_id: str


class AvailabilityResult(BaseModel):
    supplier: str
    supplier_id: str
    available: bool
    price: float
    checked_at: datetime = Field(default_factory=utc_now)


class ShippingAddress(BaseModel):
    full_name: str
    line1: str
    line2: str | None = None
    city: str
    state: str
    zip_code: str
    phone: str | None = None


class OrderPayload(BaseModel):
    supplier: str
    supplier_id: str
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1)
    address: ShippingAddress
    metadata: dict[str, str] = Field(default_factory=dict)


class OrderSession(BaseModel):
    session_id: str
    supplier: str
    supplier_id: str
    status: str = "pending_confirmation"
    order_payload: OrderPayload
    confirm_token: str
    created_at: datetime = Field(default_factory=utc_now)


class OrderResult(BaseModel):
    session_id: str
    supplier_order_ref: str
    status: str
    confirmed_at: datetime = Field(default_factory=utc_now)


class QuickBuyPayload(BaseModel):
    order_payload: OrderPayload
    nonce: str
    signature: str = ""
    expires_at: datetime


class QuickBuyVerifyResponse(BaseModel):
    valid: bool


class DependencyStatus(BaseModel):
    name: str
    status: str  # "ok" | "error" | "unknown"
    latency: str | None = None
    error: str | None = None


class DetailedHealthResponse(BaseModel):
    service: str = "limitra-supplier-service"
    status: str = "ok"  # "ok" | "degraded" | "error"
    dependencies: list[DependencyStatus] = Field(default_factory=list)
    checked_at: str


class LegacyHealthResponse(BaseModel):
    status: str = "ok"
    service: str = "limitra-supplier-service"
