from __future__ import annotations

import json
import re
from typing import Any

import structlog
from bs4 import BeautifulSoup

from app.schemas.common import FieldProvenance, utc_now
from app.schemas.products import (
    NormalizedProduct,
    ProductAvailability,
    ProductImage,
    ProductVariant,
    SellerInfo,
    ShippingOption,
)
from app.schemas.search import SearchResultItem
from app.services.normalization_service import (
    FieldMergeCandidate,
    parse_availability,
    parse_price_and_currency,
    parse_rating_and_reviews,
    select_best_field,
)
from app.suppliers.base import extract_json_object_after_key
from app.suppliers.config_schema import SupplierConfigSchema

logger = structlog.get_logger(__name__)


def _to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def _to_optional_str(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]) if val else None
    s = str(val).strip()
    return s if s else None


class AmazonParser:
    def __init__(self, config: SupplierConfigSchema | None = None):
        self.config = config

    def parse_search_results(self, html: str, provider: str = "amazon") -> list[SearchResultItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[SearchResultItem] = []

        containers = soup.select("div[data-component-type='s-search-result']")
        if not containers:
            containers = soup.select("div.s-result-item[data-asin]")

        for container in containers:
            try:
                asin = _to_str(container.get("data-asin")).strip()
                if not asin:
                    continue

                # Title & link (prioritize true title text over badge spans like "Amazon Renewed")
                title_el = (
                    container.select_one("h2 a span.a-text-normal")
                    or container.select_one("h2 span.a-text-normal")
                    or container.select_one("h2 a span:not(.a-badge-text)")
                    or container.select_one("h2 a span")
                    or container.select_one("h2 span")
                    or container.select_one("h2")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)

                link_el = container.select_one("h2 a") or container.select_one("a.a-link-normal")
                href = _to_str(link_el.get("href")) if link_el else ""
                if href and not href.startswith("http"):
                    href = f"https://www.amazon.com{href}"
                elif not href:
                    href = f"https://www.amazon.com/dp/{asin}"

                # Price
                price_el = container.select_one(".a-price .a-offscreen") or container.select_one(
                    "span.a-price-whole"
                )
                raw_price = price_el.get_text(strip=True) if price_el else ""
                current_price, currency = parse_price_and_currency(raw_price)

                orig_price_el = container.select_one(".a-text-price .a-offscreen")
                original_price = None
                if orig_price_el:
                    original_price, _ = parse_price_and_currency(
                        orig_price_el.get_text(strip=True), default_currency=currency
                    )

                # Image
                img_el = container.select_one("img.s-image") or container.select_one("img")
                img_url = _to_optional_str(img_el.get("src")) if img_el else None

                # Rating & reviews
                rating_el = container.select_one(
                    "i.a-icon-star-small span.a-icon-alt"
                ) or container.select_one("span[aria-label*='stars']")
                rev_el = container.select_one(
                    "span[aria-label*='ratings']"
                ) or container.select_one("span.a-size-base.s-underline-text")
                rating, reviews = parse_rating_and_reviews(
                    rating_el.get_text(strip=True) if rating_el else None,
                    rev_el.get_text(strip=True) if rev_el else None,
                )

                items.append(
                    SearchResultItem(
                        provider=provider,
                        external_id=asin,
                        title=title,
                        url=href,
                        price=current_price or 0.0,
                        original_price=original_price,
                        currency=currency,
                        thumbnail_url=img_url,
                        rating=rating,
                        review_count=reviews,
                        condition="new",
                        seller_name="Amazon",
                        free_shipping=True if container.select_one(".s-prime") else False,
                        in_stock=True,
                    )
                )
            except Exception as e:
                logger.debug("amazon_search_item_parse_error", error=str(e))

        return items

    def parse_product_page(
        self,
        html: str,
        url: str,
        asin: str,
        locale: str = "en-US",
        country: str = "US",
        currency: str = "USD",
    ) -> NormalizedProduct:
        soup = BeautifulSoup(html, "html.parser")
        provenance: dict[str, FieldProvenance] = {}

        # 1. JSON-LD Extraction
        json_ld_data = self._extract_json_ld(soup)

        # 2. Title
        title_candidates = []
        if json_ld_data.get("name"):
            title_candidates.append(
                FieldMergeCandidate(json_ld_data["name"], source="json_ld", confidence=0.95)
            )

        title_el = soup.select_one("#productTitle") or soup.select_one("h1#title span")
        if title_el:
            title_candidates.append(
                FieldMergeCandidate(
                    title_el.get_text(strip=True),
                    source="css_selector",
                    selector="#productTitle",
                    confidence=0.90,
                )
            )

        title, p_title = select_best_field(title_candidates)
        if p_title:
            provenance["title"] = p_title

        # 3. Price & Currency
        price_candidates = []
        offers = json_ld_data.get("offers", {})
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if offers.get("price"):
            try:
                p_val = float(offers["price"])
                p_curr = offers.get("priceCurrency") or currency
                price_candidates.append(
                    FieldMergeCandidate((p_val, p_curr), source="json_ld", confidence=0.95)
                )
            except ValueError:
                pass

        price_el = (
            soup.select_one("#corePrice_feature_div .a-price .a-offscreen")
            or soup.select_one("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")
            or soup.select_one(".apexPriceToPay .a-offscreen")
            or soup.select_one("#priceblock_ourprice")
            or soup.select_one(".a-price .a-offscreen")
        )
        if price_el:
            p_val_num, p_curr_num = parse_price_and_currency(
                price_el.get_text(strip=True), default_currency=currency
            )
            if p_val_num is not None:
                price_candidates.append(
                    FieldMergeCandidate(
                        (p_val_num, p_curr_num),
                        source="css_selector",
                        selector=".a-price .a-offscreen",
                        confidence=0.90,
                    )
                )

        price_tuple, p_price = select_best_field(price_candidates)
        current_price = price_tuple[0] if price_tuple else 0.0
        final_currency = price_tuple[1] if price_tuple else currency
        if p_price:
            provenance["current_price"] = p_price

        # Original Price
        orig_el = soup.select_one(".basisPrice .a-offscreen") or soup.select_one(
            "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"
        )
        original_price = None
        if orig_el:
            original_price, _ = parse_price_and_currency(
                orig_el.get_text(strip=True), default_currency=final_currency
            )
            provenance["original_price"] = FieldProvenance(
                source="css_selector", selector=".basisPrice", confidence=0.85
            )

        # 4. Bullet Points & Description
        bullet_points: list[str] = []
        for li in soup.select("#feature-bullets ul li span.a-list-item"):
            txt = li.get_text(strip=True)
            if txt and not txt.startswith("›"):
                bullet_points.append(txt)

        desc_el = soup.select_one("#productDescription p") or soup.select_one("#productDescription")
        description = desc_el.get_text(strip=True) if desc_el else "\n".join(bullet_points)
        provenance["description"] = FieldProvenance(
            source="css_selector", selector="#feature-bullets", confidence=0.85
        )

        # 5. Availability
        avail_str = None
        avail_el = soup.select_one("#availability span") or soup.select_one("#availability")
        if avail_el:
            avail_str = avail_el.get_text(strip=True)
        elif offers.get("availability"):
            avail_str = offers["availability"]
        availability, available_qty = parse_availability(avail_str)

        # 6. Rating & Reviews
        rating = None
        review_count = None
        agg = json_ld_data.get("aggregateRating", {})
        if agg.get("ratingValue"):
            try:
                rating = float(agg["ratingValue"])
                review_count = int(agg.get("reviewCount", 0))
                provenance["rating"] = FieldProvenance(source="json_ld", confidence=0.95)
            except ValueError:
                pass
        if rating is None:
            r_el = soup.select_one("#acrPopover") or soup.select_one("i.a-icon-star span")
            rev_el = soup.select_one("#acrCustomerReviewText")
            r_title = _to_optional_str(r_el.get("title")) if r_el else None
            rating, review_count = parse_rating_and_reviews(
                r_title or (r_el.get_text(strip=True) if r_el else None),
                rev_el.get_text(strip=True) if rev_el else None,
            )

        # 7. Images
        images: list[ProductImage] = []
        seen_imgs: set[str] = set()

        main_img = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
        if main_img:
            src = _to_str(main_img.get("src") or main_img.get("data-old-hires"))
            if src and src not in seen_imgs:
                seen_imgs.add(src)
                images.append(ProductImage(url=src, position=0))

        for idx, alt_img in enumerate(soup.select("#altImages ul li img"), start=1):
            src = _to_str(alt_img.get("src"))
            if src and not src.endswith(".gif"):
                # Clean thumbnail to large hires url
                hires = re.sub(r"\._[A-Z0-9_,]+_\.", ".", src)
                if hires not in seen_imgs:
                    seen_imgs.add(hires)
                    images.append(ProductImage(url=hires, position=idx))

        # 8. Specifications
        specifications: dict[str, str] = {}
        for row in soup.select("#productDetails_techSpec_section_1 tr, #prodDetails tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td:
                k = th.get_text(strip=True)
                v = td.get_text(strip=True)
                if k and v:
                    specifications[k] = v

        # 9. Brand & Seller
        brand_el = soup.select_one("#bylineInfo") or soup.select_one("a#bylineInfo")
        brand = (
            brand_el.get_text(strip=True)
            .replace("Brand:", "")
            .replace("Visit the", "")
            .replace("Store", "")
            .strip()
            if brand_el
            else None
        )
        if not brand and json_ld_data.get("brand"):
            b = json_ld_data["brand"]
            brand = b.get("name") if isinstance(b, dict) else str(b)

        seller_el = soup.select_one("#merchant-info") or soup.select_one("#sellerProfileTriggerId")
        seller_name = "Amazon"
        if seller_el:
            seller_text = seller_el.get_text(strip=True)
            if "Ships from and sold by" in seller_text:
                seller_name = seller_text.replace("Ships from and sold by", "").strip()
            elif seller_text:
                seller_name = seller_text

        seller = SellerInfo(name=seller_name)

        # 10. Shipping (only add when supported by source)
        shipping_options: list[ShippingOption] = []
        ship_block = soup.select_one(
            "#mir-layout-DELIVERY_BLOCK, #deliveryBlockMessage, #fast-track-message, #delivery-message"
        )
        if ship_block:
            ship_text = ship_block.get_text(separator=" ", strip=True)
            if "free" in ship_text.lower():
                shipping_options.append(
                    ShippingOption(
                        name="Standard Delivery",
                        price=0.0,
                        currency=final_currency,
                        free_shipping=True,
                    )
                )
            else:
                p, c = parse_price_and_currency(ship_text, default_currency=final_currency)
                if p is not None and p > 0:
                    shipping_options.append(
                        ShippingOption(
                            name="Standard Delivery",
                            price=float(p),
                            currency=c or final_currency,
                            free_shipping=False,
                        )
                    )

        # 11. Variants Extraction
        variants = self._extract_variants(
            soup=soup,
            asin=asin,
            base_price=current_price,
            currency=final_currency,
            json_ld_data=json_ld_data,
        )

        return NormalizedProduct(
            provider="amazon",
            external_id=asin,
            canonical_url=url,
            locale=locale,
            country=country,
            title=title or f"Amazon Item {asin}",
            brand=brand,
            description=description,
            bullet_points=bullet_points,
            current_price=current_price,
            original_price=original_price,
            currency=final_currency,
            availability=availability,
            available_quantity=available_qty,
            rating=rating,
            review_count=review_count,
            condition="new",
            seller=seller,
            images=images,
            specifications=specifications,
            variants=variants,
            shipping_options=shipping_options,
            identifiers={"asin": asin},
            source_metadata=provenance,
            retrieved_at=utc_now(),
            variant_retrieval=bool(len(variants) > 0),
        )

    def _extract_variants(
        self,
        soup: BeautifulSoup,
        asin: str,
        base_price: float,
        currency: str,
        json_ld_data: dict[str, Any],
    ) -> list[ProductVariant]:
        from app.config import get_settings

        settings = get_settings()
        max_combos = getattr(settings, "MAX_VARIANT_COMBINATIONS", 50)
        variants: list[ProductVariant] = []
        seen_ids: set[str] = set()

        # 1. Check JSON-LD hasVariant
        for raw_var in json_ld_data.get("hasVariant", []):
            if not isinstance(raw_var, dict):
                continue
            v_id_raw = raw_var.get("sku") or raw_var.get("productID") or raw_var.get("identifier")
            v_id = str(v_id_raw).strip() if v_id_raw else None
            if v_id and v_id in seen_ids:
                continue
            if v_id:
                seen_ids.add(v_id)

            v_title = raw_var.get("name")
            v_price: float | None = None
            v_curr = currency
            v_avail = ProductAvailability.UNKNOWN

            offer = raw_var.get("offers")
            if isinstance(offer, list) and offer:
                offer = offer[0]
            if isinstance(offer, dict):
                if offer.get("price") is not None:
                    try:
                        v_price = float(offer["price"])
                    except (ValueError, TypeError):
                        v_price = None
                if offer.get("priceCurrency"):
                    v_curr = str(offer["priceCurrency"])
                if offer.get("availability"):
                    avail_str = str(offer["availability"]).lower()
                    if "instock" in avail_str:
                        v_avail = ProductAvailability.IN_STOCK
                    elif "outofstock" in avail_str:
                        v_avail = ProductAvailability.OUT_OF_STOCK
                    elif "limited" in avail_str:
                        v_avail = ProductAvailability.LIMITED_STOCK

            opts: dict[str, str] = {}
            for k in ("color", "size", "material", "pattern"):
                if raw_var.get(k):
                    opts[k.capitalize()] = str(raw_var[k])

            v_img: list[ProductImage] = []
            if raw_var.get("image"):
                u = raw_var["image"]
                if isinstance(u, str):
                    v_img.append(ProductImage(url=u))
                elif isinstance(u, list):
                    for item_u in u:
                        if isinstance(item_u, str):
                            v_img.append(ProductImage(url=item_u))

            variants.append(
                ProductVariant(
                    external_id=v_id,
                    title=v_title,
                    selected_options=opts,
                    price=v_price,
                    currency=v_curr,
                    availability=v_avail,
                    images=v_img,
                    canonical_url=f"https://www.amazon.com/dp/{v_id}" if v_id else None,
                )
            )
            if len(variants) >= max_combos:
                return variants

        # 2. Check inline twister JavaScript states
        price_map: dict[str, float] = {}
        avail_map: dict[str, ProductAvailability] = {}
        image_map: dict[str, list[ProductImage]] = {}

        for script in soup.select("script"):
            text = script.get_text() or ""
            if "asinToPrice" in text or "twisterPriceData" in text:
                try:
                    p_data = extract_json_object_after_key(text, "asinToPrice") or extract_json_object_after_key(text, "twisterPriceData")
                    if isinstance(p_data, dict):
                        for c_asin, p_info in p_data.items():
                            if isinstance(p_info, (int, float)):
                                price_map[str(c_asin)] = float(p_info)
                            elif isinstance(p_info, dict) and "price" in p_info:
                                try:
                                    price_map[str(c_asin)] = float(p_info["price"])
                                except (ValueError, TypeError):
                                    pass
                except Exception:
                    pass

            if "colorImages" in text or "variationImages" in text:
                try:
                    img_data = extract_json_object_after_key(text, "colorImages") or extract_json_object_after_key(text, "variationImages")
                    if isinstance(img_data, dict):
                        for c_key, imgs in img_data.items():
                            if isinstance(imgs, list):
                                c_imgs = []
                                for im in imgs:
                                    if isinstance(im, dict):
                                        u = im.get("large") or im.get("hiRes") or im.get("main", {}).get("url")
                                        if u:
                                            c_imgs.append(ProductImage(url=str(u)))
                                if c_imgs:
                                    image_map[str(c_key)] = c_imgs
                except Exception:
                    pass

            if "asinVariationValues" in text:
                try:
                    var_map = extract_json_object_after_key(text, "asinVariationValues")
                    if var_map and isinstance(var_map, dict):
                        for child_asin, dims in var_map.items():
                            if not child_asin or child_asin in seen_ids or child_asin == asin:
                                continue
                            seen_ids.add(str(child_asin))
                            opts = {}
                            if isinstance(dims, dict):
                                for d_key, d_val in dims.items():
                                    clean_k = (
                                        d_key.replace("_name", "")
                                        .replace("-", " ")
                                        .capitalize()
                                    )
                                    opts[clean_k] = str(d_val)

                            c_price = price_map.get(str(child_asin))
                            c_avail = avail_map.get(str(child_asin), ProductAvailability.UNKNOWN)
                            c_imgs = image_map.get(str(child_asin), [])
                            if not c_imgs and "Color" in opts:
                                c_imgs = image_map.get(opts["Color"], [])

                            variants.append(
                                ProductVariant(
                                    external_id=str(child_asin),
                                    title=f"Variant {child_asin}",
                                    selected_options=opts,
                                    price=c_price,
                                    currency=currency,
                                    availability=c_avail,
                                    images=c_imgs,
                                    canonical_url=f"https://www.amazon.com/dp/{child_asin}",
                                )
                            )
                            if len(variants) >= max_combos:
                                return variants
                except Exception:
                    pass

        # 3. Check Swatches only if ONE variation group exists
        variation_containers = soup.select(
            "div#variation_color_name, div#variation_size_name, div#variation_style_name, div[id^='variation_']"
        )
        # If there are multiple variation groups without a valid combination map,
        # we do NOT generate Cartesian combinations or individual dimension variants.
        if len(variation_containers) == 1:
            group_div = variation_containers[0]
            raw_group = (
                str(group_div.get("id", "variation_option"))
                .replace("variation_", "")
                .replace("_name", "")
                .capitalize()
            )
            for item in group_div.select("li[data-defaultasin], li[data-asin]"):
                raw_child = item.get("data-defaultasin") or item.get("data-asin")
                if not raw_child:
                    continue
                child_asin = str(raw_child).strip()
                if not child_asin or child_asin in seen_ids or child_asin == asin:
                    continue
                seen_ids.add(child_asin)

                opt_val_raw = item.get("title") or item.get_text(strip=True) or child_asin
                opt_val = re.sub(
                    r"^Click to select\s+", "", str(opt_val_raw), flags=re.IGNORECASE
                ).strip()

                img_el = item.select_one("img")
                v_images: list[ProductImage] = []
                if img_el and img_el.get("src"):
                    v_images.append(ProductImage(url=str(img_el["src"])))

                swatch_price: float | None = None
                p_el = item.select_one(".a-price .a-offscreen")
                if p_el:
                    p_num, _ = parse_price_and_currency(
                        p_el.get_text(strip=True), default_currency=currency
                    )
                    swatch_price = p_num

                raw_cls = item.get("class")
                cls_list: list[str] = [raw_cls] if isinstance(raw_cls, str) else ([str(c) for c in raw_cls] if isinstance(raw_cls, list) else [])
                swatch_avail = ProductAvailability.UNKNOWN
                if any("unavailable" in c.lower() for c in cls_list):
                    swatch_avail = ProductAvailability.OUT_OF_STOCK


                variants.append(
                    ProductVariant(
                        external_id=str(child_asin),
                        title=f"{raw_group}: {opt_val}",
                        selected_options={raw_group: opt_val},
                        price=swatch_price,
                        currency=currency,
                        availability=swatch_avail,
                        images=v_images,
                        canonical_url=f"https://www.amazon.com/dp/{child_asin}",
                    )
                )
                if len(variants) >= max_combos:
                    return variants

        return variants

    def _extract_json_ld(self, soup: BeautifulSoup) -> dict[str, Any]:
        scripts = soup.select("script[type='application/ld+json']")
        for script in scripts:
            try:
                data = json.loads(script.get_text() or "")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
                elif isinstance(data, dict):
                    if data.get("@type") == "Product":
                        return data
                    for item in data.get("@graph", []):
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except Exception:
                pass
        return {}
