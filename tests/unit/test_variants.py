from __future__ import annotations

from app.suppliers.amazon.parser import AmazonParser
from app.suppliers.ebay.parser import EbayParser


def test_amazon_no_variants():
    html = """
    <html>
        <body>
            <span id="productTitle">Single Product</span>
            <span class="a-price"><span class="a-offscreen">$29.99</span></span>
        </body>
    </html>
    """
    parser = AmazonParser()
    prod = parser.parse_product_page(html, "https://www.amazon.com/dp/B001", "B001")
    assert len(prod.variants) == 0


def test_amazon_variants_from_json_ld():
    html = """
    <html>
        <head>
            <script type="application/ld+json">
            {
                "@type": "Product",
                "name": "Multi-Variant Shoes",
                "hasVariant": [
                    {
                        "@type": "Product",
                        "sku": "B001-RED-9",
                        "name": "Red / Size 9",
                        "color": "Red",
                        "size": "9",
                        "offers": {"price": 35.00, "priceCurrency": "USD"},
                        "image": "https://img.amazon.com/red9.jpg"
                    },
                    {
                        "@type": "Product",
                        "sku": "B001-BLUE-10",
                        "name": "Blue / Size 10",
                        "color": "Blue",
                        "size": "10",
                        "offers": {"price": 40.00, "priceCurrency": "USD"},
                        "image": "https://img.amazon.com/blue10.jpg"
                    }
                ]
            }
            </script>
        </head>
        <body>
            <span id="productTitle">Multi-Variant Shoes</span>
        </body>
    </html>
    """
    parser = AmazonParser()
    prod = parser.parse_product_page(html, "https://www.amazon.com/dp/B001", "B001")
    assert len(prod.variants) == 2
    v0 = prod.variants[0]
    assert v0.external_id == "B001-RED-9"
    assert v0.selected_options == {"Color": "Red", "Size": "9"}
    assert v0.price == 35.00
    assert len(v0.images) == 1
    assert v0.images[0].url == "https://img.amazon.com/red9.jpg"

    v1 = prod.variants[1]
    assert v1.external_id == "B001-BLUE-10"
    assert v1.selected_options == {"Color": "Blue", "Size": "10"}
    assert v1.price == 40.00


def test_amazon_variants_from_twister_script():
    html = """
    <html>
        <body>
            <span id="productTitle">Twister Product</span>
            <span class="a-price"><span class="a-offscreen">$49.99</span></span>
            <script>
                var data = {
                    "asinVariationValues": {
                        "B002A": {"color_name": "Black", "size_name": "Medium"},
                        "B002B": {"color_name": "White", "size_name": "Large"}
                    }
                };
            </script>
        </body>
    </html>
    """
    parser = AmazonParser()
    prod = parser.parse_product_page(html, "https://www.amazon.com/dp/B001", "B001")
    assert len(prod.variants) == 2
    ids = {v.external_id for v in prod.variants}
    assert "B002A" in ids
    assert "B002B" in ids


def test_amazon_variants_from_swatch_dom():
    html = """
    <html>
        <body>
            <span id="productTitle">Swatch Product</span>
            <div id="variation_color_name">
                <ul class="a-button-toggle">
                    <li data-defaultasin="B003A" title="Click to select Red">
                        <img src="https://img.amazon.com/red.jpg" />
                        <span>Red</span>
                    </li>
                    <li data-defaultasin="B003B" title="Click to select Green">
                        <img src="https://img.amazon.com/green.jpg" />
                        <span>Green</span>
                    </li>
                    <li data-defaultasin="B003A" title="Duplicate Red">
                        <span>Red</span>
                    </li>
                </ul>
            </div>
        </body>
    </html>
    """
    parser = AmazonParser()
    prod = parser.parse_product_page(html, "https://www.amazon.com/dp/B001", "B001")
    # Duplicate B003A must be deduplicated
    assert len(prod.variants) == 2
    v0 = next(v for v in prod.variants if v.external_id == "B003A")
    assert v0.selected_options["Color"] == "Red"
    assert len(v0.images) == 1


def test_ebay_variants_from_msku_dropdowns():
    html = """
    <html>
        <body>
            <h1 class="x-item-title__mainTitle"><span>T-Shirt Multiple Colors</span></h1>
            <span class="x-price-primary"><span class="ux-textspans">$15.00</span></span>
            <select class="msku-sel" name="Color" id="msku-sel-1">
                <option value="-1">Select Color</option>
                <option value="1">Red</option>
                <option value="2">Blue +$3.00</option>
            </select>
        </body>
    </html>
    """
    parser = EbayParser()
    prod = parser.parse_product_page(html, "https://www.ebay.com/itm/11223344", "11223344")
    assert len(prod.variants) == 2
    v_red = next(v for v in prod.variants if v.title and "Red" in v.title)
    v_blue = next(v for v in prod.variants if v.title and "Blue" in v.title)
    assert v_red.price is None
    assert v_blue.price == 18.00  # $15.00 + $3.00


def test_ebay_variants_from_inline_script():
    html = """
    <html>
        <body>
            <h1 class="x-item-title__mainTitle"><span>Inline Script Variants</span></h1>
            <script>
                var itmVariations = {
                    "itemVariationsMap": {
                        "Size-S": {"price": 10.00},
                        "Size-M": {"price": 12.00},
                        "Size-L": {"price": 14.00}
                    }
                };
            </script>
        </body>
    </html>
    """
    parser = EbayParser()
    prod = parser.parse_product_page(html, "https://www.ebay.com/itm/55667788", "55667788")
    assert len(prod.variants) == 3
    prices = {v.price for v in prod.variants}
    assert prices == {10.0, 12.0, 14.0}


def test_malformed_supplier_variation_data_does_not_crash():
    html = """
    <html>
        <head>
            <script type="application/ld+json">
                { "corrupt json without closing brace
            </script>
        </head>
        <body>
            <span id="productTitle">Corrupt Script Product</span>
            <script>
                var data = {"asinVariationValues": "not a dictionary"};
            </script>
        </body>
    </html>
    """
    parser = AmazonParser()
    prod = parser.parse_product_page(html, "https://www.amazon.com/dp/B099", "B099")
    assert prod.external_id == "B099"
    assert len(prod.variants) == 0
