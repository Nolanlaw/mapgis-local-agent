"""Geodetic, topology, map/legend and spatial accounting checks for saved layers."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cache
import json
import math
from pathlib import Path
import re
import struct

from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import polygonize
from shapely.validation import explain_validity

from .core import BridgeError, native_ids, read_native, scalar, sha
from .topology import _wp_arc_records, _wp_nodes, validate_wp_topology


RULES_FILE = Path(__file__).with_name('data') / 'standards' / 'cartography-rules-v1.json'
CODE_FIELDS = ('CODE', 'STRATA', 'LITH')


@cache
def _stock_rules():
    return json.loads(RULES_FILE.read_text(encoding='utf-8'))


def parse_crs(value):
    if value is None or (isinstance(value, str) and value.upper() == 'LOCAL'):
        return None
    try:
        return CRS.from_user_input(value)
    except (CRSError, ValueError, TypeError) as exc:
        raise BridgeError('AUDIT_CRS_MISMATCH: invalid coordinate reference system') from exc


def false_easting(crs):
    if not crs or crs.coordinate_operation is None:
        return None
    return next((float(p.value) for p in crs.coordinate_operation.params if p.code == '8806'), None)


def check_frames(left, right):
    left, right = left or None, right or None
    if left is None and right is None:
        return
    if left is None or right is None or left.is_projected != right.is_projected:
        raise BridgeError('AUDIT_UNPROJECTED_MIX')
    if left.equals(right, ignore_axis_order=True):
        return
    a, b = false_easting(left), false_easting(right)
    if a is not None and b is not None and math.isclose(abs(a - b), 40_000_000, rel_tol=0, abs_tol=.001):
        raise BridgeError('AUDIT_FALSE_EASTING')
    raise BridgeError('AUDIT_CRS_MISMATCH')


def normalize_unit(unit):
    value = unit.strip().lower()
    return {'meter': 'm', 'meters': 'm', 'metre': 'm', 'metres': 'm', 'degrees': 'degree',
            'deg': 'degree', 'feet': 'ft', 'foot': 'ft', 'millimeter': 'mm',
            'millimetre': 'mm', 'us survey foot': 'us-ft'}.get(value, value)


def frame_unit(crs):
    return normalize_unit(crs.axis_info[0].unit_name) if crs and crs.axis_info else None


def check_frame_values(crs, bounds=None, unit=None):
    if unit is not None and crs and normalize_unit(unit) != frame_unit(crs):
        raise BridgeError('AUDIT_CRS_MISMATCH: declared unit differs from CRS axes')
    if bounds is None or not crs:
        return
    offset = false_easting(crs)
    eastings = (float(bounds[0]), float(bounds[2]))
    # Native headers can decode to a custom CRS without an authority identifier
    # while still exposing the exact projection offset.
    if offset == 40_500_000 and any(abs(x - offset) > 20_000_000 for x in eastings):
        raise BridgeError('AUDIT_FALSE_EASTING')
    if offset == 500_000 and any(abs(x - 40_500_000) < 2_000_000 for x in eastings):
        raise BridgeError('AUDIT_FALSE_EASTING')


@dataclass(frozen=True)
class Layer:
    path: Path
    digest: str
    reader: object
    ids: list[int]
    scale: float | None
    role: str | None

    @property
    def kind(self):
        return self.reader.shapeType

    @property
    def crs(self):
        return getattr(self.reader, 'effective_crs', self.reader.crs) or None

    @property
    def geometries(self):
        return self.reader.geom


def read_layer(source):
    if isinstance(source, (str, Path)):
        source = {'file': str(source)}
    elif hasattr(source, 'model_dump'):
        source = source.model_dump()
    if not isinstance(source, dict) or not isinstance(source.get('file'), str):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: an audit layer requires a file')
    path = Path(source['file']).resolve(strict=True)
    blob, reader, _ = read_native(path)
    digest = sha(blob)
    if source.get('expected_sha256') is not None and digest != source['expected_sha256']:
        raise BridgeError('STALE_SOURCE')
    scale = struct.unpack_from('<d', blob, 143)[0]
    return Layer(path, digest, reader, native_ids(reader),
                 scale if math.isfinite(scale) and scale > 0 else None, source.get('role'))


def _coords(geometry):
    if geometry is None or geometry.is_empty:
        return None
    point = geometry.representative_point()
    return [float(point.x), float(point.y)] if math.isfinite(point.x) and math.isfinite(point.y) else None


def _finding(rule, category, severity, message, fix, layer=None, ids=(), coords=None, **details):
    return {'rule_id': rule, 'category': category, 'severity': severity, 'message': message,
            'file': str(layer.path) if isinstance(layer, Layer) else (str(layer) if layer else None),
            'record_ids': list(ids), 'coords': coords, 'suggested_fix': fix, **details}


def _topology(rule, severity, message, fix, layer, ids, coords, **details):
    return _finding(rule, 'topology', severity, message, fix, layer, ids, coords, **details)


def _line_findings(layer, tolerance):
    parts = [(part, rid) for geom, rid in zip(layer.geometries, layer.ids) if not geom.is_empty
             for part in (geom.geoms if geom.geom_type == 'MultiLineString' else [geom])]
    tree = STRtree([line for line, _ in parts])
    for index, (line, rid) in enumerate(parts):
        if line.is_closed:
            continue
        for endpoint in (Point(line.coords[0]), Point(line.coords[-1])):
            peers = tree.query(endpoint, predicate='dwithin', distance=tolerance)
            if not any(int(peer) != index for peer in peers):
                yield _topology('DRC_DANGLE', 'WARNING', 'Line end has no connection within the snap tolerance.',
                                'Snap to the intended node or document an intentional end.', layer, [rid], _coords(endpoint))


def _arc_findings(layer, location):
    try:
        blob, _, _, arcs = _wp_arc_records(layer.path)
        if sha(blob) != layer.digest:
            raise BridgeError('STALE_DURING_TOPOLOGY_READ')
        topology = validate_wp_topology(layer.path)
        _wp_nodes(arcs)
    except BridgeError as exc:
        if str(exc).startswith('STALE_'):
            raise
        yield _topology('DRC_WP_ARC_TOPOLOGY', 'WARNING', str(exc),
                        'Check native arc ownership and signed walks.', layer, [], location)
        return

    parents = {}

    def root(vertex):
        parents.setdefault(vertex, vertex)
        while parents[vertex] != vertex:
            parents[vertex] = parents[parents[vertex]]
            vertex = parents[vertex]
        return vertex

    for aid, arc in arcs.items():
        start, end = arc['node_ids']
        if start == end == 0:
            start = end = -aid  # A closed arc with unassigned endpoints supplies one vertex.
        parents[root(start)] = root(end)
    components = len({root(node) for node in parents})
    faces = 1 + sum(1 for _ in polygonize([LineString(arc['coordinates']) for arc in arcs.values()]))
    euler = len(parents) - len(arcs) + faces
    if topology['orphan_arcs'] or euler != 1 + components:
        ids = [aid for aid, arc in arcs.items() if not arc['owner_region_ids']]
        yield _topology('DRC_EULER_ANOMALY', 'WARNING', 'Unowned arcs or unexpected planar Euler characteristic.',
                        'Inspect the arc graph and shared boundaries.', layer, ids, location,
                        vertices=len(parents), arcs=len(arcs), faces=faces, components=components, euler=euler)


def _polygon_findings(layer, area_limit, compactness, overlap_tolerance):
    valid = [(geom, rid) for geom, rid in zip(layer.geometries, layer.ids) if geom.is_valid and not geom.is_empty]
    tree = STRtree([geom for geom, _ in valid])
    for index, (geom, rid) in enumerate(valid):
        if geom.area < area_limit and geom.length and 4 * math.pi * geom.area / geom.length**2 < compactness:
            yield _topology('DRC_SLIVER', 'WARNING', 'Polygon meets both sliver thresholds.',
                            'Review the narrow polygon against the geologic source.', layer, [rid], _coords(geom))
        for peer in sorted(int(i) for i in tree.query(geom, predicate='intersects') if i > index):
            other, other_id = valid[peer]
            overlap = geom.intersection(other)
            if overlap.area > overlap_tolerance:
                yield _topology('DRC_POLYGON_OVERLAP', 'ERROR', 'Polygon interiors overlap.',
                                'Resolve duplicate or crossing boundaries.', layer, [rid, other_id], _coords(overlap))
    yield from _arc_findings(layer, _coords(valid[0][0]) if valid else None)


def _label_findings(points, polygons):
    valid = [(geom, rid) for geom, rid in zip(polygons.geometries, polygons.ids) if geom.is_valid and not geom.is_empty]
    tree = STRtree([geom for geom, _ in valid])
    labels = defaultdict(list)
    for point, rid in zip(points.geometries, points.ids):
        owners = sorted(int(i) for i in tree.query(point, predicate='within')) if not point.is_empty else []
        if len(owners) != 1:
            yield _topology('DRC_LABEL_AMBIGUOUS' if owners else 'DRC_LABEL_OUTSIDE', 'ERROR',
                            'Label point must be in exactly one polygon interior.',
                            'Place the label point inside its intended polygon.', points, [rid], _coords(point))
        for owner in owners:
            labels[owner].append(rid)
    for index, (geom, rid) in enumerate(valid):
        identities = labels[index]
        if len(identities) != 1:
            yield _topology('DRC_MULTIPLE_LABELS' if identities else 'DRC_UNLABELLED_POLYGON',
                            'ERROR', f'Polygon contains {len(identities)} label points.',
                            'Keep exactly one label point per polygon.', polygons, [rid], _coords(geom),
                            label_record_ids=identities, label_file=str(points.path))


def _token(value):
    return str(value).strip() or None if value is not None else None


def _rgb(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r'#[a-fA-F0-9]{6}', value):
        return tuple(bytes.fromhex(value[1:]))
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(type(v) is int and 0 <= v <= 255 for v in value):
        return tuple(value)
    raise BridgeError('INVALID_TOOL_ARGUMENTS: RGB requires three byte values or #RRGGBB')


def _first(mapping, keys):
    return next((value for key in keys if (value := _token(mapping.get(key))) is not None), None)


def _legend_sources(legend_items, slib_path, root):
    items = list(legend_items or [])
    palette, exceptions = {}, []
    membership = legend_items is not None
    if slib_path is None:
        return items, palette, exceptions, membership
    path = Path(slib_path).resolve(strict=True)
    manifest = path / 'legend_items.json' if path.is_dir() else path
    if manifest.suffix.lower() == '.json' and manifest.is_file():
        spec = json.loads(manifest.read_text(encoding='utf8'))
        if isinstance(spec, list):
            spec = {'legend_items': spec}
        if not isinstance(spec, dict):
            raise BridgeError('INVALID_TOOL_ARGUMENTS: expected a legend manifest')
        items.extend(spec.get('legend_items', []))
        membership |= 'legend_items' in spec
        palette.update({str(key): _rgb(rgb) for key, rgb in spec.get('palette', {}).items()})
        exceptions.extend(spec.get('exceptions', []))
    if path.suffix.lower() == '.cln':
        if root is None:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: CLN inspection requires a configured root')
        from .native_legend import native_legend_info
        from .object_store import file_sha256
        items.extend(native_legend_info(Path(root), str(path), file_sha256(path))['items'])
        membership = True
    elif path.is_dir() or path.name.lower() == 'pcolor.lib':
        library = path / 'pcolor.lib' if path.is_dir() else path
        if library.is_file():
            from .symbol_library import _colors
            for index, record in enumerate(_colors(library.read_bytes())):
                k, c, m, y = record[:4]
                if max(k, c, m, y) <= 100 and not any(record[4:6]):
                    palette.setdefault(str(index), tuple(round(255 * (1 - ch / 100) * (1 - k / 100))
                                                         for ch in (c, m, y)))
    elif not (manifest.is_file() and manifest.suffix.lower() == '.json'):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: Slib folder, palette, CLN or legend JSON required')
    return items, palette, exceptions, membership


def _lvs(layers, items, palette, rules, exceptions, membership):
    declared = {}
    for item in items:
        number = _first(item, ('color_no', 'color_id', 'color')) or _first(item.get('style', {}), ('fill_color', 'color'))
        code = _first(item, ('code', 'strata', 'lith'))
        key = ('code', code) if code is not None else ('color', number)
        if key[1] is None or key in declared:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: legend identities must be present and unique')
        declared[key] = number, _token(item.get('color_code')), _rgb(item.get('rgb'))

    def exempt(rule, code, number, layer):
        return any(entry.get('reason') and entry.get('rule_id', 'LVS_STANDARD_DEVIATION') == rule
                   and {'code', 'color', 'file'}.intersection(entry)
                   and (entry.get('code') is None or _token(entry['code']) == code)
                   and (entry.get('color') is None or _token(entry['color']) == number)
                   and (entry.get('file') is None or layer and str(entry['file']) in {layer.path.name, str(layer.path)})
                   for entry in exceptions)

    def standard(code, designator, rgb):
        rule = rules['codes'].get(code)
        if code is not None and rule is None:
            return 'Code is outside the bundled reference set.'
        if rule is None:
            return None
        if designator is not None and rule.get('color_codes') and designator not in rule['color_codes']:
            return 'Color designator differs from the reference set.'
        if rgb is not None and rule.get('rgb') and not any(
            all(lo <= channel <= hi for channel, lo, hi in zip(rgb, low, high))
            for low, high in rule['rgb']
        ):
            return 'RGB differs from the reference set.'

    def deviation(message, code, number, layer=None, rid=None, xy=None):
        if message and not exempt('LVS_STANDARD_DEVIATION', code, number, layer):
            return _finding('LVS_STANDARD_DEVIATION', 'lvs', 'WARNING', message,
                            'Check project palette and standard, or document a scoped exception.',
                            layer, [rid] if rid is not None else [], xy, code=code, color_no=number)

    observed = set()
    for layer in layers:
        reader = layer.reader
        style = {'POINT': reader.point_params, 'LINE': reader.line_params, 'POLYGON': reader.polygon_params}[layer.kind]
        for index, (rid, geom) in enumerate(zip(layer.ids, layer.geometries)):
            attrs = {str(k).upper(): scalar(v) for k, v in reader.data.iloc[index].items()}
            code = _first(attrs, CODE_FIELDS)
            number = _first(attrs, ('COLOR_NO', 'COLOR_ID', 'COLOR'))
            if number is None and not style.empty:
                number = _first(style.iloc[index].to_dict(), ('fill_color', 'color'))
            if code is None and number is None:
                continue
            key = ('code', code) if code is not None else ('color', number)
            if key not in declared and ('color', number) in declared:
                key = ('color', number)
            observed.add(key)
            xy = _coords(geom)
            if membership and key not in declared and not exempt('LVS_MISSING_LEGEND', code, number, layer):
                yield _finding('LVS_MISSING_LEGEND', 'lvs', 'ERROR', 'Mapped unit is missing from the legend.',
                               'Add the unit to the project legend.', layer, [rid], xy, code=code, color_no=number)
            declared_number, _, legend_rgb = declared.get(key, (None, None, None))
            rgb = _rgb(attrs.get('RGB')) or palette.get(number)
            message = standard(code, _token(attrs.get('COLOR_CODE')), rgb)
            if declared_number is not None and number is not None and declared_number != number:
                message = 'Map and legend color indices disagree.'
            if rgb is not None and legend_rgb is not None and rgb != legend_rgb:
                message = 'Map and legend RGB values disagree.'
            if finding := deviation(message, code, number, layer, rid, xy):
                yield finding
    for (kind, identity), (number, designator, rgb) in declared.items():
        code = identity if kind == 'code' else None
        if (kind, identity) not in observed and not exempt('LVS_UNUSED_LEGEND', code, number, None):
            yield _finding('LVS_UNUSED_LEGEND', 'lvs', 'WARNING', 'Declared unit is absent from the map.',
                           'Verify layer selection or remove the unused legend entry.', code=code, color_no=number)
        if finding := deviation(standard(code, designator, rgb or palette.get(number)), code, number):
            yield finding


def _ledger(layers):
    area = records = filled = cells = 0
    for layer in layers:
        if layer.kind == 'POLYGON':
            area += sum(g.area for g in layer.geometries if not g.is_empty)
        records += len(layer.ids)
        data = layer.reader.data
        cells += data.size
        filled += sum(_token(scalar(v)) is not None for row in data.itertuples(index=False, name=None) for v in row)
    return {'total_area': area, 'record_count': records, 'nonempty_attributes': filled,
            'attribute_cells': cells, 'attribute_coverage': filled / cells if cells else 1.0}


def _account(before, after, area_tol, record_tol, coverage_tol):
    baseline, current = _ledger(before), _ledger(after)
    area = current['total_area'] - baseline['total_area']
    rate = area / baseline['total_area'] if baseline['total_area'] else (None if area else 0.0)
    records = current['record_count'] - baseline['record_count']
    coverage = current['attribute_coverage'] - baseline['attribute_coverage']
    result = {'before': baseline, 'after': current, 'area_delta': area, 'area_change_rate': rate,
              'record_delta': records, 'attribute_coverage_delta': coverage}
    findings = []
    if rate is None or abs(rate) > area_tol:
        findings.append(_finding('ACCOUNTING_AREA_DRIFT', 'accounting', 'ERROR', 'Mapped area changed beyond tolerance.',
                                 'Reconcile area, shared boundaries and intentional edits.'))
    if abs(records) > record_tol or coverage < -coverage_tol:
        findings.append(_finding('ACCOUNTING_RECORD_LOSS', 'accounting', 'WARNING',
                                 'Feature-count change or coverage loss exceeds tolerance.',
                                 'Reconcile physical records and missing attribute values.'))
    return result, findings


def run_drc_audit(files, reference_crs=None, *, label_files=None, before_files=None,
                  legend_items=None, slib_path=None, exceptions=None, rules_path=None,
                  dangle_tolerance=0.0, sliver_area_threshold=1.0, sliver_compactness=.1,
                  overlap_tolerance=0.0, area_tolerance=.001, record_tolerance=0,
                  attribute_tolerance=.01, unit=None, scale=None, root=None,
                  enforce_frames=False):
    """Audit ordered saved layers. Geometry, locations and tolerances use source map units."""
    if not files:
        raise BridgeError('INVALID_TOOL_ARGUMENTS: at least one map layer is required')
    for value in (dangle_tolerance, sliver_area_threshold, sliver_compactness, overlap_tolerance,
                  area_tolerance, record_tolerance, attribute_tolerance):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: finite nonnegative DRC tolerances required')
    if scale is not None and (type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: positive scale denominator required')
    layers = [read_layer(file) for file in files]
    extras = [read_layer(file) for file in label_files or []]
    points = [*extras, *(layer for layer in layers if layer.role in ('label', 'labels', 'label_points'))]
    if any(layer.kind != 'POINT' for layer in points):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: label points must be WT layers')
    before = [read_layer(file) for file in before_files or []]
    rules = _stock_rules() if rules_path is None else json.loads(Path(rules_path).read_text(encoding='utf8'))
    ref = parse_crs(reference_crs) if reference_crs is not None else layers[0].crs
    expected_scale = scale if scale is not None else next((layer.scale for layer in layers if layer.scale), None)
    findings, checked = [], []
    for layer in [*layers, *extras, *before]:
        try:
            check_frames(ref, layer.crs)
            check_frame_values(layer.crs, layer.reader.bbox if layer.geometries else None, unit)
            if expected_scale is not None and layer.scale is not None and not math.isclose(expected_scale, layer.scale):
                raise BridgeError('AUDIT_CRS_MISMATCH: stored scale denominators differ')
        except BridgeError as exc:
            if enforce_frames:
                raise
            findings.append(_finding(str(exc).split(':')[0], 'geodesy', 'ERROR', str(exc),
                                     'Inspect the CRS, datum, stored scale and false eastings.', layer,
                                     coords=_coords(next(iter(layer.geometries), None))))

    for layer in [*layers, *extras]:
        reader, frame = layer.reader, layer.crs
        for geom, rid in zip(layer.geometries, layer.ids):
            if geom.is_empty or not geom.is_valid:
                explanation = explain_validity(geom)
                match = re.search(r'\[([-\d.eE+]+) ([-\d.eE+]+)\]', explanation)
                xy = [float(v) for v in match.groups()] if match else _coords(geom)
                rule = 'DRC_SELF_INTERSECTION' if 'self-intersection' in explanation.lower() else 'DRC_INVALID_GEOMETRY'
                findings.append(_topology(rule, 'ERROR', 'Active geometry is empty.' if geom.is_empty else explanation,
                                          'Repair geometry and recheck topology.', layer, [rid], xy))
            elif layer.kind == 'LINE' and not geom.is_simple:
                findings.append(_topology('DRC_SELF_INTERSECTION', 'ERROR', 'Line intersects itself.',
                                          'Split or repair the line at the crossing.', layer, [rid], _coords(geom)))
        if layer.kind == 'LINE':
            findings.extend(_line_findings(layer, dangle_tolerance))
        elif layer.kind == 'POLYGON':
            findings.extend(_polygon_findings(layer, sliver_area_threshold, sliver_compactness, overlap_tolerance))
        checked.append({'file': str(layer.path), 'sha256': layer.digest, 'type': layer.kind,
                        'topology': {'features': len(reader.geom), 'invalid': sum(not g.is_valid for g in reader.geom),
                                     'empty': sum(g.is_empty for g in reader.geom)},
                        'attributes': {'rows': len(reader.data), 'fields': len(reader.fields),
                                       'aligned': len(reader.data) == len(layer.ids) == len(reader.geom),
                                       'physical_ids_unique': len(set(layer.ids)) == len(layer.ids)},
                        'crs': {'epsg': frame.to_epsg() if frame is not None else None,
                                'projected': frame.is_projected if frame is not None else None,
                                'false_easting': false_easting(frame), 'unit': frame_unit(frame),
                                'datum': frame.datum.name if frame is not None and frame.datum else None,
                                'scale': layer.scale}})
    same_frame = not any(item['category'] == 'geodesy' for item in findings)
    polygons = [layer for layer in layers if layer.kind == 'POLYGON']
    if points:
        if len(points) != len(polygons):
            raise BridgeError('INVALID_TOOL_ARGUMENTS: bind one ordered label WT per WP')
        if same_frame:
            for point, polygon in zip(points, polygons):
                findings.extend(_label_findings(point, polygon))

    if legend_items is not None or slib_path is not None:
        items, palette, project_exceptions, membership = _legend_sources(legend_items, slib_path, root)
        if not membership:
            findings.append(_finding('LVS_LEGEND_MEMBERSHIP_UNAVAILABLE', 'lvs', 'INFO',
                                     'Palette slots do not identify the entries of a project legend.',
                                     'Bind explicit legend items, a CLN or a legend_items.json manifest.', slib_path))
        findings.extend(_lvs(layers, items, palette, rules,
                             [*rules.get('exceptions', []), *project_exceptions, *(exceptions or [])], membership))
    accounting = None
    if before_files is not None and same_frame:
        accounting, drift = _account(before, layers, area_tolerance, record_tolerance, attribute_tolerance)
        findings.extend(drift)
    errors = sum(item['severity'] == 'ERROR' for item in findings)
    warnings = sum(item['severity'] == 'WARNING' for item in findings)
    return {'files': checked, 'checks': 'saved geometry validity, attribute alignment and coordinate compatibility',
            'violations': findings, 'summary': {'passed': errors == 0, 'error_count': errors,
            'warning_count': warnings, 'accounting': accounting, 'rules_version': rules['version']}}
