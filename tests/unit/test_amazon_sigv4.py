from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from app.suppliers.amazon.api_client import AmazonApiClient


def test_amazon_paapi_sigv4_deterministic_headers():
    client = AmazonApiClient()

    with (
        patch.object(client.settings, "AMAZON_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE"),
        patch.object(
            client.settings, "AMAZON_SECRET_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        ),
        patch.object(client.settings, "AMAZON_REGION", "us-east-1"),
        patch.object(client.settings, "AMAZON_HOST", "webservices.amazon.com"),
    ):
        fixed_time = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)

        payload = {
            "ItemIds": ["B08N5WRWNW"],
            "PartnerTag": "limitra-20",
            "PartnerType": "Associates",
        }
        body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

        headers1 = client._build_aws_headers(body_bytes, "GetItems", timestamp=fixed_time)
        headers2 = client._build_aws_headers(body_bytes, "GetItems", timestamp=fixed_time)

        # Deterministic output
        assert headers1 == headers2

        # Required headers
        assert headers1["Content-Type"] == "application/json; charset=UTF-8"
        assert headers1["Content-Encoding"] == "amz-1.0"
        assert headers1["X-Amz-Date"] == "20261030T120000Z"
        assert headers1["X-Amz-Target"] == "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems"
        assert headers1["Host"] == "webservices.amazon.com"

        auth = headers1["Authorization"]
        assert "AWS4-HMAC-SHA256" in auth
        assert "Credential=AKIAIOSFODNN7EXAMPLE/20261030/us-east-1/ProductAdvertisingAPI/aws4_request" in auth
        assert "SignedHeaders=content-encoding;content-type;host;x-amz-date;x-amz-target" in auth
        assert "Signature=" in auth
        sig = auth.split("Signature=")[1]
        assert len(sig) == 64  # SHA256 hex string
