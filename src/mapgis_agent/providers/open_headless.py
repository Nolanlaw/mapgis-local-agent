"""The process-free Python geospatial route."""
from importlib.metadata import PackageNotFoundError, version

from .base import MapGISProvider, operation_backend


_ENGINES = ('shapely', 'pyproj', 'geopandas', 'pyogrio', 'rasterio', 'ezdxf', 'mapgis2shp')
_OPERATIONS = {'abi.probe', 'drc.audit', 'fdr.export', 'labels.place', 'preview.render', 'provider.inspect'}
_BACKENDS = {
    'open_geospatial', 'numpy', 'numpy_scipy', 'native_file_codec', 'python_compatible_mapgis',
    'rasterio_gdal', 'Rasterio/GDAL', 'numpy_scipy_proj', 'rasterio_gdal_geos',
    'adapted_mit_ovobj_grids', 'pinned_wellpathpy_scipy', 'analytic_trigonometry',
    'existing_openpyxl_tables', 'python', 'rasterio_gdal_geos_native_encoder',
    'numpy_scipy_rasterio', 'ezdxf_geos_native_encoder', 'numpy_openpyxl_explicit_background',
    'geos_existing_native_encoder', 'existing_byte_preserving_attribute_schema',
    'literal_openpyxl_decimal_units', 'geos_existing_native_spatial_summary',
    'task_snapshot_existing_native_attribute_encoder', 'exact_native_field_abi_and_existing_schema_bundle',
    'sidecar', 'dbs', 'interchange',
}


class OpenHeadlessProvider(MapGISProvider):
    provider_id = 'open_headless'
    generation = 'open-headless'

    def probe(self, root):
        engines = {}
        for name in _ENGINES:
            try:
                engines[name] = version(name)
            except PackageNotFoundError:
                engines[name] = None
        available = all(engines.values())
        return {
            'available': available,
            'status': 'AVAILABLE' if available else 'MISSING_OPEN_DEPENDENCIES',
            'tier': 'D',
            'capabilities': {'dependency_versions': engines, 'mapgis_process_required': False,
                             'open_geospatial': available},
        }

    def supports_operation(self, op_id):
        return op_id in _OPERATIONS or operation_backend(op_id).get('backend') in _BACKENDS
