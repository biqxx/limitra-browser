from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.dependencies import CurrentClientDep
from app.suppliers.base import SupplierHealth
from app.suppliers.registry import get_supplier_registry

router = APIRouter(prefix="/api/v1/providers", tags=["Providers"])


@router.get("", summary="List Registered Suppliers")
async def list_providers(client: CurrentClientDep) -> list[dict[str, Any]]:
    registry = get_supplier_registry()
    results = []
    for adapter in registry.list_all():
        results.append(
            {
                "provider": adapter.provider,
                "supported_domains": sorted(adapter.supported_domains),
                "supported_locales": sorted(adapter.supported_locales),
                "capabilities": adapter.capabilities,
                "converter_version": adapter.converter_version,
            }
        )
    return results


@router.get("/{provider}", summary="Get Provider Details")
async def get_provider_details(provider: str, client: CurrentClientDep) -> dict[str, Any]:
    registry = get_supplier_registry()
    adapter = registry.get(provider)
    return {
        "provider": adapter.provider,
        "supported_domains": sorted(adapter.supported_domains),
        "supported_locales": sorted(adapter.supported_locales),
        "capabilities": adapter.capabilities,
        "converter_version": adapter.converter_version,
    }


@router.get("/{provider}/health", response_model=SupplierHealth, summary="Provider Healthcheck")
async def check_provider_health(provider: str, client: CurrentClientDep) -> SupplierHealth:
    registry = get_supplier_registry()
    adapter = registry.get(provider)
    return await adapter.healthcheck()
