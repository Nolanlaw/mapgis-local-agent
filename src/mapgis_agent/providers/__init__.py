"""Select or inspect an implemented local provider without changing the host."""
from .base import MapGISProvider, ProviderRegistry
from .mapgis10_stub import MapGIS10ProviderStub
from .mapgis67 import MapGIS67Provider
from .open_headless import OpenHeadlessProvider


_REGISTRY = ProviderRegistry((MapGIS67Provider(), OpenHeadlessProvider(), MapGIS10ProviderStub()))


def detect_providers(root, *, operation_id=None):
    return _REGISTRY.detect(root, operation_id)


def select_provider(root, preferred=None):
    return _REGISTRY.select(root, preferred)
