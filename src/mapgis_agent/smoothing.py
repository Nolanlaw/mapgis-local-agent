"""Cubic interpolation for the existing native line/arc vertex editor.

SciPy solves the spline. Native files retain editable polylines: on each cubic
knot span, ||C''|| is bounded by its endpoint values. Linear interpolation error
is at most max(||C''||)*h**2/8, so sampling has a declared source-unit bound.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.interpolate import make_interp_spline

from .core import BridgeError, native_ids
from .topology import _wp_arc_records


def _smooth(points, tolerance):
    original = np.asarray(points, dtype=float)
    if original.ndim != 2 or original.shape[1] != 2 or not np.isfinite(original).all():
        raise BridgeError('SMOOTH_COORDINATES: finite XY vertices required')
    precision = 256*max(math.ulp(float(value)) for value in original.flat)
    if tolerance < precision:
        raise BridgeError('SMOOTH_PRECISION: chord error is below source-coordinate precision')
    keep = np.r_[True, np.any(original[1:] != original[:-1], axis=1)]
    points = original[keep]
    closed = np.array_equal(points[0], points[-1])
    if len(points) < (4 if closed else 2):
        raise BridgeError('SMOOTH_COLLAPSED: two open or three distinct closed vertices required')
    origin = points[0].copy()
    relative = points-origin
    lengths = np.hypot(*(relative[1:]-relative[:-1]).T)
    total = float(lengths.sum())
    if not math.isfinite(total) or total <= 0:
        raise BridgeError('SMOOTH_COORDINATES: finite nonzero chord lengths required')
    t = np.r_[0., np.cumsum(lengths)/total]
    if not np.all(np.diff(t)>0):
        raise BridgeError('SMOOTH_PRECISION: indistinguishable chord parameters')
    try:
        spline = make_interp_spline(t, relative, k=3, bc_type='periodic' if closed else 'natural')
        knots = np.unique(np.r_[t, spline.t[(spline.t>=0.) & (spline.t<=1.)]])
        second = spline.derivative(2)(knots)
        curvature = np.hypot(*second.T)
    except (ValueError, ArithmeticError) as exc:
        raise BridgeError('SMOOTH_INTERPOLATION: '+str(exc)) from exc
    if not np.isfinite(curvature).all():
        raise BridgeError('SMOOTH_PRECISION: nonfinite derivative bound')
    samples = [0.]
    bound = 0.
    for i, (start, end) in enumerate(zip(knots, knots[1:])):
        maximum = float(max(curvature[i:i+2]))
        estimate = (end-start)*math.sqrt(maximum)/math.sqrt(tolerance)/math.sqrt(8*0.95)
        if not math.isfinite(estimate) or estimate > 99999:
            raise BridgeError('SMOOTH_VERTEX_LIMIT: at most 100000 vertices per curve')
        count = max(1, math.ceil(estimate))
        if len(samples)+count > 100000:
            raise BridgeError('SMOOTH_VERTEX_LIMIT: at most 100000 vertices per curve')
        samples.extend(np.linspace(start,end,count+1)[1:].tolist())
        bound = max(bound, maximum*((end-start)/count)**2/8)
    result = spline(samples)+origin
    # Keep input interpolation vertices exact, including both shared endpoints.
    for value, point in zip(t, points):
        result[np.searchsorted(samples,value)] = point
    result[0], result[-1] = original[0], original[-1]
    if not np.isfinite(result).all():
        raise BridgeError('SMOOTH_PRECISION: nonfinite output')
    return result.tolist(), {'input_vertices':len(original), 'output_vertices':len(result),
        'closed':bool(closed), 'boundary':'periodic' if closed else 'natural',
        'chord_error_bound':bound+precision/128}


def prepare_smoothing(operation, parameters, reader, source):
    is_arc = operation == 'arc.smooth'
    key, identity_key = ('arc_ids','arc_id') if is_arc else ('record_ids','record_id')
    if reader.shapeType != ('POLYGON' if is_arc else 'LINE'):
        raise BridgeError('SMOOTH_TYPE: line.smooth requires WL; arc.smooth requires WP')
    if reader.crs and reader.crs.is_geographic:
        raise BridgeError('SMOOTH_FRAME: projected or declared local coordinates required')
    if not isinstance(parameters,dict) or set(parameters)!={key,'max_chord_error'}:
        raise BridgeError('SMOOTH_PARAMETERS: explicit IDs and max_chord_error in source units required')
    ids, tolerance = parameters[key], parameters['max_chord_error']
    if type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance<=0:
        raise BridgeError('SMOOTH_TOLERANCE: positive finite source units required')
    if is_arc:
        if source is None:raise BridgeError('SMOOTH_TYPE: saved WP required')
        records = {aid:arc['coordinates'] for aid,arc in _wp_arc_records(source)[3].items()}
    else:
        records = {rid:list(g.coords) for rid,g in zip(native_ids(reader),reader.geom)}
    if (not isinstance(ids,list) or not 1<=len(ids)<=1000 or any(type(rid) is not int for rid in ids)
            or len(set(ids))!=len(ids) or not set(ids)<=records.keys()):
        raise BridgeError('SMOOTH_IDS: 1..1000 unique active physical IDs required')
    updates, details, total = [], [], 0
    for rid in sorted(ids):
        coordinates, detail = _smooth(records[rid],tolerance)
        total += len(coordinates)
        if total>1000000:raise BridgeError('SMOOTH_VERTEX_LIMIT: at most 1000000 vertices per transaction')
        updates.append({identity_key:rid,'coordinates':coordinates})
        details.append({identity_key:rid,**detail})
    return {'updates':updates, 'smoothing':{'algorithm':'scipy-natural-periodic-cubic-v1',
        'max_chord_error':tolerance, 'coordinate_frame':'source', 'curves':details}}
