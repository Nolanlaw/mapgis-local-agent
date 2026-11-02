"""Viewshed through the bundled GDAL C API, with explicit elevation/range rules."""
from __future__ import annotations

import ctypes as C
import json
import math
import os
from pathlib import Path
from typing import Literal

import numpy as np
import psutil
from pydantic import BaseModel, ConfigDict, Field, StrictInt
import rasterio
from rasterio.windows import Window

from .artifacts import begin_bundle, finish_bundle, fail_bundle, reconcile_bundle
from .core import Bridge, BridgeError, json_text, sha
from .grid_analysis import _gdal, _LOCK, _linear_scale
from .grid_vectors import _grid_sources
from .native_raster import _raster_sources, _snapshot_raster
from .raster import file_sha


class ViewshedParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    observer: list[float] = Field(min_length=2, max_length=2, description='XY in the explicitly declared projected metre frame')
    vertical_unit: Literal['metre', 'foot', 'us_survey_foot']
    band: StrictInt = Field(default=1, ge=1)
    observer_height: float = Field(default=2., ge=0, description='Above the sampled DEM, in vertical_unit')
    target_height: float = Field(default=0., ge=0, description='Above the DEM, in vertical_unit')
    max_distance: float = Field(default=0., ge=0, description='Metres from the selected cell centre;0 means unlimited')
    curvature_coefficient: float = Field(default=0., description='Explicit model coefficient;0 is a planar calculation')
    mode: Literal['edge', 'diagonal', 'max', 'min'] = 'edge'


class LineOfSightParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    observer: list[float] = Field(min_length=2, max_length=2)
    target: list[float] = Field(min_length=2, max_length=2)
    vertical_unit: Literal['metre', 'foot', 'us_survey_foot']
    observer_height: float = Field(default=2., ge=0)
    target_height: float = Field(default=0., ge=0)
    band: StrictInt = Field(default=1, ge=1)


def line_of_sight(file, expected_sha256, parameters, frame, expected_members=None):
    """Read-only DEM ray with fixed surface equality; return composable native geometry."""
    from ._los import cells, trace, UPSTREAM
    options = LineOfSightParameters.model_validate(parameters)
    crs, scale = _linear_scale(frame, options.vertical_unit)
    if crs is None or not math.isclose(crs.axis_info[0].unit_conversion_factor, 1., rel_tol=1e-12):
        raise BridgeError('VISIBILITY_PROJECTED_METRE_FRAME')
    source = Path(file).resolve(strict=True)
    dependencies = _raster_sources(source, expected_sha256, expected_members)
    if source.suffix.lower() == '.grd':
        dependencies = list({v['file']: v for v in dependencies+_grid_sources(source, expected_sha256, frame)}.values())
    with rasterio.open(source) as ds:
        if not 1 <= options.band <= ds.count:
            raise BridgeError('VISIBILITY_BAND')
        if ds.crs and not crs.equals(ds.crs):
            raise BridgeError('VISIBILITY_CRS_MISMATCH')
        t = ds.transform
        if (any(not math.isfinite(v) for v in t) or t.a <= 0 or t.e >= 0 or t.b != 0 or t.d != 0
                or not math.isclose(t.a, -t.e, rel_tol=1e-12)):
            raise BridgeError('VISIBILITY_GRID: square north-up projected cells required')
        endpoints = [ds.index(*xy) for xy in (options.observer, options.target)]
        if any(not (0 <= r < ds.height and 0 <= c < ds.width) for r, c in endpoints):
            raise BridgeError('VISIBILITY_ENDPOINT_OUTSIDE')
        (ra, ca), (rb, cb) = endpoints
        if max(abs(rb-ra), abs(cb-ca))+1 > 20000:
            raise BridgeError('VISIBILITY_RAY_TOO_LONG: at most20000 sampled cells')
        left, top, right, bottom = min(ca, cb), min(ra, rb), max(ca, cb)+1, max(ra, rb)+1
        if (right-left)*(bottom-top) > 2_000_000:
            raise BridgeError('VISIBILITY_TOO_LARGE: coarsen or crop the DEM; at most2M window cells')
        data = ds.read(options.band, window=Window(left, top, right-left, bottom-top), masked=True)
        start, end = (ca-left, ra-top), (cb-left, rb-top)
        ray = cells(start, end)
        mask = np.ma.getmaskarray(data)
        if any(mask[r, c] or not np.isfinite(data.data[r, c]) for c, r in ray):
            raise BridgeError('VISIBILITY_NODATA: a sampled ray cell has missing elevation')
        band_scale, offset = ds.scales[options.band-1], ds.offsets[options.band-1]
        if not math.isfinite(band_scale) or not math.isfinite(offset):
            raise BridgeError('VISIBILITY_VALUE_SCALE')
        elevation = (data.data.astype('float64')*band_scale+offset)/scale
        if any(not np.isfinite(elevation[r, c]) for c, r in ray):
            raise BridgeError('VISIBILITY_NONFINITE_HEIGHT')
        za = float(elevation[start[1], start[0]]+options.observer_height/scale)
        zb = float(elevation[end[1], end[0]]+options.target_height/scale)
        if not math.isfinite(za) or not math.isfinite(zb):
            raise BridgeError('VISIBILITY_NONFINITE_HEIGHT')
        result = trace(elevation, start, end, za, zb)
        samples = []
        for i, value in enumerate(result['samples']):
            row, column = value['row']+top, value['column']+left
            x, y = ds.xy(row, column)
            samples.append({'index': i, 'row': row, 'column': column, 'x': x, 'y': y,
                'distance_m': value['distance_pixels']*t.a, 'terrain_elevation_m': value['ground'],
                'sight_elevation_m': value['line'], 'clearance_m': value['clearance'], 'above_terrain': value['above']})
        sampled = [list(ds.xy(r, c)) for r, c in endpoints]
    fingerprint = sha(json_text({'sources': dependencies, 'parameters': options.model_dump(), 'crs': frame,
                                'algorithm': UPSTREAM}).encode('utf8'))
    feature = lambda identity, geometry, properties: {'type': 'Feature', 'id': str(identity),
        'geometry': geometry, 'properties': {'ID': identity, **properties}}
    collection = lambda features: {'type': 'FeatureCollection', 'features': features}
    map_features = [] if start == end else [feature(1, {'type': 'LineString', 'coordinates': sampled}, {'visible': int(result['visible'])})]
    profiles = [] if start == end else [feature(1, {'type': 'LineString', 'coordinates': [[v['distance_m'], v['terrain_elevation_m']] for v in samples]}, {'role': 'terrain'}),
        feature(2, {'type': 'LineString', 'coordinates': [[0., za], [samples[-1]['distance_m'], zb]]}, {'role': 'sight'})]
    for dependency in dependencies:
        if file_sha(Path(dependency['file'])) != dependency['sha256']:
            raise BridgeError('STALE_SOURCE')
    return {'visible': result['visible'], 'first_obstruction': None if result['first_obstruction_index'] is None else samples[result['first_obstruction_index']],
        'sample_count': len(samples), 'minimum_clearance_m': min(v['clearance_m'] for v in samples),
        'horizontal_distance_m': samples[-1]['distance_m'], 'samples': samples, 'sources': dependencies,
        'source_preserved': True, 'source_value_scale': band_scale, 'source_value_offset': offset,
        'algorithm': UPSTREAM, 'source_crs': frame, 'parameters': options.model_dump(), 'sampled_endpoints': sampled,
        'absolute_endpoint_heights_m': [za, zb], 'map_features': collection(map_features), 'profile_features': collection(profiles),
        'endpoint_features': collection([feature(i+1, {'type': 'Point', 'coordinates': xy},
            {'role': role, 'height_m': height}) for i, (xy, role, height) in enumerate(zip(sampled, ['observer', 'target'], [za, zb]))]),
        'profile_crs': {'kind': 'local', 'name': 'DEM visibility section: X distance metres; Y elevation metres',
                        'unit': 'metre', 'axis_order': 'xy', 'reference_sha256': fingerprint},
        'scope': 'Planar cell-centre Bresenham terrain samples; equality visible, no clearance epsilon or curvature; no vegetation/buildings/field visibility inferred; same-cell ray has no nonzero 2D line'}


def _run_viewshed(source, target, parameters, scale):
    with _LOCK:
        library, runtime = _gdal()
        library.GDALGetRasterBand.restype = C.c_void_p
        library.GDALGetRasterBand.argtypes = [C.c_void_p, C.c_int]
        library.GDALViewshedGenerate.restype = C.c_void_p
        library.GDALViewshedGenerate.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, C.POINTER(C.c_char_p),
            *[C.c_double]*9, C.c_int, C.c_double, C.c_void_p, C.c_void_p, C.c_int, C.POINTER(C.c_char_p)]
        dataset = library.GDALOpenEx(str(source).encode('utf8'), 2, None, None, None)
        if not dataset:
            raise BridgeError('VISIBILITY_OPEN_FAILED: '+library.CPLGetLastErrorMsg().decode('utf8', 'replace'))
        output = None
        try:
            band = library.GDALGetRasterBand(dataset, 1)
            options = (C.c_char_p*2)(b'COMPRESS=DEFLATE', None)
            output = library.GDALViewshedGenerate(band, b'GTiff', str(target).encode('utf8'), options,
                *parameters.observer, parameters.observer_height/scale, parameters.target_height/scale,
                1., 0., 2., 255., parameters.curvature_coefficient,
                {'diagonal': 1, 'edge': 2, 'max': 3, 'min': 4}[parameters.mode], parameters.max_distance,
                None, None, 1, None)
            if not output:
                raise BridgeError('VISIBILITY_PROCESSING_FAILED: '+library.CPLGetLastErrorMsg().decode('utf8', 'replace'))
            closed = library.GDALClose(output)
            output = None
            if closed:
                raise BridgeError('VISIBILITY_OUTPUT_CLOSE_FAILED')
        finally:
            if output:
                library.GDALClose(output)
            library.GDALClose(dataset)
        return runtime|{'api': 'GDALViewshedGenerate'}


def _checkpoint(path, record):
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('x', encoding='utf8') as stream:
        stream.write(json_text(record))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _producer_alive(stage):
    if not (stage/'producer.json').is_file():
        raise BridgeError('ARTIFACT_INCOMPLETE: producer identity was not recorded; use a new request')
    identity = json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process = psutil.Process(identity['pid'])
        return (abs(process.create_time()-identity['start']) < .01
                and Path(process.exe()).resolve() == Path(identity['executable']).resolve())
    except psutil.NoSuchProcess:
        return False


def _finish_viewshed(bridge, request_id):
    receipt = bridge.status(request_id)
    stage = bridge.destination_path(receipt['stage'])
    checkpoint = stage/'viewshed-completed.json'
    if not checkpoint.is_file():
        raise BridgeError('ARTIFACT_INCOMPLETE: no completed GDAL viewshed was replayed; use a new request')
    record = json.loads(checkpoint.read_text(encoding='utf8'))
    if record['payload_sha256'] != sha(json_text(receipt['payload']).encode('utf8')):
        raise BridgeError('VISIBILITY_CHECKPOINT_CHANGED')
    for item in receipt['sources']:
        if file_sha(Path(item['file'])) != item['sha256']:
            raise BridgeError('STALE_SOURCE')
    for item in record['staged_members']:
        path = stage/item['name']
        if not path.resolve().is_relative_to(stage.resolve()) or file_sha(path) != item['sha256']:
            raise BridgeError('VISIBILITY_CHECKPOINT_CHANGED')
    library, runtime = _gdal()
    if (runtime != {**record['gdal'], 'api': 'GDALDEMProcessing'}
            or file_sha(Path(library._name)) != record['gdal']['dll_sha256']):
        raise BridgeError('VISIBILITY_RUNTIME_CHANGED')
    options = ViewshedParameters.model_validate(record['parameters'])
    crs, _ = _linear_scale(receipt['payload']['template']['crs'], options.vertical_unit)
    center = record['observer']['sampled_xy']
    files = stage/'files'
    files.mkdir(exist_ok=True)
    target = files/Path(receipt['output']).name
    corrected = 0
    # Keep the original GDAL result immutable. Only these inexpensive derived
    # files can be written again after an interrupted finalization.
    with rasterio.open(stage/'viewshed-raw.tif') as ds:
        image, profile = ds.read(1), ds.profile
        if ds.count != 1 or ds.dtypes[0] != 'uint8' or ds.crs != crs:
            raise BridgeError('VISIBILITY_OUTPUT_PROFILE')
        if not set(np.unique(image)) <= {0, 1, 2}:
            raise BridgeError('VISIBILITY_UNEXPECTED_CODES')
        if options.max_distance:
            r, c = np.mgrid[:ds.height, :ds.width]
            xs = ds.transform.c+(c+.5)*ds.transform.a
            ys = ds.transform.f+(r+.5)*ds.transform.e
            distance = np.hypot(xs-center[0], ys-center[1])
            outside = distance > options.max_distance+max(1e-9, options.max_distance*1e-12)
            corrected = int(np.count_nonzero(outside & (image != 2)))
            image[outside] = 2
        output_transform = ds.transform
        metrics = {'shape': [ds.height, ds.width],
            'counts': {str(v): int(np.count_nonzero(image == v)) for v in (0, 1, 2)},
            'bounds': list(ds.bounds), 'transform': list(ds.transform)[:6]}
    with rasterio.open(target, 'w', **profile) as ds:
        ds.write(image, 1)
    display = files/(target.stem+'-display.tif')
    colors = np.array([[222, 224, 227], [49, 145, 112], [255, 255, 255]], dtype='uint8')
    with rasterio.open(display, 'w', driver='GTiff', height=image.shape[0], width=image.shape[1], count=3,
        dtype='uint8', crs=crs, transform=output_transform, compress='deflate') as ds:
        ds.write(np.moveaxis(colors[image], 2, 0))
    metadata = {'schema': 'mapgis-agent/visibility/1', 'grid_sha256': file_sha(target),
        'crs': receipt['payload']['template']['crs'], 'parameters': options.model_dump(),
        'observer': record['observer'], 'gdal': record['gdal'], 'metrics': metrics,
        'codes': {'0': 'not_visible', '1': 'visible', '2': 'outside_distance'},
        'range_reclassified_pixels': corrected,
        'source_value_scale': record['source_value_scale'], 'source_value_offset': record['source_value_offset'],
        'vertical_to_metre': record['vertical_to_metre'],
        'display': {'file': display.name, 'sha256': file_sha(display), 'rgb': colors.tolist()},
        'source_preserved': True,
        'scope': 'Numeric DEM cell model; no vegetation/buildings or observed field visibility inferred'}
    (files/(target.name+'.json')).write_text(json_text(metadata), encoding='utf8')
    return finish_bundle(bridge, request_id, files, metadata)


def reconcile_viewshed(bridge, request_id):
    receipt = bridge.status(request_id)
    stage = bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING', 'FAILED'} or (stage/'complete.json').is_file():
        return reconcile_bundle(bridge, request_id)
    if receipt.get('artifact_type') != 'terrain.viewshed':
        raise BridgeError('VISIBILITY_REQUEST_REQUIRED')
    if _producer_alive(stage):
        raise BridgeError('VISIBILITY_PRODUCER_RUNNING: the original process still owns this analysis')
    return _finish_viewshed(bridge, request_id)


def prepare_viewshed(bridge: Bridge, file: str, expected_sha256: str,
                     parameters: dict|ViewshedParameters, template: dict, request_id: str,
                     expected_members: dict|None = None):
    options = ViewshedParameters.model_validate(parameters)
    if (set(template) != {'name', 'crs'} or Path(template['name']).name != template['name']
        or Path(template['name']).suffix.lower() not in {'.tif', '.tiff'}):
        raise BridgeError('VISIBILITY_TEMPLATE: TIFF basename and exact source CRS required')
    crs, scale = _linear_scale(template['crs'], options.vertical_unit)
    if crs is None or not crs.is_projected or not math.isclose(crs.axis_info[0].unit_conversion_factor, 1., rel_tol=1e-12):
        raise BridgeError('VISIBILITY_PROJECTED_METRE_FRAME: reproject the DEM with existing raster warp first')
    source = Path(file).resolve(strict=True)
    raster_sources = _raster_sources(source, expected_sha256, expected_members)
    frame_sources = _grid_sources(source, expected_sha256, template['crs']) if source.suffix.lower() == '.grd' else []
    sources = list({v['file']: v for v in raster_sources+frame_sources}.values())
    with rasterio.open(source) as ds:
        if not 1 <= options.band <= ds.count:
            raise BridgeError('VISIBILITY_BAND')
        if ds.crs and not crs.equals(ds.crs):
            raise BridgeError('VISIBILITY_CRS_MISMATCH')
        t = ds.transform
        if (any(not math.isfinite(v) for v in t) or t.a <= 0 or t.e >= 0 or t.b != 0 or t.d != 0
            or not math.isclose(t.a, -t.e, rel_tol=1e-12)):
            raise BridgeError('VISIBILITY_GRID: square north-up projected cells required')
        row, col = ds.index(*options.observer)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            raise BridgeError('VISIBILITY_OBSERVER_OUTSIDE')
        center = list(ds.xy(row, col))
        # GDAL samples the containing cell, not an interpolated subpixel eye.
        options = options.model_copy(update={'observer': center})
        if options.max_distance:
            radius = math.ceil(options.max_distance/t.a)+1
            left, top = max(0, col-radius), max(0, row-radius)
            right, bottom = min(ds.width, col+radius+1), min(ds.height, row+radius+1)
        else:
            left, top, right, bottom = 0, 0, ds.width, ds.height
        window = Window(left, top, right-left, bottom-top)
        if window.width*window.height > 2_000_000:
            raise BridgeError('VISIBILITY_TOO_LARGE: limit the analysis distance or use a coarser DEM')
        data = ds.read(options.band, window=window, masked=True)
        if np.any(np.ma.getmaskarray(data)) or np.any(~np.isfinite(data.data)):
            raise BridgeError('VISIBILITY_NODATA: GDAL ignores input NoData; explicitly fill/crop before this analysis')
        band_scale, offset = ds.scales[options.band-1], ds.offsets[options.band-1]
        if not math.isfinite(band_scale) or not math.isfinite(offset):
            raise BridgeError('VISIBILITY_VALUE_SCALE')
        elevation = (data.data.astype('float64')*band_scale+offset)/scale
        if np.any(~np.isfinite(elevation)):
            raise BridgeError('VISIBILITY_NONFINITE_HEIGHT')
        work_transform = ds.window_transform(window)
    payload = {'operation': 'terrain.viewshed', 'file': str(source), 'expected_sha256': expected_sha256,
        'parameters': ViewshedParameters.model_validate(parameters).model_dump(exclude_unset=True),
        'template': template, 'expected_members': expected_members, 'viewshed_protocol': 1}
    stage, receipt, new = begin_bundle(bridge, payload, request_id, sources, template['name'])
    if not new:
        return receipt
    try:
        process = psutil.Process()
        _checkpoint(stage/'producer.json', {'pid': process.pid, 'start': process.create_time(),
                                           'executable': process.exe()})
        _snapshot_raster(source, raster_sources, stage/'input')
        working = stage/'elevation.tif'
        profile = {'driver': 'GTiff', 'height': int(window.height), 'width': int(window.width),
                   'count': 1, 'dtype': 'float64', 'crs': crs, 'transform': work_transform, 'compress': 'deflate'}
        with rasterio.open(working, 'w', **profile) as ds:
            ds.write(elevation, 1)
            ds.set_band_unit(1, 'metre')
        raw = stage/'viewshed-raw.tif'
        runtime = _run_viewshed(working, raw, options, scale)
        observer = {'requested_xy': ViewshedParameters.model_validate(parameters).observer, 'sampled_xy': center,
                    'cell': {'row': row, 'column': col}, 'ground_elevation_m': float(elevation[row-top, col-left])}
        staged = [working, raw, *sorted((stage/'input').rglob('*'))]
        members = [{'name': v.relative_to(stage).as_posix(), 'sha256': file_sha(v)} for v in staged if v.is_file()]
        _checkpoint(stage/'viewshed-completed.json', {'payload_sha256': sha(json_text(payload).encode('utf8')),
            'parameters': options.model_dump(), 'observer': observer, 'gdal': runtime,
            'staged_members': members, 'source_value_scale': band_scale, 'source_value_offset': offset,
            'vertical_to_metre': 1/scale})
        return _finish_viewshed(bridge, request_id)
    except Exception as error:
        fail_bundle(bridge, request_id, error)
        raise
