from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import FieldProvenance, utc_now


class ProductAvailability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    LIMITED_STOCK = "limited_stock"
    UNKNOWN = "unknown"


class PriceStatus(str, Enum):
    """Why a price is (or is not) present. Never inferred; always derived from supplier data."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    RANGE = "range"
    FREE = "free"


class DataQuality(BaseModel):
    """Structured completeness report consumed by the Laravel pricing/approval workflow."""

    complete: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    price_verified: bool = False
    currency_verified: bool = False
    images_verified: bool = False
    variants_complete: bool = False
    price_status: PriceStatus = PriceStatus.UNAVAILABLE
    requested_currency: str | None = None
    detected_currency: str | None = None
    price_range: dict[str, float] | None = None


class SellerInfo(BaseModel):
    name: str = "Unknown Seller"
    id: str | None = None
    rating: float | None = None
    review_count: int | None = None
    url: str | None = None
    verified: bool = False


class DiscountInfo(BaseModel):
    percentage: float | None = None
    amount: float | None = None


class ProductImage(BaseModel):
    url: str
    thumbnail_url: str | None = None
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None
    position: int = 0
    variant_ids: list[str] = Field(default_factory=list)


class ProductVideo(BaseModel):
    url: str
    thumbnail_url: str | None = None
    title: str | None = None


class ProductVariant(BaseModel):
    external_id: str | None = None
    sku: str | None = None
    title: str | None = None
    selected_options: dict[str, str] = Field(
        default_factory=dict, description="e.g. {'color': 'Blue', 'size': 'XL'}"
    )
    price: float | None = None
    original_price: float | None = None
    currency: str | None = None
    availability: ProductAvailability = ProductAvailability.UNKNOWN
    available_quantity: int | None = None
    images: list[ProductImage] = Field(default_factory=list)
    canonical_url: str | None = None


class ShippingOption(BaseModel):
    name: str = "Standard Shipping"
    price: float | None = None
    currency: str | None = None
    estimated_days_min: int | None = None
    estimated_days_max: int | None = None
    free_shipping: bool | None = None


class NormalizedProduct(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    external_id: str
    canonical_url: str
    locale: str = "en-US"
    country: str = "US"
    title: str
    subtitle: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    description: str = ""
    bullet_points: list[str] = Field(default_factory=list)
    category: str | None = None
    category_path: list[str] = Field(default_factory=list)
    condition: str | None = None
    seller: SellerInfo = Field(default_factory=SellerInfo)
    current_price: float | None = None
    original_price: float | None = None
    discount: DiscountInfo | None = None
    currency: str | None = None
    availability: ProductAvailability = ProductAvailability.UNKNOWN
    available_quantity: int | None = None
    rating: float | None = None
    review_count: int | None = None
    images: list[ProductImage] = Field(default_factory=list)
    videos: list[ProductVideo] = Field(default_factory=list)
    specifications: dict[str, str] = Field(default_factory=dict)
    variants: list[ProductVariant] = Field(default_factory=list)
    shipping_options: list[ShippingOption] = Field(default_factory=list)
    estimated_delivery: str | None = None
    return_policy: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    source_metadata: dict[str, FieldProvenance] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utc_now)
    variant_retrieval: bool = False
    price_range: dict[str, float] | None = None
    warnings: list[str] = Field(default_factory=list)
    data_quality: DataQuality = Field(default_factory=DataQuality)


class ProductRetrievalRequest(BaseModel):
    provider: str | None = None
    external_id: str | None = None
    url: str | None = None
    country: str = "US"
    locale: str = "en-US"
    currency: str = "USD"
    include_variants: bool = True
    include_shipping: bool = True
    include_raw: bool = False
    force_refresh: bool = False
