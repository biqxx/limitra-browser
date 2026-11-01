# supplier-browser-service

A cloud-based product discovery and extraction service built with **Python 3.12**, **FastAPI**, **Pydantic v2**, **Async Playwright (Chromium)**, **PostgreSQL (SQLAlchemy 2 Async)**, **Alembic**, and **Redis**.

The service provides normalized ecommerce product search and complete product detail retrieval across multiple suppliers (**eBay** and **Amazon** initially, architected to allow adding **AliExpress**, **Walmart**, **Shein**, **Temu**, **Alibaba**, or independent ecommerce platforms without modifying the core application).

---

## 🏛️ System Architecture

```text
                                  Client Apps / Microservices
                                              │  (HTTPS + X-API-Key)
                                              ▼
                                   ┌──────────────────────┐
                                   │ Load Balancer / Nginx│
                                   └──────────┬───────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
          ┌────────────────────────┐                     ┌────────────────────────┐
          │  FastAPI Replica 1     │                     │  FastAPI Replica 2     │
          │ (Auth, Cache, SSRF)    │                     │ (Auth, Cache, SSRF)    │
          └───────────┬────────────┘                     └───────────┬────────────┘
                      │                                               │
                      └───────────────────────┬───────────────────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
         ┌────────────────────┐    ┌────────────────────┐    ┌────────────────────┐
         │ PostgreSQL DB      │    │ Redis Cache / Lock │    │ Async Job Worker   │
         │ (Entities/Auditing)│    │ & Rate Limiting    │    │ (Playwright Pool)  │
         └────────────────────┘    └────────────────────┘    └──────────┬─────────┘
                                                                        │
                                       ┌────────────────────────────────┴────────────────┐
                                       ▼                                                 ▼
                          ┌────────────────────────┐                       ┌────────────────────────┐
                          │ Official Supplier APIs │                       │ Headless Chromium      │
                          │ • eBay Browse API      │                       │ • Isolated Contexts    │
                          │ • Amazon PA-API 5.0    │                       │ • SSRF & Route Guards  │
                          └────────────────────────┘                       └────────────────────────┘
```

### Architectural Principles

1. **Adapter Isolation**: Each supplier has a self-contained adapter in `app/suppliers/<supplier>/` encapsulating domains, URL normalization, selectors, API clients, and parsing. No supplier-specific conditionals exist in the core services.
2. **Compliance & Anti-Evasion**: Operates strictly within legal, ethical, and supplier policy boundaries. No CAPTCHA bypassing, no fingerprint spoofing, and no access control evasion. Returns structured `supplier_blocked` errors recommending official API credentials.
3. **Extraction Priority**:
   1. Official Supplier API (highest priority & accuracy, confidence ~0.99)
   2. Embedded JSON-LD schema (`Product`, `Offer`, `AggregateRating`, confidence ~0.95)
   3. Structured state & meta tags (confidence ~0.85)
   4. Semantic HTML attributes (confidence ~0.75)
   5. Versioned YAML CSS selectors configuration (confidence ~0.60)
4. **Resilience & Concurrency**: Process pooling with automatic recycling after page limits, global and per-supplier concurrency controls, distributed locks (`NX EX` with Lua releases), and negative caching for unavailable items.
5. **SSRF & Network Hardening**: Strict host allowlists, DNS pre-resolution against RFC 1918/link-local/cloud metadata (`169.254.169.254`), IDN normalization, and redirect validation on every hop.

---

## 🚀 Quickstart with Docker Compose

### Prerequisites
- Docker Engine 24+ and Docker Compose v2.20+

### Run Services
```bash
# Clone and enter directory
git clone https://github.com/example/supplier-browser-service.git
cd supplier-browser-service

# Copy environment template
cp .env.example .env

# Launch all infrastructure services
docker compose up -d --build
```

### Check Service Health
```bash
# Liveness probe
curl -s http://localhost:8000/health/live

# Readiness probe (verifies database, Redis, and browser worker)
curl -s http://localhost:8000/health/ready
```

### Production dependencies and supplier credentials

`compose.production.yml` deploys the migration, API, and worker containers only. It expects
reachable PostgreSQL and Redis services through `DATABASE_URL` and `REDIS_URL`, plus existing
Docker networks named `platform_edge` and `platform_data`. A fresh standalone EC2 instance must
either provide those services itself or use externally reachable managed/shared services; Docker
networks do not span separate EC2 hosts.

eBay browser scraping is disabled by default because eBay rejects automated browser access. Set
`EBAY_API_ENABLED=true`, `EBAY_APP_ID`, `EBAY_CERT_ID`, and `EBAY_ENVIRONMENT=PRODUCTION` through
the deployment environment. Keep `EBAY_BROWSER_FALLBACK_ENABLED=false` in production.

Amazon uses PA-API when its credentials are configured and otherwise uses the browser adapter.
The browser adapter selects the storefront from the requested country and reports the currency it
actually detects; a mismatch is returned as a data-quality warning and is never silently relabelled.

Optional Playwright proxying is configured with `BROWSER_PROXY_SERVER`,
`BROWSER_PROXY_USERNAME`, `BROWSER_PROXY_PASSWORD`, and `BROWSER_PROXY_BYPASS`. Apply the same
settings to both API and worker containers because synchronous search/detail requests may execute
browser work in the API process while queued extraction runs in the worker. Store proxy credentials
only in the protected deployment environment, never in the image or repository.

Live supplier acceptance tests are intentionally opt-in:

```bash
RUN_LIVE_SUPPLIER_TESTS=true pytest -q tests/integration/test_amazon_live_acceptance.py
RUN_LIVE_SUPPLIER_TESTS=true pytest -q tests/integration/test_ebay_live_acceptance.py
```

Never place supplier credentials in the repository or Docker image.

---

## 💻 Local Development Setup

### 1. Create Virtual Environment
```bash
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.\.venv\Scripts\activate
```

### 2. Install Dependencies
```bash
pip install -e .[dev]
playwright install chromium
```

### 3. Run Database Migrations
```bash
# Applies Alembic migrations (creates 10 tables)
alembic upgrade head
```

### 4. Run the API Server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Run the Test Suite
```bash
# Run all unit, contract, and integration tests
pytest -v

# Run code style and lint check
ruff check .

# Run static type checker
mypy app
```

---

## 🔑 Authentication & API Keys

Every request requires the `X-API-Key` header (unless `ENFORCE_API_KEYS=false` is set in development).

API keys are stored as **HMAC-SHA256 hashes** with a secret salt (`API_KEY_SECRET`). Plaintext keys are never stored in the database or logged.

### Default Bootstrap Admin Key
Configured in `.env`:
```text
BOOTSTRAP_ADMIN_API_KEY=adm_secret_bootstrap_key_12345
```

### Generating Client API Keys
Use the helper in `app/security/auth.py`:
```python
from app.security.auth import generate_new_api_key

plain_key, key_hash = generate_new_api_key(prefix="sk_live_")
print(f"Provide this key to client once: {plain_key}")
print(f"Store this hash in database: {key_hash}")
```

---

## 📡 API Reference & Usage Examples

### 1. Synchronous Search

**Endpoint**: `POST /api/v1/search`  
**Headers**:
- `Content-Type: application/json`
- `X-API-Key: adm_secret_bootstrap_key_12345`

#### Request:
```bash
curl -X POST http://localhost:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: adm_secret_bootstrap_key_12345" \
  -d '{
    "provider": "ebay",
    "query": "wireless noise cancelling headphones",
    "country": "US",
    "locale": "en-US",
    "currency": "USD",
    "page": 1,
    "page_size": 20,
    "filters": {
      "minimum_price": 50,
      "maximum_price": 400,
      "free_shipping": true
    }
  }'
```

#### Response (`200 OK`):
```json
{
  "provider": "ebay",
  "query": "wireless noise cancelling headphones",
  "page": 1,
  "page_size": 20,
  "total_results": 1420,
  "items": [
    {
      "provider": "ebay",
      "external_id": "112233445566",
      "title": "Sony WH-1000XM5 Wireless Noise Cancelling Headphones - Black",
      "url": "https://www.ebay.com/itm/112233445566",
      "price": 298.00,
      "original_price": 399.99,
      "currency": "USD",
      "thumbnail_url": "https://i.ebayimg.com/images/g/abc/s-l500.jpg",
      "rating": 4.8,
      "review_count": 1420,
      "condition": "new",
      "seller_name": "TechStoreDirect",
      "free_shipping": true,
      "in_stock": true
    }
  ],
  "applied_filters": {
    "minimum_price": 50,
    "maximum_price": 400,
    "free_shipping": true
  },
  "retrieval_timestamp": "2026-10-29T18:00:00Z",
  "cache_status": "hit",
  "warnings": [],
  "partial_result": false
}
```

---

### 2. Product Retrieval by URL or ID

**Endpoint**: `POST /api/v1/products/retrieve`

#### Retrieve by URL:
```bash
curl -X POST http://localhost:8000/api/v1/products/retrieve \
  -H "Content-Type: application/json" \
  -H "X-API-Key: adm_secret_bootstrap_key_12345" \
  -d '{
    "url": "https://www.ebay.com/itm/112233445566",
    "include_variants": true,
    "include_shipping": true,
    "force_refresh": false
  }'
```

#### Retrieve by Provider and External ID:
```bash
curl -X POST http://localhost:8000/api/v1/products/retrieve \
  -H "Content-Type: application/json" \
  -H "X-API-Key: adm_secret_bootstrap_key_12345" \
  -d '{
    "provider": "amazon",
    "external_id": "B09XS7JWHH",
    "country": "US",
    "locale": "en-US",
    "currency": "USD"
  }'
```

#### Normalized Product Response (`200 OK`):
```json
{
  "provider": "amazon",
  "external_id": "B09XS7JWHH",
  "canonical_url": "https://www.amazon.com/dp/B09XS7JWHH",
  "locale": "en-US",
  "country": "US",
  "title": "Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones, Black",
  "subtitle": null,
  "brand": "Sony",
  "manufacturer": null,
  "model": "WH-1000XM5",
  "description": "Industry Leading noise cancellation-two processors and 8 microphones...",
  "bullet_points": [
    "Industry Leading noise cancellation-two processors and 8 microphones",
    "Magnificent Sound, engineered to perfection with the new Integrated Processor V1",
    "Up to 30-hour battery life with quick charging"
  ],
  "category": "Electronics",
  "category_path": ["Electronics", "Headphones", "Over-Ear"],
  "condition": "new",
  "seller": {
    "name": "Amazon.com",
    "id": null,
    "rating": null,
    "review_count": null,
    "url": null,
    "verified": true
  },
  "current_price": 398.00,
  "original_price": 449.99,
  "discount": {
    "percentage": 11.5,
    "amount": 51.99
  },
  "currency": "USD",
  "availability": "in_stock",
  "available_quantity": null,
  "rating": 4.6,
  "review_count": 12840,
  "images": [
    {
      "url": "https://m.media-amazon.com/images/I/61+btxzpfDL._AC_SL1500_.jpg",
      "thumbnail_url": null,
      "alt_text": "Sony Headphones",
      "width": 1500,
      "height": 1500,
      "position": 0,
      "variant_ids": []
    }
  ],
  "videos": [],
  "specifications": {
    "Brand": "Sony",
    "Model Name": "WH-1000XM5",
    "Color": "Black",
    "Connectivity Technology": "Wireless, Bluetooth 5.2"
  },
  "variants": [],
  "shipping_options": [
    {
      "name": "Standard Shipping",
      "price": 0.0,
      "currency": "USD",
      "estimated_days_min": 2,
      "estimated_days_max": 4,
      "free_shipping": true
    }
  ],
  "estimated_delivery": null,
  "return_policy": "30-day refund or replacement",
  "identifiers": {
    "asin": "B09XS7JWHH"
  },
  "source_metadata": {
    "title": {
      "source": "css_selector",
      "selector": "#productTitle",
      "confidence": 0.90
    },
    "current_price": {
      "source": "css_selector",
      "selector": ".a-price .a-offscreen",
      "confidence": 0.90
    }
  },
  "retrieved_at": "2026-10-29T18:00:00Z"
}
```

---

### 3. Asynchronous Background Jobs

For deep queries, broad catalog searches, or items with dozens of variants:

#### Queue Search Job:
```bash
curl -X POST http://localhost:8000/api/v1/jobs/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: adm_secret_bootstrap_key_12345" \
  -H "Idempotency-Key: idemp_search_001" \
  -d '{
    "provider": "ebay",
    "query": "vintage mechanical watch",
    "page": 1,
    "page_size": 50
  }'
```
Response (`202 Accepted`):
```json
{
  "job_id": "c1f727c6-79cf-41c8-bb84-48f8c4149dd7",
  "status": "queued",
  "idempotency_key": "idemp_search_001",
  "created_at": "2026-10-29T18:00:00Z"
}
```

#### Check Job Status:
```bash
curl -X GET http://localhost:8000/api/v1/jobs/c1f727c6-79cf-41c8-bb84-48f8c4149dd7 \
  -H "X-API-Key: adm_secret_bootstrap_key_12345"
```
Response (`200 OK`):
```json
{
  "job_id": "c1f727c6-79cf-41c8-bb84-48f8c4149dd7",
  "job_type": "search",
  "status": "completed",
  "provider": "ebay",
  "progress": {
    "stage": "completed",
    "percentage": 100,
    "message": null
  },
  "created_at": "2026-10-29T18:00:00Z",
  "started_at": "2026-10-29T18:00:01Z",
  "completed_at": "2026-10-29T18:00:03Z",
  "result": { ... },
  "error": null,
  "idempotency_key": "idemp_search_001"
}
```

#### Cancel Job:
```bash
curl -X DELETE http://localhost:8000/api/v1/jobs/c1f727c6-79cf-41c8-bb84-48f8c4149dd7 \
  -H "X-API-Key: adm_secret_bootstrap_key_12345"
```
Response: `204 No Content`

---

### 4. Error Responses

All errors return a structured JSON response without exposing internal traces or credentials:

```json
{
  "error": {
    "code": "supplier_blocked",
    "message": "The supplier 'amazon' rejected automated browser access (HTTP 403 Access Denied). Please configure and use the supplier's official API credentials.",
    "provider": "amazon",
    "retryable": false,
    "request_id": "req_84ef81c8b7464c20",
    "details": {
      "recommendation": "Use official supplier API"
    }
  }
}
```

Standard Error Codes:
- `invalid_request`
- `unsupported_provider`
- `unsupported_domain`
- `invalid_product_url`
- `product_not_found`
- `product_unavailable`
- `supplier_timeout`
- `supplier_rate_limited`
- `supplier_blocked`
- `official_api_unavailable`
- `parsing_failed`
- `partial_result`
- `job_not_found`
- `internal_error`

---

## 🔌 Adding a New Supplier

To add a new supplier (e.g. `walmart` or `aliexpress`):

1. **Create Adapter Directory**:
   ```text
   app/suppliers/walmart/
   ├── __init__.py
   ├── adapter.py
   ├── api_client.py
   ├── parser.py
   └── selectors.yaml
   ```

2. **Define Selectors in `selectors.yaml`**:
   ```yaml
   provider: walmart
   version: 1
   domains:
     - walmart.com
     - www.walmart.com
   search:
     url_template: "https://www.walmart.com/search?q={query}&page={page}"
     result_container:
       - "[data-item-id]"
   product:
     title:
       - "h1[itemprop='name']"
   ```

3. **Subclass `SupplierAdapter` in `adapter.py`**:
   ```python
   from app.suppliers.base import SupplierAdapter, SupplierExecutionContext, SupplierHealth
   from app.schemas.search import ProductSearchRequest, ProductSearchResponse
   from app.schemas.products import NormalizedProduct, ProductRetrievalRequest


   class WalmartAdapter(SupplierAdapter):
       provider = "walmart"
       supported_domains = {"walmart.com", "www.walmart.com"}
       supported_locales = {"en-US"}

       async def search(
           self, request: ProductSearchRequest, context: SupplierExecutionContext
       ) -> ProductSearchResponse:
           # implementation
           ...

       async def get_product(
           self, request: ProductRetrievalRequest, context: SupplierExecutionContext
       ) -> NormalizedProduct:
           # implementation
           ...

       def recognizes_url(self, url: str) -> bool:
           return "walmart.com" in url.lower()

       def normalize_url(self, url: str) -> str:
           # extract item id and construct clean canonical URL
           ...

       def extract_external_id(self, url: str) -> str | None: ...

       async def healthcheck(self) -> SupplierHealth: ...
   ```

4. **Register with the Registry**:
   In `app/suppliers/registry.py` (or dynamically at startup):
   ```python
   registry = get_supplier_registry()
   registry.register(WalmartAdapter())
   ```
   **No `if provider == "walmart"` statements are added to core services!**

---

## 🔒 Security & SSRF Protection

External client URLs are completely untrusted. The security pipeline enforces:
1. **Scheme Validation**: Strictly `https://` (HTTP allowed only for explicit test hosts `localhost`, `127.0.0.1`, `testserver`).
2. **Domain Allowlists**: Checked against the specific adapter's `supported_domains`.
3. **IDN Normalization**: Homograph and Punycode attacks normalized via `idna.encode`.
4. **DNS Rebinding & IP Filtering**: Pre-resolves IP and blocks:
   - Loopback (`127.0.0.0/8`, `::1`)
   - Private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
   - Link-local & cloud metadata (`169.254.0.0/16`, `fe80::/10`)
   - Multicast & broadcast (`224.0.0.0/4`, `255.255.255.255/32`)
5. **Credential Stripping**: Reject URLs with embedded credentials (`user:pass@host`).
6. **Redirect Verification**: Validates every redirect hop against the supplier domain allowlist.

---

## 🛡️ Compliance & Legal Responsibilities

This service is engineered exclusively for lawful, authorized product discovery and catalog normalization:
- **No CAPTCHA Solving / Bypassing**: Will never attempt OCR or bypass mechanisms.
- **No Bot Detection Evasion**: Will never employ stealth fingerprint injectors designed to circumvent security controls.
- **Official APIs Preferred**: Uses official authenticated partner APIs (eBay Browse API, Amazon PA-API 5.0) when credentials are provided.
- **Graceful Failure**: If a supplier page serves a CAPTCHA, access denied, or rate limit page, the service cleanly detects it, terminates navigation, and returns an actionable `supplier_blocked` error recommending official API credentials.
