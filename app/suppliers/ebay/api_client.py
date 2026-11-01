from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import datetime, timezone
from typing import Any, cast

import httpx
import structlog

from app.api.errors import (
    OfficialApiUnavailableException,
    ProductNotFoundException,
    SupplierAccessForbiddenException,
    SupplierAuthenticationException,
    SupplierNotConfiguredException,
    SupplierRateLimitedException,
    SupplierTimeoutException,
)
from app.config import get_settings
from app.schemas.common import utc_now
from app.schemas.products import (
    DiscountInfo,
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

EBAY_ENV_VARS = ["EBAY_API_ENABLED", "EBAY_APP_ID", "EBAY_CERT_ID", "EBAY_ENVIRONMENT"]

# country -> Browse API marketplace id. ``X-EBAY-C-MARKETPLACE-ID`` must be one of these.
EBAY_MARKETPLACES: dict[str, str] = {
    "US": "EBAY_US",
    "GB": "EBAY_GB",
    "UK": "EBAY_GB",
    "DE": "EBAY_DE",
    "CA": "EBAY_CA",
    "AU": "EBAY_AU",
    "FR": "EBAY_FR",
    "IT": "EBAY_IT",
    "ES": "EBAY_ES",
    "NL": "EBAY_NL",
    "IE": "EBAY_IE",
    "AT": "EBAY_AT",
    "CH": "EBAY_CH",
    "PL": "EBAY_PL",
    "HK": "EBAY_HK",
    "SG": "EBAY_SG",
    "MY": "EBAY_MY",
    "PH": "EBAY_PH",
    "IN": "EBAY_IN",
}

# Browse API ``estimatedAvailabilityStatus`` -> availability. Anything else stays UNKNOWN.
_AVAILABILITY_MAP = {
    "IN_STOCK": ProductAvailability.IN_STOCK,
    "LIMITED_STOCK": ProductAvailability.LIMITED_STOCK,
    "OUT_OF_STOCK": ProductAvailability.OUT_OF_STOCK,
}


def ebay_marketplace_id(country: str | None) -> str:
    return EBAY_MARKETPLACES.get((country or "US").strip().upper(), "EBAY_US")


def _opt_float(val: Any) -> float | None:
    """Parse a numeric API value. Missing / unparsable -> None (never 0.0)."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _opt_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _money(obj: Any) -> tuple[float | None, str | None]:
    """Extract (amount, currency) from an eBay ``{"value": "12.3", "currency": "USD"}``.

    A price is only valid when BOTH value and currency are present and the value is > 0.
    """
    if not isinstance(obj, dict):
        return None, None
    value = _opt_float(obj.get("value"))
    currency = obj.get("currency")
    currency = str(currency).upper() if currency else None
    if value is None or value <= 0:
        return None, currency
    return value, currency


def _sanitize_ebay_error(resp: httpx.Response) -> str:
    """Extracts a sanitized summary from an eBay error response without leaking tokens or secrets."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "error_description" in data:
                return str(data["error_description"])[:300]
            if "errors" in data and isinstance(data["errors"], list) and data["errors"]:
                first = data["errors"][0]
                if isinstance(first, dict):
                    return str(
                        first.get("message")
                        or first.get("longMessage")
                        or first.get("errorId")
                        or "eBay API error"
                    )[:300]
            if "message" in data:
                return str(data["message"])[:300]
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


def _days_from_now(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (dt.date() - datetime.now(timezone.utc).date()).days)


def _availability(raw: dict[str, Any]) -> tuple[ProductAvailability, int | None]:
    """Map Browse ``estimatedAvailabilities``. No entry => UNKNOWN (never IN_STOCK)."""
    entries = raw.get("estimatedAvailabilities")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return ProductAvailability.UNKNOWN, None
    entry = entries[0]
    status = _AVAILABILITY_MAP.get(
        str(entry.get("estimatedAvailabilityStatus", "")).upper(), ProductAvailability.UNKNOWN
    )
    return status, _opt_int(entry.get("estimatedAvailableQuantity"))


def _shipping_options(raw: dict[str, Any], fallback_currency: str | None) -> list[ShippingOption]:
    options: list[ShippingOption] = []
    for opt in raw.get("shippingOptions", []) or []:
        if not isinstance(opt, dict):
            continue
        cost = opt.get("shippingCost")
        amount = _opt_float(cost.get("value")) if isinstance(cost, dict) else None
        cost_currency = (cost or {}).get("currency") if isinstance(cost, dict) else None
        # A *fixed* cost of 0 is the supplier explicitly saying "free". A missing cost
        # (e.g. CALCULATED shipping) stays None: we do not know it.
        explicit = str(opt.get("shippingCostType", "")).upper() == "FIXED" or amount is not None
        price = amount if (explicit and amount is not None) else None
        days_min = _days_from_now(opt.get("minEstimatedDeliveryDate"))
        days_max = _days_from_now(opt.get("maxEstimatedDeliveryDate"))
        options.append(
            ShippingOption(
                name=str(opt.get("shippingServiceCode") or opt.get("type") or "Shipping"),
                price=price,
                currency=str(cost_currency).upper() if cost_currency else fallback_currency,
                estimated_days_min=days_min,
                estimated_days_max=days_max,
                free_shipping=(price == 0.0) if price is not None else None,
            )
        )
    return options


def _images(raw: dict[str, Any]) -> list[ProductImage]:
    images: list[ProductImage] = []
    main = (raw.get("image") or {}).get("imageUrl") if isinstance(raw.get("image"), dict) else None
    if main:
        images.append(ProductImage(url=main, position=0))
    for add in raw.get("additionalImages", []) or []:
        if isinstance(add, dict) and add.get("imageUrl"):
            images.append(ProductImage(url=add["imageUrl"], position=len(images)))
    return images


def _variant_from_item(raw_v: dict[str, Any], fallback_currency: str | None) -> ProductVariant:
    raw_item_id = raw_v.get("itemId") or raw_v.get("sku")
    v_id = str(raw_item_id).strip() if raw_item_id else None
    v_price, v_curr = _money(raw_v.get("price"))

    opts: dict[str, str] = {}
    for asp in (raw_v.get("variationAspects") or raw_v.get("localizedAspects") or []):
        if isinstance(asp, dict) and asp.get("name") and asp.get("value"):
            opts[str(asp["name"])] = str(asp["value"])

    v_imgs: list[ProductImage] = []
    for img in raw_v.get("images", []) or []:
        if isinstance(img, dict) and img.get("imageUrl"):
            v_imgs.append(ProductImage(url=img["imageUrl"]))
    if not v_imgs:
        v_imgs.extend(_images(raw_v))

    stock = ProductAvailability.UNKNOWN
    qty: int | None = None
    status_raw = str(raw_v.get("stockStatus", "")).upper()
    if status_raw == "IN_STOCK":
        stock = ProductAvailability.IN_STOCK
    elif status_raw in ("OUT_OF_STOCK", "UNAVAILABLE"):
        stock = ProductAvailability.OUT_OF_STOCK
    elif raw_v.get("estimatedAvailabilities"):
        stock, qty = _availability(raw_v)

    return ProductVariant(
        external_id=v_id,
        title=None,
        selected_options=opts,
        price=v_price,
        currency=v_curr or (fallback_currency if v_price is not None else None),
        availability=stock,
        available_quantity=qty,
        images=v_imgs,
        canonical_url=raw_v.get("itemWebUrl"),
    )


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

    def require_configured(self) -> None:
        """Raise a specific, structured configuration error when the API cannot be used."""
        if not self.settings.EBAY_API_ENABLED:
            raise SupplierNotConfiguredException(
                "ebay", "the official eBay Browse API is disabled (EBAY_API_ENABLED).", EBAY_ENV_VARS
            )
        if not self.settings.EBAY_APP_ID or not self.settings.EBAY_CERT_ID:
            raise SupplierNotConfiguredException(
                "ebay",
                "eBay credentials are missing (EBAY_APP_ID / EBAY_CERT_ID).",
                EBAY_ENV_VARS,
            )

    async def close(self) -> None:
        """Closes any underlying resources."""
        pass

    @property
    def base_url(self) -> str:
        if self.settings.EBAY_ENVIRONMENT.upper() == "SANDBOX":
            return "https://api.sandbox.ebay.com"
        return "https://api.ebay.com"

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        """
        Retrieves or refreshes OAuth 2.0 application token using HTTP Basic authentication.
        Task safe with asyncio.Lock. Caches token until 5 minutes before expiry.
        Credentials and authorization headers are never logged.
        """
        now = time.time()
        if self._token and self._token_expires_at > (now + 300):
            return self._token

        async with self._token_lock:
            now = time.time()
            if self._token and self._token_expires_at > (now + 300):
                return self._token

            self.require_configured()

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
            except httpx.TimeoutException as e:
                raise SupplierTimeoutException(
                    "eBay OAuth token request timed out.", provider="ebay"
                ) from e
            except httpx.RequestError as e:
                logger.error("ebay_token_network_error", error_type=type(e).__name__)
                raise OfficialApiUnavailableException(
                    "ebay", f"Token request network error ({type(e).__name__})"
                ) from e

            if resp.status_code in (400, 401):
                sanitized = _sanitize_ebay_error(resp)
                logger.warning(
                    "ebay_oauth_authentication_failed", status=resp.status_code, error=sanitized
                )
                raise SupplierAuthenticationException("ebay", sanitized, resp.status_code)

            if resp.status_code == 403:
                sanitized = _sanitize_ebay_error(resp)
                raise SupplierAccessForbiddenException("ebay", sanitized, 403)

            if resp.status_code == 429:
                raise SupplierRateLimitedException(
                    "eBay OAuth rate limit exceeded.", provider="ebay", retry_after_seconds=30
                )

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

    def _raise_for_status(self, resp: httpx.Response, item_id: str | None = None) -> None:
        """Map eBay HTTP statuses to stable, sanitised exceptions."""
        status = resp.status_code
        if status == 200:
            return
        sanitized = _sanitize_ebay_error(resp)
        if status == 401:
            self._invalidate_token()
            raise SupplierAuthenticationException("ebay", sanitized, 401)
        if status == 403:
            raise SupplierAccessForbiddenException("ebay", sanitized, 403)
        if status == 404:
            raise ProductNotFoundException(item_id or "unknown", provider="ebay")
        if status == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            retry_after = (
                int(retry_after_raw) if retry_after_raw and str(retry_after_raw).isdigit() else 30
            )
            raise SupplierRateLimitedException(
                "eBay official API rate limit exceeded.",
                provider="ebay",
                retry_after_seconds=retry_after,
            )
        if status >= 500:
            raise OfficialApiUnavailableException(
                "ebay", f"eBay API server error ({status}): {sanitized}"
            )
        raise OfficialApiUnavailableException(
            "ebay", f"eBay API request failed with HTTP {status}: {sanitized}"
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        country: str,
        params: dict[str, str | int] | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        token = await self._get_access_token(client)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": ebay_marketplace_id(country),
            "Accept": "application/json",
        }
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=15.0)
        except httpx.TimeoutException as e:
            raise SupplierTimeoutException("eBay API request timed out.", provider="ebay") from e
        except httpx.RequestError as e:
            raise OfficialApiUnavailableException(
                "ebay", f"eBay API network error ({type(e).__name__})"
            ) from e
        self._raise_for_status(resp, item_id)
        data = resp.json()
        return data if isinstance(data, dict) else {}

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
            offset = max(0, (page - 1) * page_size)
            data = await self._get(
                client,
                f"{self.base_url}/buy/browse/v1/item_summary/search",
                country,
                params={"q": query, "limit": page_size, "offset": offset},
            )

            items: list[SearchResultItem] = []
            for raw in data.get("itemSummaries", []) or []:
                if not isinstance(raw, dict):
                    continue
                price, item_currency = _money(raw.get("price"))
                warnings: list[str] = []
                if price is None:
                    warnings.append("supplier did not provide a price for this listing")
                if raw.get("priceDisplayCondition"):
                    warnings.append(
                        "supplier price display condition: " + str(raw["priceDisplayCondition"])
                    )
                if str(raw.get("itemGroupType", "")).upper() == "SELLER_DEFINED_VARIATIONS":
                    warnings.append("multi-variation listing; open details for variation prices")

                options = _shipping_options(raw, item_currency)
                free_shipping: bool | None = None
                if options:
                    known = [o for o in options if o.free_shipping is not None]
                    if any(o.free_shipping for o in known):
                        free_shipping = True
                    elif known and len(known) == len(options):
                        free_shipping = False

                seller = (
                    cast(dict[str, Any], raw.get("seller"))
                    if isinstance(raw.get("seller"), dict)
                    else {}
                )
                img = (
                    cast(dict[str, Any], raw.get("image"))
                    if isinstance(raw.get("image"), dict)
                    else {}
                )
                items.append(
                    SearchResultItem(
                        provider="ebay",
                        external_id=str(raw.get("itemId", "")),
                        title=str(raw.get("title", "")),
                        url=str(raw.get("itemWebUrl", "")),
                        price=price,
                        currency=item_currency if price is not None else None,
                        thumbnail_url=img.get("imageUrl"),
                        condition=(str(raw["condition"]).lower() if raw.get("condition") else None),
                        seller_name=seller.get("username"),
                        free_shipping=free_shipping,
                        in_stock=None,  # search summaries carry no stock information
                        warnings=warnings,
                    )
                )

            total_results = data.get("total")
            return ProductSearchResponse(
                provider="ebay",
                query=query,
                page=page,
                page_size=page_size,
                total_results=_opt_int(total_results),
                items=items,
                applied_filters={},
                retrieval_timestamp=utc_now(),
                cache_status="miss",
            )

    async def _fetch_group_variants(
        self,
        client: httpx.AsyncClient,
        group_id: str,
        country: str,
        fallback_currency: str | None,
    ) -> list[ProductVariant]:
        data = await self._get(
            client,
            f"{self.base_url}/buy/browse/v1/item/get_items_by_item_group",
            country,
            params={"item_group_id": group_id},
        )
        return [
            _variant_from_item(raw, fallback_currency)
            for raw in data.get("items", []) or []
            if isinstance(raw, dict)
        ]

    async def get_item(
        self,
        item_id: str,
        country: str = "US",
        currency: str = "USD",
        locale: str = "en-US",
    ) -> NormalizedProduct:
        async with httpx.AsyncClient() as client:
            data = await self._get(
                client,
                f"{self.base_url}/buy/browse/v1/item/{item_id}",
                country,
                item_id=item_id,
            )

            current_price, item_currency = _money(data.get("price"))
            warnings: list[str] = []
            if current_price is None:
                warnings.append("eBay did not provide a price for this item")

            # Original price / discount only when eBay explicitly provides them.
            original_price: float | None = None
            discount: DiscountInfo | None = None
            marketing = data.get("marketingPrice")
            if isinstance(marketing, dict):
                original_price, _ = _money(marketing.get("originalPrice"))
                pct = _opt_float(marketing.get("discountPercentage"))
                amt, _ = _money(marketing.get("discountAmount"))
                if pct is not None or amt is not None:
                    discount = DiscountInfo(percentage=pct, amount=amt)

            images = _images(data)

            specifications: dict[str, str] = {}
            for row in data.get("localizedAspects", []) or []:
                if isinstance(row, dict) and row.get("name") and row.get("value"):
                    specifications[str(row["name"])] = str(row["value"])

            shipping_options = _shipping_options(data, item_currency)

            seller_data = (
                cast(dict[str, Any], data.get("seller"))
                if isinstance(data.get("seller"), dict)
                else {}
            )
            seller = SellerInfo(
                name=str(seller_data.get("username") or "Unknown Seller"),
                rating=_opt_float(seller_data.get("feedbackPercentage")),
                review_count=_opt_int(seller_data.get("feedbackScore")),
            )

            availability, available_qty = _availability(data)

            # Variations. (1) inline `itemVariations`; (2) the official item-group endpoint.
            variants: list[ProductVariant] = [
                _variant_from_item(raw_v, item_currency)
                for raw_v in data.get("itemVariations", []) or []
                if isinstance(raw_v, dict)
            ]
            group = data.get("primaryItemGroup")
            group_id = (group or {}).get("itemGroupId") if isinstance(group, dict) else None
            if not variants and group_id:
                try:
                    variants = await self._fetch_group_variants(
                        client, str(group_id), country, item_currency
                    )
                except (ProductNotFoundException, OfficialApiUnavailableException) as e:
                    warnings.append(f"variation details could not be retrieved: {e.message}")

            # Category
            category: str | None = None
            category_path: list[str] = []
            if data.get("categoryPath"):
                category_path = [p.strip() for p in str(data["categoryPath"]).split("|") if p.strip()]
                category = category_path[-1] if category_path else None
            if not category and isinstance(data.get("categories"), list) and data["categories"]:
                first_cat = data["categories"][0]
                if isinstance(first_cat, dict):
                    category = first_cat.get("categoryName")

            identifiers: dict[str, str] = {}
            legacy_id = data.get("legacyItemId")
            identifiers["ebay_item_id"] = str(legacy_id) if legacy_id else str(item_id)
            for key in ("gtin", "mpn", "epid", "ean", "upc", "isbn"):
                value = data.get(key)
                if isinstance(value, list) and value:
                    value = value[0]
                if value:
                    identifiers[key] = str(value)

            review = data.get("reviewRating")
            rating = _opt_float(review.get("averageRating")) if isinstance(review, dict) else None
            review_count = _opt_int(review.get("reviewCount")) if isinstance(review, dict) else None

            description = data.get("description") or data.get("shortDescription") or ""
            condition = data.get("condition")
            returns = data.get("returnTerms")

            provenance = {
                "title": FieldProvenance(source="official_api", confidence=0.99),
                "description": FieldProvenance(source="official_api", confidence=0.99),
            }
            if current_price is not None:
                provenance["current_price"] = FieldProvenance(source="official_api", confidence=0.99)

            return NormalizedProduct(
                provider="ebay",
                external_id=item_id,
                canonical_url=data.get("itemWebUrl", f"https://www.ebay.com/itm/{item_id}"),
                locale=locale,
                country=country,
                title=str(data.get("title", "")),
                subtitle=data.get("subtitle"),
                brand=data.get("brand"),
                description=re.sub(r"\s+", " ", str(description)).strip() if description else "",
                category=category,
                category_path=category_path,
                current_price=current_price,
                original_price=original_price,
                discount=discount,
                currency=item_currency,
                availability=availability,
                available_quantity=available_qty,
                rating=rating,
                review_count=review_count,
                condition=str(condition).lower() if condition else None,
                seller=seller,
                images=images,
                specifications=specifications,
                variants=variants,
                shipping_options=shipping_options,
                return_policy=(returns or {}).get("returnsAcceptedText")
                if isinstance(returns, dict)
                else None,
                identifiers=identifiers,
                source_metadata=provenance,
                retrieved_at=utc_now(),
                variant_retrieval=bool(len(variants) > 0),
                warnings=warnings,
            )
