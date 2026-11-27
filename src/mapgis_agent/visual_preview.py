"""Deterministic, GUI-free MapGIS vector previews and pixel comparisons."""
from __future__ import annotations

from collections import Counter
from functools import cache
from html import escape
import io
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from pymapgis.reader import _read_crs
from shapely.geometry import box

from .core import BridgeError, annotation_text, native_ids, read_native, sha
from .drc import frame_unit


PAPER = (249, 248, 243)
INK = (42, 52, 52)
MUTED = (89, 103, 101)
FAULT = (191, 39, 37)
FILLS = ((219, 228, 203), (235, 214, 183), (200, 220, 225), (225, 210, 226),
         (239, 231, 172), (212, 226, 219), (232, 207, 198), (203, 214, 233))
FONT = 'Microsoft YaHei'


@cache
def _vips():
    try:
        import pyvips
    except (ImportError, OSError) as exc:
        raise BridgeError('PREVIEW_RUNTIME: pyvips with libvips is required') from exc
    return pyvips


@cache
def _raster_accelerator():
    try:
        import cv2
    except (ImportError, OSError):
        return None
    return cv2


def _read_png(path):
    img = _vips().Image.new_from_buffer(Path(path).read_bytes(), '')
    if img.bands not in (3, 4):
        img = img.colourspace('srgb')
    if img.hasalpha():
        img = img.flatten(background=list(PAPER))
    if img.format == 'ushort':
        img = (img / 257).cast('uchar')
    img = img.cast('uchar')
    return np.frombuffer(img.write_to_memory(), dtype=np.uint8).reshape(img.height, img.width, 3)


def _save(array, path, dpi):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = array.shape[:2]
    img = _vips().Image.new_from_memory(array.tobytes(), width, height, 3, 'uchar')
    data = img.copy(interpretation='srgb', xres=dpi / 25.4, yres=dpi / 25.4).pngsave_buffer(compression=6)
    fd, temporary = tempfile.mkstemp(prefix='.preview-', suffix='.png', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(path)


def _line(image, points, color, thickness=1):
    cv2 = _raster_accelerator()
    points = np.asarray(points, dtype=np.int32)
    if len(points) < 2:
        return
    if cv2 is not None:
        cv2.polylines(image, [points], False, color, thickness, cv2.LINE_8)
        return
    for a, b in zip(points[:-1], points[1:]):
        count = max(abs(b - a)) + 1
        xy = np.rint(np.linspace(a, b, count)).astype(int)
        for dy in range(-thickness // 2 + 1, thickness // 2 + 1):
            for dx in range(-thickness // 2 + 1, thickness // 2 + 1):
                x, y = xy[:, 0] + dx, xy[:, 1] + dy
                keep = (x >= 0) & (x < image.shape[1]) & (y >= 0) & (y < image.shape[0])
                image[y[keep], x[keep]] = color


def _fill(image, rings, color):
    if not rings or not len(rings[0]):
        return
    all_points = np.concatenate(rings)
    x0, y0 = np.maximum(all_points.min(axis=0), 0)
    x1, y1 = np.minimum(all_points.max(axis=0) + 1, (image.shape[1], image.shape[0]))
    if x0 >= x1 or y0 >= y1:
        return
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2 = _raster_accelerator()
    if cv2 is not None:
        cv2.fillPoly(mask, [np.asarray(r - (x0, y0), dtype=np.int32) for r in rings], 1)
    else:
        edges = np.concatenate([np.stack([ring[:-1], ring[1:]], axis=1) for ring in rings])
        a, b = edges[:, 0], edges[:, 1]
        for y in range(y0, y1):
            crosses = (a[:, 1] > y) != (b[:, 1] > y)
            left, right = a[crosses], b[crosses]
            xs = np.sort(left[:, 0] + (y - left[:, 1]) * (right[:, 0] - left[:, 0]) / (right[:, 1] - left[:, 1]))
            for lo, hi in zip(xs[::2], xs[1::2]):
                start, end = max(0, math.ceil(lo) - x0), min(x1 - x0, math.floor(hi) + 1 - x0)
                if start < end:
                    mask[y - y0, start:end] = 1
    image[y0:y1, x0:x1][mask.astype(bool)] = color


def _rect(image, x, y, width, height, color, thickness=1):
    _line(image, [(x, y), (x + width, y), (x + width, y + height),
                  (x, y + height), (x, y)], color, thickness)


def _dot(image, x, y, radius, color):
    x0, x1 = max(0, x - radius), min(image.shape[1], x + radius + 1)
    y0, y1 = max(0, y - radius), min(image.shape[0], y + radius + 1)
    if x0 < x1 and y0 < y1:
        yy, xx = np.ogrid[y0:y1, x0:x1]
        image[y0:y1, x0:x1][(xx-x)**2 + (yy-y)**2 <= radius**2] = color


def _text(image, value, x, y, color=INK, size=14, *, baseline=False, angle=0):
    if not value:
        return
    vips = _vips()
    try:
        mask = vips.Image.text(escape(value), font=f'{FONT} {size}', dpi=72)
        if angle:
            mask = mask.similarity(angle=-angle, background=[0])
    except vips.Error as exc:
        raise BridgeError('PREVIEW_FONT: libvips text support and a local CJK font are required') from exc
    if baseline:
        y -= mask.height
    x0, x1 = max(0, x), min(image.shape[1], x + mask.width)
    y0, y1 = max(0, y), min(image.shape[0], y + mask.height)
    if x0 >= x1 or y0 >= y1:
        return
    alpha = np.frombuffer(mask.write_to_memory(), dtype=np.uint8).reshape(mask.height, mask.width)
    alpha = alpha[y0-y:y1-y, x0-x:x1-x, None].astype(np.uint16)
    region = image[y0:y1, x0:x1]
    region[:] = (region.astype(np.uint16) * (255 - alpha) + np.asarray(color, dtype=np.uint16) * alpha + 127) // 255


def _read_layers(files):
    layers = []
    for source in files:
        row = source if isinstance(source, dict) else {'file': source}
        path = Path(row['file']).resolve(strict=True)
        blob, reader, sections = read_native(path)
        digest = sha(blob)
        if row.get('expected_sha256') is not None and row['expected_sha256'] != digest:
            raise BridgeError('STALE_SOURCE')
        _, drawing_factor = _read_crs(io.BytesIO(blob))
        texts = {rid: annotation_text(blob, sections, rid) for rid in native_ids(reader)} if reader.shapeType == 'POINT' else {}
        layers.append((row.get('name', path.stem), path, reader, texts, drawing_factor))
    if not layers or not any(any(not geom.is_empty for geom in layer[2].geom) for layer in layers):
        raise BridgeError('PREVIEW_EMPTY: a preview needs nonempty native geometry')
    from .drc import check_frames
    for _, _, reader, _, _ in layers[1:]:
        check_frames(layers[0][2].crs, reader.crs)
    return layers


def _fit(bounds, width, height, margin):
    x0, y0, x1, y1 = bounds
    span = max(x1 - x0, y1 - y0)
    if not math.isfinite(span) or span < 0:
        raise BridgeError('PREVIEW_BOUNDS')
    span = span or 1.
    scale = min((width - 2 * margin) / max(x1 - x0, span * .05),
                (height - 2 * margin) / max(y1 - y0, span * .05))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = (width - 1) / 2 / scale, (height - 1) / 2 / scale
    bounds = (cx - dx, cy - dy, cx + dx, cy + dy)

    def project(points):
        xy = np.asarray(points, dtype=float)[..., :2]
        pixel = (xy - (bounds[0], bounds[3])) * (scale, -scale)
        return np.clip(np.rint(pixel), -1_000_000, 1_000_000).astype(np.int32)

    return bounds, scale, project


def _draw_feature(image, geom, project, fill, stroke):
    kind = geom.geom_type
    if geom.is_empty:
        return
    if kind == 'Polygon':
        rings = [project(ring.coords) for ring in [geom.exterior, *geom.interiors]]
        _fill(image, rings, fill)
        for ring in rings:
            _line(image, ring, stroke)
    elif kind in ('LineString', 'LinearRing'):
        _line(image, project(geom.coords), stroke, 2)
    elif kind == 'Point':
        x, y = project(geom.coords)[0]
        _dot(image, int(x), int(y), 4, stroke)
    elif hasattr(geom, 'geoms'):
        for part in geom.geoms:
            _draw_feature(image, part, project, fill, stroke)


def _number(value):
    return f'{value:,.4f}'.rstrip('0').rstrip('.') if abs(value) < 1000 else f'{value:,.1f}'.removesuffix('.0')


def _scale_bar(image, bounds, scale, crs, x, y, length):
    max_map = length / scale
    label, per_unit = frame_unit(crs) or 'map units', 1.
    if crs and crs.is_geographic:
        west, south, east, north = bounds
        middle = (south + north) / 2
        _, _, meters = crs.get_geod().inv(west, middle, min(east, west + max_map), middle)
        per_unit, label = abs(meters) / max_map, 'm'
    if not math.isfinite(per_unit) or per_unit <= 0:
        per_unit, label = 1., 'map units'
    distance = max_map * per_unit
    magnitude = 10 ** math.floor(math.log10(distance))
    nice = max(v for v in (magnitude, 2 * magnitude, 5 * magnitude) if v <= distance)
    pixels = max(1, round(nice / per_unit * scale))
    _line(image, [(x, y-4), (x, y), (x+pixels, y), (x+pixels, y-4)], INK, 2)
    if label == 'm' and nice >= 1000:
        nice, label = nice / 1000, 'km'
    _text(image, f'{nice:g} {label}', x, y + 6, size=11)


def _render(layers, output_png, width, height, dpi, drc_report, title, view=None, focus=None):
    if (type(width) is not int or type(height) is not int or not 400 <= width <= 8192
            or not 320 <= height <= 8192 or type(dpi) is not int or not 72 <= dpi <= 1200):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: preview dimensions or DPI out of range')
    image = np.full((height, width, 3), PAPER, dtype=np.uint8)
    left, top = 80, 58
    sidebar = min(230, max(120, width // 5))
    map_width, map_height = width - left - sidebar - 16, height - top - 82
    canvas = np.full((map_height, map_width, 3), (255, 255, 251), dtype=np.uint8)
    all_bounds = np.asarray([geom.bounds for _, _, reader, _, _ in layers for geom in reader.geom if not geom.is_empty])
    data_bounds = tuple(np.r_[all_bounds[:, :2].min(axis=0), all_bounds[:, 2:].max(axis=0)])
    bounds, scale, project = _fit(data_bounds if view is None else view, map_width, map_height, 15 if view is None else 0)
    frame = box(*bounds)
    total_texts = 0
    for layer_index, (name, _, reader, texts, factor) in enumerate(layers):
        fill = FILLS[layer_index % len(FILLS)]
        stroke = (74, 103, 93) if reader.shapeType == 'POLYGON' else (52, 85, 104)
        for row, (geom, rid) in enumerate(zip(reader.geom, native_ids(reader))):
            if geom.is_empty or not frame.intersects(geom.envelope):
                continue
            # Clipping caps pixel work for zooms. Preview still shows invalid geometries.
            if not frame.covers(geom.envelope) and geom.is_valid:
                geom = geom.intersection(frame)
            if text := texts.get(rid):
                if geom.is_empty:
                    continue
                x, y = map(int, project(geom.coords)[0])
                style = reader.point_params.iloc[row]
                height_map = float(style.get('height', 1.) or 1.) * factor
                size = min(32, max(9, round(height_map * scale))) if math.isfinite(height_map) else 12
                angle = float(style.get('angle', 0.) or 0.)
                _text(canvas, text[:180], x, y, stroke, size, baseline=True,
                      angle=angle if math.isfinite(angle) else 0)
                total_texts += 1
            else:
                _draw_feature(canvas, geom, project, fill, stroke)
        row_y = top + 30 + layer_index * 24
        if row_y + 18 < height - 56:
            x = left + map_width + 16
            image[row_y+2:row_y+14, x:x+16] = fill if reader.shapeType == 'POLYGON' else stroke
            _text(image, f'{name[:18]} ({len(reader.geom)})', x + 23, row_y, size=12)
    _text(image, title or 'MapGIS · 地质图预览', left, 17, size=24)
    _text(image, 'Layers / 图层', left + map_width + 16, top + 3, size=14)
    placements = []
    violations = (drc_report or {}).get('violations', [])
    for item in ([focus] if focus else violations):
        xy = item.get('coords')
        if not xy or not all(math.isfinite(v) for v in xy):
            continue
        x, y = map(int, project([xy])[0])
        if not (0 <= x < map_width and 0 <= y < map_height):
            continue
        _rect(canvas, x - 9, y - 9, 18, 18, FAULT, 2)
        _line(canvas, [(x - 4, y), (x + 4, y)], FAULT, 2)
        _line(canvas, [(x, y - 4), (x, y + 4)], FAULT, 2)
        placements.append({'rule_id': item['rule_id'], 'pixel': [x + left, y + top]})
        if focus:
            _text(image, f"{item['rule_id']}   ({_number(xy[0])}, {_number(xy[1])})",
                  left, height - 58, color=FAULT, size=14)
    if violations and not focus:
        counts = Counter(item['severity'] for item in violations)
        _text(image, f"DRC  {counts['ERROR']} errors  {counts['WARNING']} warnings", left, height - 24, FAULT, 13)
    image[top:top+map_height, left:left+map_width] = canvas
    _rect(image, left-1, top-1, map_width+1, map_height+1, INK)
    for index in range(5):
        frac = index / 4
        px, py = left + round(frac * (map_width-1)), top + round(frac * (map_height-1))
        _line(image, [(px, top + map_height), (px, top + map_height + 5)], INK)
        _line(image, [(left-6, py), (left, py)], INK)
        _text(image, _number(bounds[0] + frac * (bounds[2]-bounds[0])), max(0, px-18), top+map_height+8, size=10)
        _text(image, _number(bounds[3] - frac * (bounds[3]-bounds[1])), 3, py-5, size=10)
    _scale_bar(image, bounds, scale, layers[0][2].crs, left + 5, height - 38 if focus else height - 48,
               min(130, map_width//3))
    return {'path': _save(image, output_png, dpi), 'width_px': width, 'height_px': height, 'dpi': dpi,
            'bounds': list(bounds), 'layer_order': [str(layer[1]) for layer in layers],
            'annotation_count': total_texts, 'markers': placements}


def render_overview(files, output_png, *, width_px=1200, height_px=900, dpi=150, drc_report=None, title=None):
    return _render(_read_layers(files), output_png, width_px, height_px, dpi, drc_report, title)


def render_drc_crops(files, drc_report, output_dir, *, dpi=300, max_crops=8):
    if type(max_crops) is not int or not 0 <= max_crops <= 100:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: max_crops must be between 0 and 100')
    layers = _read_layers(files)
    bounds = np.asarray([g.bounds for _, _, reader, _, _ in layers for g in reader.geom if not g.is_empty])
    span = max(np.max(bounds[:, 2:], axis=0) - np.min(bounds[:, :2], axis=0))
    extent = float(span) * .055 if span > 0 else 1.
    crops = []
    coordinates = (item for item in drc_report.get('violations', [])
                   if item.get('coords') and all(math.isfinite(v) for v in item['coords']))
    from itertools import islice
    for i, item in enumerate(islice(coordinates, max_crops), 1):
        x, y = item['coords']
        file = Path(output_dir) / f'drc-{i:02}.png'
        crops.append(_render(layers, file, 900, 650, dpi, None, 'MapGIS · DRC 局部检查',
                             view=(x-extent, y-extent, x+extent, y+extent), focus=item))
    return crops


def render_diff_heatmap(before_png, after_png, output_png):
    before, after = map(_read_png, (before_png, after_png))
    if before.shape != after.shape:
        raise BridgeError('PREVIEW_DIFF_SIZE: source PNGs must have the same pixel dimensions')
    path = Path(output_png).resolve()
    absolute_path = path.with_name(path.stem + '-absolute.png')
    if {path, absolute_path}.intersection(Path(p).resolve() for p in (before_png, after_png)):
        raise BridgeError('PREVIEW_DIFF_OUTPUT: use distinct output PNG paths')
    delta = np.abs(after.astype(np.int16) - before.astype(np.int16)).astype(np.uint8)
    intensity = delta.max(axis=2)
    ymin, xmin = np.nonzero(intensity)
    heat = np.stack([np.full(intensity.shape, 255, dtype=np.uint8),
                     234 - (intensity.astype(np.uint16) * 210 // 255),
                     106 - (intensity.astype(np.uint16) * 76 // 255)], axis=2)
    alpha = np.where(intensity > 0, 66 + intensity.astype(np.uint16) * 144 // 255, 0)[..., None]
    overlay = ((after.astype(np.uint32) * (255-alpha) + heat.astype(np.uint32) * alpha + 127) // 255).astype(np.uint8)
    return {'path': _save(overlay, path, 150), 'absolute_difference_png': _save(delta, absolute_path, 150),
            'changed_pixel_ratio': float(np.count_nonzero(intensity) / intensity.size),
            'mean_abs_diff': float(delta.mean()), 'max_diff': int(delta.max()),
            'diff_bbox': [int(xmin.min()), int(ymin.min()), int(xmin.max()) + 1, int(ymin.max()) + 1] if len(xmin) else None}
