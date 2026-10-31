from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.suppliers.config_schema import SupplierConfigSchema


def test_load_ebay_selectors_yaml():
    yaml_path = Path("app/suppliers/ebay/selectors.yaml")
    assert yaml_path.exists()
    config = SupplierConfigSchema.load_from_file(str(yaml_path))
    assert config.provider == "ebay"
    assert "ebay.com" in config.domains
    assert len(config.search.result_container) > 0
    assert len(config.product.title) > 0


def test_load_amazon_selectors_yaml():
    yaml_path = Path("app/suppliers/amazon/selectors.yaml")
    assert yaml_path.exists()
    config = SupplierConfigSchema.load_from_file(str(yaml_path))
    assert config.provider == "amazon"
    assert "amazon.com" in config.domains
    assert len(config.search.result_container) > 0
    assert len(config.product.title) > 0


def test_invalid_yaml_schema_raises_error():
    invalid_yaml = """
    provider: test_bad
    domains: []
    """
    with pytest.raises(ValidationError):
        SupplierConfigSchema.load_from_yaml(invalid_yaml)
