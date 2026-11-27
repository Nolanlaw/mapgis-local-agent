"""Installer inventory and a non-executable extension contract for MapGIS 10.x."""
import re
import sys

from .base import MapGISProvider


_VERSION = re.compile(r'(?<![\d.])10(?:\.\d+)*(?!\d)')
_CONTRACT = {
    'runtime': 'MapGIS 10.x/64-bit with separate version, architecture and licence verification',
    'local': ['probe_abi', 'describe_operations', 'prepare_native_operation', 'reopen_artifact'],
    'service': ['probe_service_version', 'describe_layer_schema', 'execute_guarded_job', 'reconcile_job'],
}


def _installed_products():
    if sys.platform != 'win32':
        return [], True
    import winreg

    products = set()
    complete = True
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                parent = winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                        access=winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
                continue
            with parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                            name = str(winreg.QueryValueEx(key, 'DisplayName')[0])
                            if 'mapgis' not in name.casefold():
                                continue
                            try:
                                release = str(winreg.QueryValueEx(key, 'DisplayVersion')[0])
                            except FileNotFoundError:
                                release = ''
                            if _VERSION.match(release) or _VERSION.search(name):
                                products.add((name, release))
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
    return [{'name': name, 'version': release} for name, release in sorted(products)], complete


class MapGIS10ProviderStub(MapGISProvider):
    provider_id = 'mapgis10'
    generation = '10.x/x64'

    def probe(self, root):
        products, complete = _installed_products()
        status = ('INSTALLED_EXTENSION_REQUIRED' if products else
                  'NOT_INSTALLED_ON_HOST' if complete else 'INSTALLATION_INVENTORY_INCOMPLETE')
        return {
            'available': False, 'installed': bool(products) if complete or products else None,
            'status': status, 'tier': None,
            'capabilities': {'registered_products': products, 'inventory_complete': complete,
                             'reserved_contract': {key: list(value) if isinstance(value, list) else value
                                                   for key, value in _CONTRACT.items()}},
        }

    def supports_operation(self, op_id):
        return False
