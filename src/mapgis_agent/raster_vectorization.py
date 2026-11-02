"""Thematic pixel footprints to new editable native regions, without smoothing."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pyproj import CRS
import rasterio
from rasterio.features import rasterize, shapes
from shapely import get_num_coordinates, make_valid
from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, mapping, shape

from .core import BridgeError
from .creation import prepare_creation
from .native_raster import _raster_sources, _snapshot_raster
from .raster import explicit_crs, local_crs_matches


class RasterVectorParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    band: StrictInt = Field(default=1, ge=1)
    connectivity: Literal[4, 8] = 4
    max_regions: StrictInt = Field(default=20000, ge=1, le=100000)


def _polygon_parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type in {'MultiPolygon', 'GeometryCollection'}:
        return [p for part in geometry.geoms for p in _polygon_parts(part)]
    # GDAL's 8-connected ring can include retraced, zero-area links between
    # point-touching polygons. These links have no pixel footprint.
    return []


def prepare_raster_vectorization(bridge, file, expected_sha256, parameters, template, request_id,
                                 expected_members=None):
    p = parameters if isinstance(parameters, RasterVectorParameters) else RasterVectorParameters.model_validate(parameters)
    bridge.request_id(request_id)
    if Path(template.get('name', '')).suffix.lower() != '.wp':
        raise BridgeError('VECTORIZE_TEMPLATE: a new native WP basename, explicit CRS and map_scale are required')
    source = Path(file).resolve(strict=True)
    sources = _raster_sources(source, expected_sha256, expected_members)
    target_crs = explicit_crs(template.get('crs', {}))
    folder = bridge.destination_path(bridge.root/'work/raster-vectorization')
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='read-', dir=folder) as temporary:
        snapshot = _snapshot_raster(source, sources, Path(temporary)/'input')
        with rasterio.open(snapshot) as ds:
            if p.band > ds.count:
                raise BridgeError('VECTORIZE_BAND: select an existing one-based band')
            if ds.width*ds.height > 16000000:
                raise BridgeError('VECTORIZE_SIZE: at most16000000 thematic pixels per request')
            if ds.gcps[0] or ds.tags(ns='RPC') or ds.tags(ns='GEOLOCATION'):
                raise BridgeError('VECTORIZE_GEOREFERENCE: explicitly rectify GCP/RPC/geolocation data to an affine grid first')
            grid = ds.transform
            determinant = grid.a*grid.e-grid.b*grid.d
            if not all(math.isfinite(x) for x in grid[:6]) or not math.isfinite(determinant) or determinant == 0:
                raise BridgeError('VECTORIZE_GEOREFERENCE: a finite invertible affine pixel grid is required')
            actual_crs = CRS.from_wkt(ds.crs.to_wkt()) if ds.crs else None
            local_match = (actual_crs is not None and target_crs is None
                           and local_crs_matches(actual_crs, template['crs']))
            if actual_crs is None:
                if target_crs is not None or template['crs'].get('reference_sha256') != expected_sha256:
                    raise BridgeError('VECTORIZE_CRS: an unreferenced raster needs a source-hash-bound explicit local frame')
            elif not local_match and (target_crs is None or not actual_crs.equals(target_crs, ignore_axis_order=True)):
                raise BridgeError('VECTORIZE_CRS: output CRS must equal the observed source; transform in a separate explicit step')
            raw = ds.read(p.band); valid = ds.read_masks(p.band) > 0
            selected = raw[valid]; limits = np.iinfo('int32')
            if (not len(selected) or np.iscomplexobj(raw) or not np.isfinite(selected).all()
                    or np.any(selected != np.trunc(selected))
                    or np.any(selected < limits.min) or np.any(selected > limits.max)):
                raise BridgeError('VECTORIZE_LABELS: nonempty finite exact signed32-bit integer classes required; zero is valid')
            labels = np.where(valid, raw, 0).astype('int32')
            source_frame = {'width': ds.width, 'height': ds.height, 'transform': list(grid)[:6],
                            'crs_wkt': ds.crs.to_wkt() if ds.crs else None}

    pixel_geometries = []; features = []; repaired = 0; vertices = 0; regions = 0
    class_pixels = {int(k): int(n) for k, n in zip(*np.unique(labels[valid], return_counts=True))}
    totals = {label: {'class_id': label, 'pixel_count': 0, 'region_count': 0, 'coordinate_area': 0.}
              for label in class_pixels}
    matrix = [grid.a, grid.b, grid.d, grid.e, grid.c, grid.f]
    for index, (geometry, value) in enumerate(shapes(labels, mask=valid, connectivity=p.connectivity), 1):
        if index > p.max_regions:
            raise BridgeError('VECTORIZE_REGION_LIMIT: thematic complexity exceeds explicit max_regions; clean/crop explicitly')
        polygon = shape(geometry)
        if not polygon.is_valid:
            parts = _polygon_parts(make_valid(polygon)); repaired += 1
            if not parts:
                raise BridgeError('VECTORIZE_POLYGON_REPAIR: no polygonal pixel footprint')
            polygon = parts[0] if len(parts) == 1 else MultiPolygon(parts)
        if not polygon.is_valid or polygon.is_empty:
            raise BridgeError('VECTORIZE_POLYGON_REPAIR: valid polygonal coverage could not be preserved')
        vertices += int(get_num_coordinates(polygon))
        if vertices > 2000000:
            raise BridgeError('VECTORIZE_VERTEX_LIMIT: at most2000000 output ring vertices')
        pixels = round(polygon.area)
        if pixels < 1 or abs(polygon.area-pixels) > 1e-8:
            raise BridgeError('VECTORIZE_PIXEL_AREA_DIFF: region area is not an exact integral pixel footprint')
        label = int(value); regions = index
        pixel_geometries.append((polygon, label))
        # The original WP/independent region walker can join multiple exteriors
        # through a shared point and reconstruct a disconnected interior. Keep
        # each valid polygon in its own native record, with a shared logical
        # region_id so that8-connectivity is retained without a self-touch ring.
        for part_id, part in enumerate(_polygon_parts(polygon), 1):
            part_pixels = round(part.area)
            if part_pixels < 1 or abs(part.area-part_pixels) > 1e-8:
                raise BridgeError('VECTORIZE_PIXEL_AREA_DIFF: nonintegral polygonal part')
            mapped = affine_transform(part, matrix)
            if mapped.is_empty or not mapped.is_valid or not all(math.isfinite(x) for x in mapped.bounds):
                raise BridgeError('VECTORIZE_MAP_PRECISION: source affine cannot preserve a valid finite region')
            features.append({'type': 'Feature', 'id': f'region-{index:06d}:part-{part_id}', 'geometry': mapping(mapped),
                             'properties': {'region_id': index, 'part_id': part_id, 'class_id': label,
                                            'pixel_count': part_pixels, 'coord_area': part_pixels*abs(determinant)}})
            if len(features) > 100000:
                raise BridgeError('VECTORIZE_NATIVE_PART_LIMIT: at most100000 polygonal native records')
        total = totals[label]; total['pixel_count'] += pixels; total['region_count'] += 1
        total['coordinate_area'] += pixels*abs(determinant)
    coverage = rasterize(((g, 1) for g, _ in pixel_geometries), out_shape=valid.shape, dtype='uint8') > 0
    saved_labels = rasterize(pixel_geometries, out_shape=valid.shape, dtype='int32')
    if (not np.array_equal(coverage, valid) or not np.array_equal(saved_labels[valid], labels[valid])
            or any(totals[label]['pixel_count'] != count for label, count in class_pixels.items())):
        raise BridgeError('VECTORIZE_PIXEL_ROUNDTRIP_DIFF: polygon coverage/class counts differ from source pixels')
    metadata = {'schema': 'mapgis-agent/raster-vectorization/1', 'parameters': p.model_dump(),
        'backend': 'Rasterio/GDAL polygonize; GEOS make_valid for zero-area point-touching ring links',
        'rasterio_version': rasterio.__version__, 'gdal_version': rasterio.__gdal_version__,
        'source_grid': source_frame, 'valid_pixels': int(valid.sum()), 'invalid_pixels': int((~valid).sum()),
        'regions': regions, 'native_polygon_parts': len(features), 'vertices': vertices, 'repaired_point_touching_regions': repaired,
        'classes': [totals[label] for label in sorted(totals)],
        'pixel_roundtrip': 'EXACT_CLASS_AND_VALIDITY_BEFORE_NATIVE_ENCODING',
        'coordinate_area_per_pixel': abs(determinant),
        'area_units': 'squared source XY coordinate units; not geodesic area or inferred square metres',
        'connectivity_policy': 'one valid polygon per native record; point-touching8-connected parts share region_id and have distinct part_id',
        'geometry_policy': 'pixel edges and holes retained; no smoothing, simplification, resampling or inferred geology',
        'native_policy': 'existing new-WP arc-walk encoder and independent geometry/attribute readback; original BAS save/reopen is a separate operation'}
    output_template = {**template, 'qa': {**template.get('qa', {}), 'raster_vectorization': metadata},
        'provenance': {**template.get('provenance', {}), 'raster_vectorization': {
            'file': str(source), 'expected_sha256': expected_sha256, 'expected_members': expected_members,
            'parameters': p.model_dump(), 'protocol': 2}}}
    return prepare_creation(bridge, {'type': 'FeatureCollection', 'features': features}, output_template,
                            request_id, sources=sources)
