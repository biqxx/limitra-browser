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


class EbayParser:
    def __init__(self, config: SupplierConfigSchema | None = None):
        self.config = config

    def parse_search_results(self, html: str, provider: str = "ebay") -> list[SearchResultItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[SearchResultItem] = []

        # Find items by s-item container
        containers = soup.select(".s-item")
        for container in containers:
            try:
                title_el = container.select_one(".s-item__title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or "Shop on eBay" in title:
                    continue  # Filter out eBay dummy header item

                link_el = container.select_one(".s-item__link")
                url = _to_str(link_el.get("href")) if link_el else ""

                # Extract external id from URL or data attribute
                external_id = ""
                id_match = re.search(r"/itm/(?:.*?/)?(\d+)", url)
                if id_match:
                    external_id = id_match.group(1)
                else:
                    external_id = _to_str(container.get("data-item-id"))

                price_el = container.select_one(".s-item__price")
                raw_price = price_el.get_text(strip=True) if price_el else ""
                current_price, currency = parse_price_and_currency(raw_price)

                orig_price_el = container.select_one(
                    ".s-item__trending-price .STRIKETHROUGH"
                ) or container.select_one(".s-item__discount-price")
                raw_orig = orig_price_el.get_text(strip=True) if orig_price_el else None
                original_price, _ = parse_price_and_currency(raw_orig, default_currency=currency)

                img_el = container.select_one(".s-item__image-img") or container.select_one("img")
                img_url = (
                    _to_optional_str(img_el.get("src") or img_el.get("data-src"))
                    if img_el
                    else None
                )

                # Rating and reviews
                rating_el = container.select_one(".x-star-rating span") or container.select_one(
                    ".b-starrating"
                )
                rev_el = container.select_one(".s-item__reviews-count")
                rating, reviews = parse_rating_and_reviews(
                    rating_el.get_text(strip=True) if rating_el else None,
                    rev_el.get_text(strip=True) if rev_el else None,
                )

                # Free shipping check
                shipping_el = container.select_one(
                    ".s-item__logisticsCost"
                ) or container.select_one(".s-item__shipping")
                free_shipping = False
                if shipping_el and "free" in shipping_el.get_text(strip=True).lower():
                    free_shipping = True

                items.append(
                    SearchResultItem(
                        provider=provider,
                        external_id=external_id,
                        title=title,
                        url=url,
                        price=current_price or 0.0,
                        original_price=original_price,
                        currency=currency,
                        thumbnail_url=img_url,
                        rating=rating,
                        review_count=reviews,
                        condition="new",
                        free_shipping=free_shipping,
                        in_stock=True,
                    )
                )
            except Exception as e:
                logger.debug("ebay_search_item_parse_error", error=str(e))

        return items

    def parse_product_page(
        self,
        html: str,
        url: str,
        external_id: str,
        locale: str = "en-US",
        country: str = "US",
        currency: str = "USD",
    ) -> NormalizedProduct:
        soup = BeautifulSoup(html, "html.parser")
        provenance: dict[str, FieldProvenance] = {}

        # 1. JSON-LD Extraction
        json_ld_data = self._extract_json_ld(soup)

        # 2. Extract Title
        title_candidates = []
        if json_ld_data.get("name"):
            title_candidates.append(
                FieldMergeCandidate(json_ld_data["name"], source="json_ld", confidence=0.95)
            )

        title_sel = (
            soup.select_one("h1.x-item-title__mainTitle span")
            or soup.select_one("#itemTitle")
            or soup.select_one("h1")
        )
        if title_sel:
            # Clean eBay "Details about  " prefix
            t_text = title_sel.get_text(strip=True).replace("Details about", "").strip()
            title_candidates.append(
                FieldMergeCandidate(
                    t_text,
                    source="css_selector",
                    selector="h1.x-item-title__mainTitle",
                    confidence=0.85,
                )
            )

        title, p_title = select_best_field(title_candidates)
        if p_title:
            provenance["title"] = p_title

        # 3. Extract Price & Currency
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
            soup.select_one(".x-price-primary span")
            or soup.select_one("#prcIsum")
            or soup.select_one(".x-bin-price__content span")
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
                        selector=".x-price-primary",
                        confidence=0.85,
                    )
                )

        price_tuple, p_price = select_best_field(price_candidates)
        current_price = price_tuple[0] if price_tuple else 0.0
        final_currency = price_tuple[1] if price_tuple else currency
        if p_price:
            provenance["current_price"] = p_price

        # Original Price
        orig_el = soup.select_one(".x-price-approx span") or soup.select_one(
            ".x-additional-info__text--original"
        )
        original_price = None
        if orig_el:
            original_price, _ = parse_price_and_currency(
                orig_el.get_text(strip=True), default_currency=final_currency
            )
            provenance["original_price"] = FieldProvenance(
                source="css_selector", selector=".x-price-approx", confidence=0.8
            )

        # 4. Description
        desc_candidates = []
        if json_ld_data.get("description"):
            desc_candidates.append(
                FieldMergeCandidate(json_ld_data["description"], source="json_ld", confidence=0.95)
            )
        desc_el = (
            soup.select_one("#viTabs_0_is")
            or soup.select_one("#desc_div")
            or soup.select_one(".d-item-description")
        )
        if desc_el:
            desc_candidates.append(
                FieldMergeCandidate(
                    desc_el.get_text(strip=True),
                    source="css_selector",
                    selector="#desc_div",
                    confidence=0.75,
                )
            )
        desc, p_desc = select_best_field(desc_candidates)
        if p_desc:
            provenance["description"] = p_desc

        # 5. Availability
        avail_str = None
        qty_el = soup.select_one(".d-quantity__availability span") or soup.select_one("#qtySubTxt")
        if qty_el:
            avail_str = qty_el.get_text(strip=True)
        elif offers.get("availability"):
            avail_str = offers["availability"]
        availability, available_qty = parse_availability(avail_str)

        # 6. Rating & Reviews
        rating = None
        review_count = None
        agg_rating = json_ld_data.get("aggregateRating", {})
        if agg_rating.get("ratingValue"):
            try:
                rating = float(agg_rating["ratingValue"])
                review_count = int(agg_rating.get("reviewCount", 0))
                provenance["rating"] = FieldProvenance(source="json_ld", confidence=0.95)
            except ValueError:
                pass
        if rating is None:
            r_el = soup.select_one(".x-star-rating span")
            rev_el = soup.select_one(".x-star-rating-count")
            rating, review_count = parse_rating_and_reviews(
                r_el.get_text(strip=True) if r_el else None,
                rev_el.get_text(strip=True) if rev_el else None,
            )

        # 7. Images
        images: list[ProductImage] = []
        img_urls: set[str] = set()

        if json_ld_data.get("image"):
            raw_imgs = json_ld_data["image"]
            if isinstance(raw_imgs, str):
                raw_imgs = [raw_imgs]
            for pos, u in enumerate(raw_imgs):
                if u and u not in img_urls:
                    img_urls.add(u)
                    images.append(ProductImage(url=u, position=pos))

        for _pos, img_tag in enumerate(
            soup.select(".ux-image-filmstrip-carousel img, .x-photos-min-view img, #icImg")
        ):
            raw_src = img_tag.get("src") or img_tag.get("data-src")
            src = _to_str(raw_src)
            if src and src not in img_urls and not src.endswith(".gif"):
                # Clean eBay thumbnail to full resolution (s-l64 -> s-l1600)
                clean_src = re.sub(r"s-l\d+\.", "s-l1600.", src)
                img_urls.add(clean_src)
                images.append(
                    ProductImage(
                        url=clean_src,
                        alt_text=_to_optional_str(img_tag.get("alt")),
                        position=len(images),
                    )
                )

        # 8. Specifications
        specifications: dict[str, str] = {}
        for row in soup.select(".ux-layout-section-evo__row, .itemAttr tr, .ux-labels-values"):
            labels = row.select(".ux-labels-values__labels, th, .col-label")
            values = row.select(".ux-labels-values__values, td, .col-value")
            for lbl, val in zip(labels, values, strict=False):
                k = lbl.get_text(strip=True).rstrip(":")
                v = val.get_text(strip=True)
                if k and v:
                    specifications[k] = v

        # Brand from specs or json_ld
        brand = (
            json_ld_data.get("brand", {}).get("name")
            if isinstance(json_ld_data.get("brand"), dict)
            else None
        )
        if not brand:
            brand = specifications.get("Brand")

        # 9. Seller
        seller_name = "eBay Seller"
        seller_el = soup.select_one(
            ".x-sellercard-atf__info__about-seller span"
        ) or soup.select_one(".mbg-nw")
        if seller_el:
            seller_name = seller_el.get_text(strip=True)

        seller = SellerInfo(name=seller_name)

        # 10. Shipping
        shipping_options: list[ShippingOption] = []
        ship_el = soup.select_one(".ux-labels-values--shipping") or soup.select_one("#shSummary")
        if ship_el:
            ship_text = ship_el.get_text(strip=True)
            is_free = "free" in ship_text.lower()
            ship_price, _ = parse_price_and_currency(ship_text, default_currency=final_currency)
            shipping_options.append(
                ShippingOption(
                    name="Standard Shipping",
                    price=0.0 if is_free else (ship_price or 0.0),
                    currency=final_currency,
                    free_shipping=is_free,
                )
            )

        # 11. Condition
        condition = "new"
        cond_el = soup.select_one(".x-item-condition-text") or soup.select_one(
            "[data-testid='x-item-condition']"
        )
        if cond_el:
            c_text = cond_el.get_text(strip=True).lower()
            if "used" in c_text:
                condition = "used"
            elif "refurbished" in c_text:
                condition = "refurbished"
            elif "open box" in c_text:
                condition = "open_box"

        # 12. Variants Extraction
        variants = self._extract_variants(
            soup=soup,
            item_id=external_id,
            base_price=current_price,
            currency=final_currency,
            json_ld_data=json_ld_data,
        )

        return NormalizedProduct(
            provider="ebay",
            external_id=external_id,
            canonical_url=url,
            locale=locale,
            country=country,
            title=title or f"eBay Product {external_id}",
            brand=brand,
            description=desc or "",
            current_price=current_price,
            original_price=original_price,
            currency=final_currency,
            availability=availability,
            available_quantity=available_qty,
            rating=rating,
            review_count=review_count,
            condition=condition,
            seller=seller,
            images=images,
            specifications=specifications,
            variants=variants,
            shipping_options=shipping_options,
            source_metadata=provenance,
            retrieved_at=utc_now(),
            variant_retrieval=bool(len(variants) > 0),
        )

    def _extract_variants(
        self,
        soup: BeautifulSoup,
        item_id: str,
        base_price: float,
        currency: str,
        json_ld_data: dict[str, Any],
    ) -> list[ProductVariant]:
        from app.config import get_settings

        settings = get_settings()
        max_combos = getattr(settings, "MAX_VARIANT_COMBINATIONS", 50)
        variants: list[ProductVariant] = []
        seen_ids: set[str] = set()

        # 1. JSON-LD hasVariant
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
            for k in ("color", "size", "material", "model"):
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
                    canonical_url=f"https://www.ebay.com/itm/{v_id}" if v_id else f"https://www.ebay.com/itm/{item_id}",
                )
            )
            if len(variants) >= max_combos:
                return variants

        # 2. Check inline variation scripts (e.g. itemVariationsMap, menuItemMap)
        for script in soup.select("script"):
            text = script.get_text() or ""
            for key in ("itemVariationsMap", "menuItemMap"):
                if key in text:
                    try:
                        var_map = extract_json_object_after_key(text, key)
                        if var_map and isinstance(var_map, dict):
                            for raw_key, item_info in var_map.items():
                                v_key = str(raw_key).strip()
                                if not v_key or v_key in seen_ids:
                                    continue
                                seen_ids.add(v_key)

                                v_external_id: str | None = None
                                price_val: float | None = None
                                v_avail = ProductAvailability.UNKNOWN
                                v_opts: dict[str, str] = {}
                                v_images: list[ProductImage] = []

                                if isinstance(item_info, dict):
                                    raw_item_id = item_info.get("itemId") or item_info.get("sku") or item_info.get("variationId")
                                    if raw_item_id:
                                        v_external_id = str(raw_item_id)

                                    if "price" in item_info or "itemPrice" in item_info:
                                        p_raw = item_info.get("price") or item_info.get("itemPrice")
                                        if isinstance(p_raw, (int, float)):
                                            price_val = float(p_raw)
                                        elif isinstance(p_raw, str):
                                            parsed_p, _ = parse_price_and_currency(p_raw)
                                            price_val = parsed_p

                                    if "quantityAvailable" in item_info:
                                        try:
                                            qty = int(item_info["quantityAvailable"])
                                            v_avail = ProductAvailability.IN_STOCK if qty > 0 else ProductAvailability.OUT_OF_STOCK
                                        except (ValueError, TypeError):
                                            pass

                                    if "aspects" in item_info and isinstance(item_info["aspects"], dict):
                                        for ak, av in item_info["aspects"].items():
                                            v_opts[str(ak)] = str(av)
                                    elif "options" in item_info and isinstance(item_info["options"], dict):
                                        for ak, av in item_info["options"].items():
                                            v_opts[str(ak)] = str(av)

                                    if "imageUrl" in item_info and item_info["imageUrl"]:
                                        v_images.append(ProductImage(url=str(item_info["imageUrl"])))
                                    elif "images" in item_info and isinstance(item_info["images"], list):
                                        for im in item_info["images"]:
                                            if isinstance(im, str):
                                                v_images.append(ProductImage(url=im))
                                            elif isinstance(im, dict) and im.get("imageUrl"):
                                                v_images.append(ProductImage(url=str(im["imageUrl"])))

                                if not v_opts:
                                    v_opts = {"Option": v_key}

                                variants.append(
                                    ProductVariant(
                                        external_id=v_external_id,
                                        title=f"Option {v_key}",
                                        selected_options=v_opts,
                                        price=price_val,
                                        currency=currency,
                                        availability=v_avail,
                                        images=v_images,
                                        canonical_url=f"https://www.ebay.com/itm/{v_external_id}" if v_external_id else f"https://www.ebay.com/itm/{item_id}",
                                    )
                                )
                                if len(variants) >= max_combos:
                                    return variants
                    except Exception:
                        pass

        # 3. Check MSKU Select Dropdowns in DOM only if ONE variation group exists
        dropdowns = soup.select("select.msku-sel, select[name*='msku'], select[id^='msku-sel-']")
        if len(dropdowns) == 1:
            dd = dropdowns[0]
            raw_group_name = dd.get("name") or dd.get("aria-label") or "Option"
            group_name = re.sub(r"^msku-sel-\d+", "Option", str(raw_group_name)).capitalize()

            for opt in dd.select("option"):
                opt_val = opt.get_text(strip=True)
                val_attr = opt.get("value", "")
                if not val_attr or val_attr in ("-1", "0") or "select" in opt_val.lower():
                    continue

                if opt_val in seen_ids:
                    continue
                seen_ids.add(opt_val)

                # Check if option text has price modifier (e.g. "+$10.00" or explicit "$25.00")
                opt_price: float | None = None
                price_match = re.search(r"\+\s*\$?([\d.]+)", opt_val)
                if price_match:
                    try:
                        delta = float(price_match.group(1))
                        opt_price = round(base_price + delta, 2)
                    except ValueError:
                        pass
                else:
                    direct_match = re.search(r"\$([\d.]+)", opt_val)
                    if direct_match:
                        try:
                            opt_price = float(direct_match.group(1))
                        except ValueError:
                            pass

                clean_title = re.sub(r"\s*(\+\s*\$?[\d.]+|\$([\d.]+))", "", opt_val).strip()

                opt_avail = ProductAvailability.UNKNOWN
                if opt.get("disabled") is not None or "out of stock" in opt_val.lower():
                    opt_avail = ProductAvailability.OUT_OF_STOCK

                # External ID is None unless supplier provides data-item-id
                opt_raw = opt.get("data-item-id") or opt.get("data-sku")
                opt_ext_id: str | None = str(opt_raw) if opt_raw else None

                variants.append(

                    ProductVariant(
                        external_id=opt_ext_id,
                        title=f"{group_name}: {clean_title}",
                        selected_options={group_name: clean_title},
                        price=opt_price,
                        currency=currency,
                        availability=opt_avail,
                        canonical_url=f"https://www.ebay.com/itm/{item_id}",
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
                    # Check @graph
                    for item in data.get("@graph", []):
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except Exception:
                pass
        return {}
