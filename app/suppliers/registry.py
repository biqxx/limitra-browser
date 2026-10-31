from __future__ import annotations

import structlog

from app.api.errors import UnsupportedDomainException, UnsupportedProviderException
from app.suppliers.base import SupplierAdapter

logger = structlog.get_logger(__name__)


class SupplierRegistry:
    """
    Registry for isolated supplier adapters.
    Suppliers are added by registering an adapter instance.
    No provider checks or conditionals are needed in core services.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, SupplierAdapter] = {}

    def register(self, adapter: SupplierAdapter) -> None:
        provider = adapter.provider.lower().strip()
        self._adapters[provider] = adapter
        logger.info(
            "registered_supplier_adapter",
            provider=provider,
            domains=list(adapter.supported_domains),
            version=adapter.converter_version,
        )

    def get(self, provider: str) -> SupplierAdapter:
        p = provider.lower().strip()
        if p not in self._adapters:
            raise UnsupportedProviderException(provider)
        return self._adapters[p]

    def find_by_url(self, url: str) -> SupplierAdapter:
        for adapter in self._adapters.values():
            if adapter.recognizes_url(url):
                return adapter
        raise UnsupportedDomainException(f"No registered supplier adapter recognizes URL: {url}")

    def list_all(self) -> list[SupplierAdapter]:
        return list(self._adapters.values())

    def list_providers(self) -> list[str]:
        return list(self._adapters.keys())


_GLOBAL_REGISTRY: SupplierRegistry | None = None


def get_supplier_registry() -> SupplierRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = SupplierRegistry()
        from app.suppliers.amazon.adapter import AmazonAdapter
        from app.suppliers.ebay.adapter import EbayAdapter

        _GLOBAL_REGISTRY.register(EbayAdapter())
        _GLOBAL_REGISTRY.register(AmazonAdapter())
    return _GLOBAL_REGISTRY
