"""Contracts for local, generation-specific MapGIS execution routes."""
from abc import ABC, abstractmethod
from pathlib import Path

from ..core import BridgeError


class MapGISProvider(ABC):
    provider_id: str
    generation: str

    @abstractmethod
    def probe(self, root: str | Path) -> dict:
        """Inspect the host and describe runtime readiness without starting a process."""

    def capabilities(self, root: str | Path) -> dict:
        return self.probe(root)['capabilities']

    @abstractmethod
    def supports_operation(self, op_id: str) -> bool:
        """Report whether the provider implements the operation before host preconditions."""


class ProviderRegistry:
    def __init__(self, providers):
        self._providers: dict[str, MapGISProvider] = {}
        for provider in providers:
            if provider.provider_id in self._providers:
                raise ValueError(f'Duplicate provider ID {provider.provider_id}')
            self._providers[provider.provider_id] = provider

    def detect(self, root, operation_id=None) -> list[dict]:
        return [
            {'provider_id': provider.provider_id, 'generation': provider.generation,
             **provider.probe(root),
             **({'supports_operation': provider.supports_operation(operation_id)} if operation_id else {})}
            for provider in self._providers.values()
        ]

    def select(self, root, preferred=None) -> MapGISProvider:
        if preferred is not None:
            provider = self._providers.get(preferred)
            if provider is None:
                raise BridgeError('UNKNOWN_PROVIDER')
            if not provider.probe(root)['available']:
                raise BridgeError('PROVIDER_UNAVAILABLE')
            return provider
        for provider in self._providers.values():
            if provider.probe(root)['available']:
                return provider
        raise BridgeError('PROVIDER_UNAVAILABLE')


def operation_backend(op_id):
    from ..operations import _catalog

    return next((entry['execution'] for entry in _catalog() if entry['id'] == op_id), {})
