from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class SelectorField(BaseModel):
    selectors: list[str] = Field(default_factory=list)
    attribute: str | None = None
    regex: str | None = None
    transform: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> SelectorField:
        if isinstance(raw, str):
            return cls(selectors=[raw])
        elif isinstance(raw, list):
            return cls(selectors=[str(s) for s in raw])
        elif isinstance(raw, dict):
            sels = raw.get("selectors") or raw.get("selector") or []
            if isinstance(sels, str):
                sels = [sels]
            return cls(
                selectors=sels,
                attribute=raw.get("attribute"),
                regex=raw.get("regex"),
                transform=raw.get("transform"),
            )
        return cls()


class SearchSelectors(BaseModel):
    url_template: str = ""
    result_container: list[str] = Field(default_factory=list)
    external_id: dict[str, Any] = Field(default_factory=dict)
    title: list[str] = Field(default_factory=list)
    url: list[str] = Field(default_factory=list)
    price: list[str] = Field(default_factory=list)
    original_price: list[str] = Field(default_factory=list)
    image: list[str] = Field(default_factory=list)
    next_page: list[str] = Field(default_factory=list)
    rating: list[str] = Field(default_factory=list)
    review_count: list[str] = Field(default_factory=list)


class TableSelector(BaseModel):
    rows: list[str] = Field(default_factory=list)
    key: list[str] = Field(default_factory=list)
    value: list[str] = Field(default_factory=list)


class VariantSelectors(BaseModel):
    groups: list[str] = Field(default_factory=list)
    option: list[str] = Field(default_factory=list)
    name: list[str] = Field(default_factory=list)
    price: list[str] = Field(default_factory=list)


class ProductSelectors(BaseModel):
    title: list[str] = Field(default_factory=list)
    subtitle: list[str] = Field(default_factory=list)
    description: list[str] = Field(default_factory=list)
    bullet_points: list[str] = Field(default_factory=list)
    price: list[str] = Field(default_factory=list)
    original_price: list[str] = Field(default_factory=list)
    currency: list[str] = Field(default_factory=list)
    availability: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    specifications: TableSelector | None = None
    variants: VariantSelectors | None = None
    brand: list[str] = Field(default_factory=list)
    seller: list[str] = Field(default_factory=list)
    shipping: list[str] = Field(default_factory=list)


class SupplierConfigSchema(BaseModel):
    provider: str
    version: int = 1
    domains: list[str] = Field(default_factory=list)
    locales: list[str] = Field(default_factory=lambda: ["en-US"])
    search: SearchSelectors = Field(default_factory=SearchSelectors)
    product: ProductSelectors = Field(default_factory=ProductSelectors)
    locale_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one domain must be specified in domains list.")
        return [d.strip().lower() for d in v]

    @classmethod
    def load_from_yaml(cls, yaml_content: str) -> SupplierConfigSchema:
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            raise ValueError("YAML content must resolve to a dictionary.")
        return cls.model_validate(data)

    @classmethod
    def load_from_file(cls, filepath: str) -> SupplierConfigSchema:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        return cls.load_from_yaml(content)
