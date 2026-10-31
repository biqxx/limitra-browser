from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.products import NormalizedProduct, ProductRetrievalRequest
from app.schemas.search import ProductSearchRequest, ProductSearchResponse


def extract_json_object_after_key(text: str, key: str) -> dict[str, Any] | None:
    """
    Finds `key` in text, locates the opening `{` after it,
    and tracks brace depth to extract valid JSON without regex nesting limitations.
    """
    idx = text.find(key)
    if idx == -1:
        return None
    start = text.find("{", idx + len(key))
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        import json

                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                        return None
                    except Exception:
                        return None
    return None


class SupplierExecutionContext(BaseModel):
    job_id: str | None = None
    locale: str = "en-US"
    country: str = "US"
    currency: str = "USD"
    include_variants: bool = True
    include_shipping: bool = True
    include_raw: bool = False
    force_refresh: bool = False
    browser_manager: Any | None = None

    model_config = {"arbitrary_types_allowed": True}


class SupplierHealth(BaseModel):
    provider: str
    status: str = "healthy"  # "healthy", "degraded", "unhealthy"
    official_api_configured: bool = False
    official_api_healthy: bool = False
    browser_fallback_available: bool = True
    supported_domains: list[str] = Field(default_factory=list)
    supported_locales: list[str] = Field(default_factory=list)
    error_message: str | None = None


class SupplierAdapter(ABC):
    """
    Contract interface for isolated supplier adapters.
    All supplier-specific logic must be contained in the adapter.
    """

    provider: str
    supported_domains: set[str]
    supported_locales: set[str] = {"en-US"}
    converter_version: int = 1

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "product_search": True,
            "product_retrieval": True,
            "variant_retrieval": True,
            "shipping_information": True,
            "seller_information": True,
            "reviews_summary": True,
            "official_api_support": False,
            "browser_fallback_support": True,
        }

    @abstractmethod
    async def search(
        self,
        request: ProductSearchRequest,
        context: SupplierExecutionContext,
    ) -> ProductSearchResponse:
        """Search products on supplier platform."""
        ...

    @abstractmethod
    async def get_product(
        self,
        request: ProductRetrievalRequest,
        context: SupplierExecutionContext,
    ) -> NormalizedProduct:
        """Retrieve complete normalized product from supplier platform."""
        ...

    @abstractmethod
    def recognizes_url(self, url: str) -> bool:
        """Determines if the given URL belongs to this supplier."""
        ...

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Normalizes supplier product URL into clean canonical format."""
        ...

    @abstractmethod
    def extract_external_id(self, url: str) -> str | None:
        """Extracts supplier unique product identifier from a URL."""
        ...

    @abstractmethod
    async def healthcheck(self) -> SupplierHealth:
        """Performs health check on adapter components."""
        ...
