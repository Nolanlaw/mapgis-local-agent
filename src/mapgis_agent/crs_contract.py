"""Bind modern CRS metadata to unchanged MapGIS 6.x drawing coordinates."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import struct
import warnings

from pyproj import CRS
from pyproj.exceptions import CRSError

from .core import BridgeError, sha


SCHEMA_VERSION = 'mapgis-crs-sidecar/v1'
NATIVE_HEADER = {
    'mode': 'user_drawing_unscaled', 'coordinate_type': 0, 'unit_code': 0,
    'projection_code': 0, 'ellipsoid_code': 0, 'stored_map_scale': 0.0,
    'geodetic_reference': 'declared_wkt2_only',
}


def sidecar_path(file_path):
    path = Path(file_path)
    return path.with_name(path.name + '.crs.json')


def _parameter(crs, code, angular=False):
    operation = crs.coordinate_operation
    if operation is None:
        return None
    for parameter in operation.params:
        if parameter.code == code:
            if angular and math.isclose(parameter.unit_conversion_factor, math.pi / 180, rel_tol=1e-12):
                return float(parameter.value)
            value = parameter.value * parameter.unit_conversion_factor
            return math.degrees(value) if angular else float(value)
    return None


def _zone(epsg, false_easting):
    if epsg is not None:
        for start, end, first, width in ((4491, 4501, 13, 6), (4502, 4512, 13, 6),
                                         (4513, 4533, 25, 3), (4534, 4554, 25, 3)):
            if start <= epsg <= end:
                return first + epsg - start, width
    if false_easting is not None:
        prefixed = (false_easting - 500_000) / 1_000_000
        if 1 <= prefixed <= 60 and prefixed.is_integer():
            return int(prefixed), None
    return None, None


def _details(crs):
    epsg = crs.to_epsg(min_confidence=100)
    offset = _parameter(crs, '8806')
    zone, width = _zone(epsg, offset)
    el = crs.ellipsoid
    return {'declared_epsg': epsg, 'false_easting_m': offset, 'zone': zone,
            'zone_width_degrees': width, 'central_meridian': _parameter(crs, '8802', angular=True),
            'ellipsoid': {'name': el.name, 'semi_major_m': el.semi_major_metre,
                          'semi_minor_m': el.semi_minor_metre, 'inverse_flattening': el.inverse_flattening}}


def _check_native_header(blob):
    if (len(blob) < 260 or blob[91:93] != b'\0\0' or blob[109:111] != b'\0\0'
            or struct.unpack_from('<d', blob, 143)[0] != 0.0):
        raise BridgeError('CRS_SIDECAR_NATIVE_HEADER: expected an unscaled user drawing')


def make_crs_sidecar(file_name, blob, crs):
    _check_native_header(blob)
    if not (crs.is_geographic or crs.is_projected) or len(crs.axis_info) != 2:
        raise BridgeError('CRS_SIDECAR_2D_EARTH_CRS_REQUIRED')
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='You will likely lose important projection information')
        proj4 = crs.to_proj4()
    return {'schema_version': SCHEMA_VERSION, 'file_name': file_name, 'file_sha256': sha(blob),
            'declared_wkt2': crs.to_wkt(version='WKT2_2019'), 'declared_proj4': proj4, 'axis_order': 'xy',
            'ground_coordinates_preserved': True, 'native_header_representation': dict(NATIVE_HEADER),
            **_details(crs)}


def read_crs_sidecar(file_path, *, file_bytes=None):
    """Return the validated document and WKT CRS, or None for a legacy file."""
    path = Path(file_path)
    sidecar = sidecar_path(path)
    if not os.path.lexists(sidecar):
        return None
    try:
        if not sidecar.is_file() or sidecar.is_symlink() or sidecar.stat().st_size > 1024 * 1024:
            raise ValueError('expected a regular JSON sidecar')
        contract = json.loads(sidecar.read_text(encoding='utf-8'))
        if not isinstance(contract, dict) or contract.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('unsupported schema')
    except (OSError, ValueError) as exc:
        raise BridgeError(f'CRS_SIDECAR_INVALID: {sidecar.name}') from exc
    data = path.read_bytes() if file_bytes is None else file_bytes
    if contract.get('file_name') != path.name or contract.get('file_sha256') != sha(data):
        raise BridgeError(f'CRS_SIDECAR_STALE: {sidecar.name}')
    _check_native_header(data)
    try:
        crs = CRS.from_wkt(contract['declared_wkt2'])
        if (not (crs.is_geographic or crs.is_projected) or len(crs.axis_info) != 2 or
                contract.get('axis_order') != 'xy' or contract.get('ground_coordinates_preserved') is not True or
                contract.get('native_header_representation') != NATIVE_HEADER or
                not isinstance(contract.get('declared_proj4'), str)):
            raise ValueError('invalid declared 2D contract')
        for key, actual in _details(crs).items():
            if key == 'ellipsoid':
                supplied = contract[key]
                if any(not math.isclose(supplied[k], actual[k], rel_tol=1e-12, abs_tol=1e-12)
                       for k in ('semi_major_m', 'semi_minor_m', 'inverse_flattening')):
                    raise ValueError(key)
            elif contract[key] != actual:
                raise ValueError(key)
    except (CRSError, KeyError, TypeError, ValueError) as exc:
        raise BridgeError(f'CRS_SIDECAR_INVALID: {sidecar.name}') from exc
    return contract, crs


def resolve_effective_crs(file_path, reader, *, file_bytes=None):
    contract = read_crs_sidecar(file_path, file_bytes=file_bytes)
    return contract[1] if contract is not None else (getattr(reader, 'crs', None) or None)
