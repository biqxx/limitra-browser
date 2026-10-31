from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
import structlog

from app.api.errors import (
    ForbiddenException,
    OfficialApiUnavailableException,
    ProductNotFoundException,
    SupplierRateLimitedException,
    UnauthorizedException,
)
from app.config import get_settings
from app.schemas.common import utc_now
from app.schemas.products import (
    FieldProvenance,
    NormalizedProduct,
    ProductAvailability,
    ProductImage,
    ProductVariant,
    SellerInfo,
    ShippingOption,
)
from app.schemas.search import ProductSearchResponse, SearchResultItem

logger = structlog.get_logger(__name__)


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _sanitize_ebay_error(resp: httpx.Response) -> str:
    """Extracts a sanitized summary from an eBay error response without leaking tokens or secrets."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "error_description" in data:
                return str(data["error_description"])
            if "errors" in data and isinstance(data["errors"], list) and data["errors"]:
                first = data["errors"][0]
                if isinstance(first, dict):
                    return str(first.get("message") or first.get("longMessage") or first.get("errorId") or "eBay API error")
            if "message" in data:
                return str(data["message"])
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


class EbayApiClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.EBAY_API_ENABLED
            and self.settings.EBAY_APP_ID
            and self.settings.EBAY_CERT_ID
        )

    async def close(self) -> None:
        """Closes any underlying resources."""
        pass


    @property
    def base_url(self) -> str:
        if self.settings.EBAY_ENVIRONMENT.upper() == "SANDBOX":
            return "https://api.sandbox.ebay.com"
        return "https://api.ebay.com"

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        """
        Retrieves or refreshes OAuth 2.0 application token using HTTP Basic authentication.
        Thread/task safe with asyncio.Lock. Caches token until 5 minutes before expiry.
        Redacts credentials and authorization headers from logs.
        """
        now = time.time()
        # Fast path check under existing cached token
        if self._token and self._token_expires_at > (now + 300):
            return self._token

        async with self._token_lock:
            # Re-check inside lock
            now = time.time()
            if self._token and self._token_expires_at > (now + 300):
                return self._token

            if not self.is_configured:
                raise OfficialApiUnavailableException(
                    "ebay", "Credentials (EBAY_APP_ID / EBAY_CERT_ID) not configured."
                )

            auth_str = f"{self.settings.EBAY_APP_ID}:{self.settings.EBAY_CERT_ID}"
            encoded_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")

            headers = {
                "Authorization": f"Basic {encoded_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            }
            token_url = f"{self.base_url}/identity/v1/oauth2/token"

            try:
                logger.info("requesting_ebay_oauth_token", endpoint="/identity/v1/oauth2/token")
                resp = await client.post(token_url, headers=headers, data=data, timeout=10.0)

                if resp.status_code == 401:
                    sanitized = _sanitize_ebay_error(resp)
                    logger.warning("ebay_oauth_authentication_failed", status=401, error=sanitized)
                    raise UnauthorizedException(f"eBay OAuth credentials rejected: {sanitized}")

                if resp.status_code != 200:
                    sanitized = _sanitize_ebay_error(resp)
                    logger.error("ebay_oauth_token_error", status=resp.status_code, error=sanitized)
                    raise OfficialApiUnavailableException(
                        "ebay", f"OAuth token request failed with HTTP {resp.status_code}: {sanitized}"
                    )

                body = resp.json()
                token = str(body["access_token"])
                expires_in = int(body.get("expires_in", 7200))

                self._token = token
                self._token_expires_at = now + expires_in
                logger.info("ebay_oauth_token_cached", expires_in_seconds=expires_in)
                return token

            except httpx.RequestError as e:
                logger.error("ebay_token_network_error", error=str(e))
                raise OfficialApiUnavailableException(
                    "ebay", f"Token request network error: {e}"
                ) from e

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
        country: str = "US",
        currency: str = "USD",
        locale: str = "en-US",
    ) -> ProductSearchResponse:
        async with httpx.AsyncClient() as client:
            token = await self._get_access_token(client)
            offset = max(0, (page - 1) * page_size)
            url = f"{self.base_url}/buy/browse/v1/item_summary/search"
            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": f"EBAY_{country.upper()}",
            }
            params: dict[str, str | int] = {
                "q": query,
                "limit": page_size,
                "offset": offset,
            }

            resp = await client.get(url, headers=headers, params=params, timeout=15.0)

            if resp.status_code == 401:
                # Token may have expired or been revoked; invalidate cache
                self._token = None
                self._token_expires_at = 0.0
                sanitized = _sanitize_ebay_error(resp)
                raise UnauthorizedException(f"eBay API unauthorized: {sanitized}")

            if resp.status_code == 403:
                sanitized = _sanitize_ebay_error(resp)
                raise ForbiddenException(f"eBay API access forbidden: {sanitized}")

            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After")
                retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else 30
                raise SupplierRateLimitedException(
                    "eBay official API rate limit exceeded.",
                    provider="ebay",
                    retry_after_seconds=retry_after,
                )

            if resp.status_code >= 500:
                sanitized = _sanitize_ebay_error(resp)
                raise OfficialApiUnavailableException(
                    "ebay", f"eBay API temporary server error ({resp.status_code}): {sanitized}"
                )

            if resp.status_code != 200:
                sanitized = _sanitize_ebay_error(resp)
                raise OfficialApiUnavailableException(
                    "ebay", f"Search failed with HTTP {resp.status_code}: {sanitized}"
                )

            data = resp.json()
            items: list[SearchResultItem] = []
            for raw in data.get("itemSummaries", []):
                price_dict = raw.get("price", {})
                current_price = _safe_float(price_dict.get("value"))
                item_currency = price_dict.get("currency", currency)

                img_dict = raw.get("image", {})
                img_url = img_dict.get("imageUrl")

                seller_info = raw.get("seller", {})
                free_shipping = False
                for ship in raw.get("shippingOptions", []):
                    cost = ship.get("shippingCost", {})
                    if _safe_float(cost.get("value")) == 0.0:
                        free_shipping = True
                        break

                items.append(
                    SearchResultItem(
                        provider="ebay",
                        external_id=raw.get("itemId", ""),
                        title=raw.get("title", ""),
                        url=raw.get("itemWebUrl", ""),
                        price=current_price,
                        currency=item_currency,
                        thumbnail_url=img_url,
                        seller_name=seller_info.get("username"),
                        free_shipping=free_shipping,
                        in_stock=True,
                    )
                )

            total_results = data.get("total")

            return ProductSearchResponse(
                provider="ebay",
                query=query,
                page=page,
                page_size=page_size,
                total_results=int(total_results) if total_results is not None else None,
                items=items,
                applied_filters={},
                retrieval_timestamp=utc_now(),
                cache_status="miss",
            )

    async def get_item(
        self,
        item_id: str,
        country: str = "US",
        currency: str = "USD",
        locale: str = "en-US",
    ) -> NormalizedProduct:
        async with httpx.AsyncClient() as client:
            token = await self._get_access_token(client)
            url = f"{self.base_url}/buy/browse/v1/item/{item_id}"
            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": f"EBAY_{country.upper()}",
            }

            resp = await client.get(url, headers=headers, timeout=15.0)

            if resp.status_code == 401:
                self._token = None
                self._token_expires_at = 0.0
                sanitized = _sanitize_ebay_error(resp)
                raise UnauthorizedException(f"eBay API unauthorized: {sanitized}")

            if resp.status_code == 403:
                sanitized = _sanitize_ebay_error(resp)
                raise ForbiddenException(f"eBay API access forbidden: {sanitized}")

            if resp.status_code == 404:
                raise ProductNotFoundException(item_id, provider="ebay")

            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After")
                retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else 30
                raise SupplierRateLimitedException(
                    "eBay official API rate limit exceeded.",
                    provider="ebay",
                    retry_after_seconds=retry_after,
                )

            if resp.status_code >= 500:
                sanitized = _sanitize_ebay_error(resp)
                raise OfficialApiUnavailableException(
                    "ebay", f"eBay API server error ({resp.status_code}): {sanitized}"
                )

            if resp.status_code != 200:
                sanitized = _sanitize_ebay_error(resp)
                raise OfficialApiUnavailableException(
                    "ebay", f"Item lookup failed with HTTP {resp.status_code}: {sanitized}"
                )

            data = resp.json()
            price_dict = data.get("price", {})
            current_price = _safe_float(price_dict.get("value"))
            item_currency = price_dict.get("currency", currency)

            # Images
            images: list[ProductImage] = []
            main_img = data.get("image", {}).get("imageUrl")
            if main_img:
                images.append(ProductImage(url=main_img, position=0))
            for idx, add_img in enumerate(data.get("additionalImages", []), start=1):
                u = add_img.get("imageUrl")
                if u:
                    images.append(ProductImage(url=u, position=idx))

            # Specifications
            specifications: dict[str, str] = {}
            for row in data.get("localizedAspects", []):
                k = row.get("name")
                v = row.get("value")
                if k and v:
                    specifications[k] = v

            # Shipping
            shipping_options: list[ShippingOption] = []
            for opt in data.get("shippingOptions", []):
                cost = opt.get("shippingCost", {})
                s_price = _safe_float(cost.get("value"))
                shipping_options.append(
                    ShippingOption(
                        name=opt.get("shippingServiceCode", "Standard"),
                        price=s_price,
                        currency=cost.get("currency", item_currency),
                        free_shipping=(s_price == 0.0),
                    )
                )

            seller_data = data.get("seller", {})
            seller = SellerInfo(
                name=seller_data.get("username", "eBay Seller"),
                rating=_safe_float(seller_data.get("feedbackPercentage")) or None,
                review_count=_safe_int(seller_data.get("feedbackScore")) or None,
            )

            # Variations from Browse API
            variants: list[ProductVariant] = []
            for idx, raw_v in enumerate(data.get("itemVariations", []), start=1):
                raw_item_id = raw_v.get("itemId") or raw_v.get("sku")
                v_id = str(raw_item_id).strip() if raw_item_id else None
                raw_p = raw_v.get("price", {}).get("value")
                v_price = _safe_float(raw_p) if raw_p is not None else None
                v_curr = raw_v.get("price", {}).get("currency", item_currency)
                v_opts = {}
                for asp in raw_v.get("variationAspects", []):
                    if asp.get("name") and asp.get("value"):
                        v_opts[asp["name"]] = asp["value"]
                v_imgs = [
                    ProductImage(url=img["imageUrl"])
                    for img in raw_v.get("images", [])
                    if img.get("imageUrl")
                ]
                stock_status = str(raw_v.get("stockStatus", "")).upper()
                if stock_status == "IN_STOCK":
                    v_stock = ProductAvailability.IN_STOCK
                elif stock_status in ("OUT_OF_STOCK", "UNAVAILABLE"):
                    v_stock = ProductAvailability.OUT_OF_STOCK
                else:
                    v_stock = ProductAvailability.UNKNOWN

                variants.append(
                    ProductVariant(
                        external_id=v_id,
                        title=f"Variant {idx}",
                        selected_options=v_opts,
                        price=v_price,
                        currency=v_curr,
                        availability=v_stock,
                        images=v_imgs,
                        canonical_url=raw_v.get("itemWebUrl"),
                    )
                )

            provenance = {
                "title": FieldProvenance(source="official_api", confidence=0.99),
                "current_price": FieldProvenance(source="official_api", confidence=0.99),
                "description": FieldProvenance(source="official_api", confidence=0.99),
            }

            return NormalizedProduct(
                provider="ebay",
                external_id=item_id,
                canonical_url=data.get("itemWebUrl", f"https://www.ebay.com/itm/{item_id}"),
                locale=locale,
                country=country,
                title=data.get("title", ""),
                brand=data.get("brand"),
                description=data.get("description", data.get("shortDescription", "")),
                current_price=current_price,
                currency=item_currency,
                availability=ProductAvailability.IN_STOCK
                if data.get("estimatedAvailabilities")
                else ProductAvailability.UNKNOWN,
                condition=data.get("condition", "new").lower(),
                seller=seller,
                images=images,
                specifications=specifications,
                variants=variants,
                shipping_options=shipping_options,
                return_policy=data.get("returnTerms", {}).get("returnsAcceptedText"),
                source_metadata=provenance,
                retrieved_at=utc_now(),
                variant_retrieval=bool(len(variants) > 0),
            )
