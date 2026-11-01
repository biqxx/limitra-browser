from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from app.api.errors import (
    OfficialApiUnavailableException,
    ProductNotFoundException,
    SupplierRateLimitedException,
)
from app.config import get_settings
from app.schemas.common import FieldProvenance, utc_now
from app.schemas.products import (
    NormalizedProduct,
    ProductAvailability,
    ProductImage,
    SellerInfo,
)
from app.schemas.search import ProductSearchResponse, SearchResultItem
from app.suppliers.amazon.marketplaces import get_marketplace

logger = structlog.get_logger(__name__)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.replace("%", "").replace(",", "").strip()
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return default


class AmazonApiClient:
    """
    Client interface for Amazon Product Advertising API (PA-API 5.0).
    Uses AWS Signature Version 4 for signing requests.
    Credentials are completely optional; if not present, returns clear configuration error.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.AMAZON_API_ENABLED
            and self.settings.AMAZON_ACCESS_KEY
            and self.settings.AMAZON_SECRET_KEY
            and self.settings.AMAZON_PARTNER_TAG
        )

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _get_signature_key(
        self, key: str, date_stamp: str, region_name: str, service_name: str
    ) -> bytes:
        k_date = self._sign(("AWS4" + key).encode("utf-8"), date_stamp)
        k_region = self._sign(k_date, region_name)
        k_service = self._sign(k_region, service_name)
        k_signing = self._sign(k_service, "aws4_request")
        return k_signing

    async def search_items(
        self,
        query: str,
        page: int = 1,
        page_size: int = 10,
        currency: str = "USD",
        locale: str = "en-US",
        country: str = "US",
    ) -> ProductSearchResponse:
        if not self.is_configured:
            raise OfficialApiUnavailableException(
                "amazon",
                "Amazon PA-API 5.0 credentials (AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_PARTNER_TAG) are not configured.",
            )

        # PA-API 5.0 SearchItems operation implementation
        marketplace = get_marketplace(country)
        host = marketplace.paapi_host
        endpoint = f"https://{host}/paapi5/searchitems"

        payload = {
            "Keywords": query,
            "Resources": [
                "ItemInfo.Title",
                "ItemInfo.Features",
                "Offers.Listings.Price",
                "Images.Primary.Large",
                "CustomerReviews.StarRating",
                "CustomerReviews.Count",
            ],
            "PartnerTag": self.settings.AMAZON_PARTNER_TAG,
            "PartnerType": "Associates",
            "ItemPage": page,
            "ItemCount": min(page_size, 10),
            "Marketplace": marketplace.base_url,
        }

        body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        async with httpx.AsyncClient() as client:
            headers = self._build_aws_headers(
                body_bytes,
                "SearchItems",
                host=host,
                region=marketplace.paapi_region,
            )
            resp = await client.post(endpoint, content=body_bytes, headers=headers, timeout=15.0)

            if resp.status_code == 429:
                raise SupplierRateLimitedException(
                    "Amazon PA-API rate limit exceeded.", provider="amazon"
                )
            if resp.status_code != 200:
                raise OfficialApiUnavailableException(
                    "amazon", f"SearchItems failed with HTTP {resp.status_code}"
                )

            data = resp.json()
            items: list[SearchResultItem] = []
            for raw in data.get("SearchResult", {}).get("Items", []):
                asin = raw.get("ASIN", "")
                title = raw.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
                detail_url = raw.get("DetailPageURL", f"{marketplace.base_url}/dp/{asin}")
                img_url = raw.get("Images", {}).get("Primary", {}).get("Large", {}).get("URL")

                # Price
                listing = (
                    raw.get("Offers", {}).get("Listings", [{}])[0]
                    if raw.get("Offers", {}).get("Listings")
                    else {}
                )
                price_val = _safe_float(listing.get("Price", {}).get("Amount"))
                price_curr = listing.get("Price", {}).get("Currency")

                items.append(
                    SearchResultItem(
                        provider="amazon",
                        external_id=asin,
                        title=title,
                        url=detail_url,
                        price=price_val,
                        currency=price_curr,
                        thumbnail_url=img_url,
                        in_stock=None,
                    )
                )

            total = data.get("SearchResult", {}).get("TotalResultCount")
            return ProductSearchResponse(
                provider="amazon",
                query=query,
                page=page,
                page_size=page_size,
                total_results=int(total) if total is not None else len(items),
                items=items,
                cache_status="miss",
            )

    async def get_items(
        self,
        asin: str,
        currency: str = "USD",
        locale: str = "en-US",
        country: str = "US",
    ) -> NormalizedProduct:
        if not self.is_configured:
            raise OfficialApiUnavailableException(
                "amazon",
                "Amazon PA-API 5.0 credentials (AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_PARTNER_TAG) are not configured.",
            )

        marketplace = get_marketplace(country)
        host = marketplace.paapi_host
        endpoint = f"https://{host}/paapi5/getitems"

        payload = {
            "ItemIds": [asin],
            "Resources": [
                "ItemInfo.Title",
                "ItemInfo.Features",
                "ItemInfo.ByLineInfo",
                "ItemInfo.Classifications",
                "ItemInfo.ProductInfo",
                "Offers.Listings.Price",
                "Offers.Listings.Availability",
                "Images.Primary.Large",
                "Images.Variants.Large",
                "CustomerReviews.StarRating",
                "CustomerReviews.Count",
            ],
            "PartnerTag": self.settings.AMAZON_PARTNER_TAG,
            "PartnerType": "Associates",
            "Marketplace": marketplace.base_url,
        }

        body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        async with httpx.AsyncClient() as client:
            headers = self._build_aws_headers(
                body_bytes,
                "GetItems",
                host=host,
                region=marketplace.paapi_region,
            )
            resp = await client.post(endpoint, content=body_bytes, headers=headers, timeout=15.0)

            if resp.status_code == 404:
                raise ProductNotFoundException(asin, provider="amazon")
            if resp.status_code == 429:
                raise SupplierRateLimitedException(
                    "Amazon PA-API rate limit exceeded.", provider="amazon"
                )
            if resp.status_code != 200:
                raise OfficialApiUnavailableException(
                    "amazon", f"GetItems failed with HTTP {resp.status_code}"
                )

            data = resp.json()
            items = data.get("ItemsResult", {}).get("Items", [])
            if not items:
                raise ProductNotFoundException(asin, provider="amazon")

            item = items[0]
            title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
            brand = (
                item.get("ItemInfo", {}).get("ByLineInfo", {}).get("Brand", {}).get("DisplayValue")
            )
            features = [
                f.get("DisplayValue", "")
                for f in item.get("ItemInfo", {}).get("Features", {}).get("DisplayValues", [])
            ]

            listing = (
                item.get("Offers", {}).get("Listings", [{}])[0]
                if item.get("Offers", {}).get("Listings")
                else {}
            )
            price_val = _safe_float(listing.get("Price", {}).get("Amount"))
            price_curr = listing.get("Price", {}).get("Currency")

            # Images
            images: list[ProductImage] = []
            prim_img = item.get("Images", {}).get("Primary", {}).get("Large", {}).get("URL")
            if prim_img:
                images.append(ProductImage(url=prim_img, position=0))
            for idx, var_img in enumerate(item.get("Images", {}).get("Variants", []), start=1):
                u = var_img.get("Large", {}).get("URL")
                if u:
                    images.append(ProductImage(url=u, position=idx))

            reviews = item.get("CustomerReviews", {})
            rating = _safe_float(reviews.get("StarRating", {}).get("Value")) or None
            review_count = _safe_int(reviews.get("Count")) or None

            provenance = {
                "title": FieldProvenance(source="official_api", confidence=0.99),
                "features": FieldProvenance(source="official_api", confidence=0.99),
            }
            if price_val is not None:
                provenance["current_price"] = FieldProvenance(
                    source="official_api", confidence=0.99
                )

            return NormalizedProduct(
                provider="amazon",
                external_id=asin,
                canonical_url=item.get("DetailPageURL", f"{marketplace.base_url}/dp/{asin}"),
                locale=locale,
                country=country,
                title=title,
                brand=brand,
                description="\n".join(features),
                bullet_points=features,
                current_price=price_val,
                currency=price_curr,
                availability=ProductAvailability.UNKNOWN,
                rating=rating,
                review_count=review_count,
                condition="new",
                seller=SellerInfo(name="Amazon"),
                images=images,
                identifiers={"asin": asin},
                source_metadata=provenance,
                retrieved_at=utc_now(),
            )

    def _build_aws_headers(
        self,
        payload_bytes: bytes,
        target_action: str,
        timestamp: datetime | None = None,
        host: str | None = None,
        region: str | None = None,
    ) -> dict[str, str]:
        # AWS Signature v4 header construction using exact payload bytes
        t = timestamp or datetime.now(timezone.utc)

        amz_date = t.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = t.strftime("%Y%m%d")

        host = host or self.settings.AMAZON_HOST
        region = region or self.settings.AMAZON_REGION
        service = "ProductAdvertisingAPI"
        content_type = "application/json; charset=UTF-8"
        target = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{target_action}"

        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        canonical_headers = (
            f"content-encoding:amz-1.0\n"
            f"content-type:{content_type}\n"
            f"host:{host}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{target}\n"
        )
        signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"

        canonical_request = (
            f"POST\n/paapi5/{target_action.lower()}\n\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"{algorithm}\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = self._get_signature_key(
            self.settings.AMAZON_SECRET_KEY, date_stamp, region, service
        )
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        authorization_header = (
            f"{algorithm} Credential={self.settings.AMAZON_ACCESS_KEY}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        return {
            "Content-Type": content_type,
            "Content-Encoding": "amz-1.0",
            "X-Amz-Date": amz_date,
            "X-Amz-Target": target,
            "Authorization": authorization_header,
            "Host": host,
        }
