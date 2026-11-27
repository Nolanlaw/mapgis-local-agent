"""Hash-bound DBS sheet indexes, seam reports and editable native window exports."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
import json
import math
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory

from pyproj import Transformer
from pymapgis import reader as native
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, mapping
from shapely.strtree import STRtree

from .attributes import _attribute_table
from .comparison import _diff_dicts, _extract_attributes, _extract_style
from .core import Bridge, BridgeError, annotation_text, json_text, native_ids, read_native, sha
from .creation import PARAMS, TYPES, _replace_sections, prepare_creation
from .crs_contract import make_crs_sidecar, read_crs_sidecar, sidecar_path
from .drc import false_easting
from .map_sheets import map_sheets
from .topology import validate_wp_topology


_KINDS = {suffix[1:]: kind for suffix, kind in TYPES.items()}
_EPS = 1e-7  # Below one micrometre in the required projected metre frame.


@dataclass
class Layer:
    path: Path
    blob: bytes
    reader: object
    sections: list
    table: dict
    schema: list
    crs: object
    sidecar: dict | None
    sidecar_hash: str | None
    ids: list

    def binding(self):
        return {'file': str(self.path), 'expected_sha256': sha(self.blob),
                'sidecar_sha256': self.sidecar_hash}

    def row(self, index):
        start = self.table['data']+self.ids[index]*self.table['width']
        return self.blob[start:start+self.table['width']]

    def attrs(self, index):
        values = _extract_attributes(self.reader, index)
        return {field['name']: values[field['name']] for field in self.schema}


@dataclass
class Sheet:
    code: str
    scale: int
    geographic: list
    bounds: list
    layers: dict

    def binding(self):
        return {'sheet_id': self.code, 'layers': {kind: layer.binding() for kind, layer in self.layers.items()}}


def _read_layer(kind, binding):
    if not isinstance(binding, dict) or not {'file', 'expected_sha256'} <= set(binding) <= {
            'file', 'expected_sha256', 'sidecar_sha256'}:
        raise BridgeError('DBS_SCHEMA: bind a file and SHA-256 for each WT/WL/WP layer')
    path = Path(binding['file']).resolve(strict=True)
    if path.suffix.casefold() != '.'+kind:
        raise BridgeError('DBS_SCHEMA: layer suffix differs from its declared type')
    blob, reader, sections = read_native(path)
    if sha(blob) != binding['expected_sha256']:
        raise BridgeError('STALE_SOURCE')
    side_path = sidecar_path(path)
    companion = side_path.read_bytes() if side_path.is_file() else None
    side_hash = sha(companion) if companion is not None else None
    if 'sidecar_sha256' in binding and side_hash != binding['sidecar_sha256']:
        raise BridgeError('CRS_SIDECAR_STALE')
    declared = read_crs_sidecar(path, file_bytes=blob)
    if bool(declared) != (companion is not None) or (declared and side_path.read_bytes() != companion):
        raise BridgeError('CRS_SIDECAR_STALE')
    document, crs = declared if declared else (None, getattr(reader, 'crs', None))
    if (not crs or not crs.is_projected or len(crs.axis_info) != 2
            or any(not math.isclose(axis.unit_conversion_factor, 1, rel_tol=1e-12) for axis in crs.axis_info)):
        raise BridgeError('DBS_CRS: a projected metre CRS is required')
    if reader.shapeType != _KINDS[kind]:
        raise BridgeError('DBS_SCHEMA: layer type conflicts with the native signature')
    index = native._ATTR_HEADER_INDEX[reader.shapeType]
    table = _attribute_table(blob, sections, index)
    if table is None:
        raise BridgeError('DBS_SCHEMA: missing native field dictionary')
    schema = []
    for field in table['fields']:
        raw = table['descriptors'][field.name]
        try:
            encoded = field.name.encode('gbk', errors='strict')
        except UnicodeError as exc:
            raise BridgeError('DBS_FIELD_GBK') from exc
        if not 0 < len(encoded) <= 19 or raw[:20].split(b'\0', 1)[0] != encoded:
            raise BridgeError('DBS_FIELD_GBK: field name must fit 19 GBK bytes')
        schema.append({'name': field.name, 'type': field.type_code, 'length': field.length,
                       'decimals': field.decimals, 'offset': field.offset,
                       'storage_bytes': struct.unpack_from('<h', raw, 25)[0]})
    if any(g.is_empty or not g.is_valid or not all(math.isfinite(v) for v in g.bounds) for g in reader.geom):
        raise BridgeError('INVALID_GEOMETRY: DBS sheets require finite valid 2D features')
    return Layer(path, blob, reader, sections, table, schema, crs, document, side_hash, native_ids(reader))


def _shared_boundary(left, right):
    a, b = left.geographic, right.geographic
    for axis in (0, 1):
        other = 1-axis
        low, high = max(a[other], b[other]), min(a[other+2], b[other+2])
        if high <= low + 1e-11:
            continue
        for west, east in ((a, b), (b, a)):
            if math.isclose(west[axis+2], east[axis], abs_tol=1e-11, rel_tol=0):
                return axis, west[axis+2], low, high
    return None


def _load(sheets):
    if not isinstance(sheets, list) or len(sheets) < 2:
        raise BridgeError('DBS_SCHEMA: supply at least two standard sheets')
    loaded = []
    reference = None
    for spec in sheets:
        if (not isinstance(spec, dict) or set(spec) != {'sheet_id', 'layers'}
                or not isinstance(spec['layers'], dict) or set(spec['layers']) != set(_KINDS)):
            raise BridgeError('DBS_SCHEMA: a sheet has sheet_id and layers wt, wl and wp')
        layers = {kind: _read_layer(kind, spec['layers'][kind]) for kind in _KINDS}
        reference = reference or layers
        frame = reference['wt']
        for kind, layer in layers.items():
            if false_easting(layer.crs) != false_easting(frame.crs):
                raise BridgeError('DBS_FALSE_EASTING: offset differs across layers or sheets')
            if not layer.crs.equals(frame.crs, ignore_axis_order=True) or bool(layer.sidecar) != bool(frame.sidecar):
                raise BridgeError('DBS_CRS: CRS or native/sidecar representation differs')
            if layer.schema != reference[kind].schema:
                raise BridgeError(f'DBS_SCHEMA: ordered native field dictionary differs for {kind}')
        geographic_crs = frame.crs.geodetic_crs
        decoded = map_sheets('decode', {'codes': [spec['sheet_id']]},
                              {'definition': geographic_crs.to_wkt(), 'axis_order': 'xy'})['sheets'][0]
        crs_transform = Transformer.from_crs(geographic_crs, frame.crs, always_xy=True)
        extent = list(crs_transform.transform_bounds(*decoded['geographic_bounds'], densify_pts=64, errcheck=True))
        loaded.append(Sheet(decoded['code'], decoded['map_scale'], decoded['geographic_bounds'], extent, layers))
    if len({sheet.code for sheet in loaded}) != len(loaded) or len({sheet.scale for sheet in loaded}) != 1:
        raise BridgeError('DBS_ADJACENCY: sheet IDs must be distinct and share one standard scale')
    pairs = [(left, right, boundary) for left, right in combinations(loaded, 2)
             if (boundary := _shared_boundary(left, right)) is not None]
    neighbours = defaultdict(set)
    for left, right, _ in pairs:
        neighbours[left.code].add(right.code)
        neighbours[right.code].add(left.code)
    remaining = {sheet.code for sheet in loaded[1:]}
    todo = [loaded[0].code]
    while todo:
        linked = remaining & neighbours[todo.pop()]
        remaining -= linked
        todo.extend(linked)
    if remaining:
        raise BridgeError('DBS_ADJACENCY: the standard neatlines must form a connected set of shared edges')
    return loaded, pairs


def _describe(loaded, pairs):
    frame = loaded[0].layers['wt']
    return {'status': 'VALID', 'scale': loaded[0].scale, 'axis_order': 'xy',
            'crs': {'wkt': frame.crs.to_wkt(), 'epsg': frame.crs.to_epsg(),
                    'false_easting_m': false_easting(frame.crs)},
            'schemas': {kind: layer.schema for kind, layer in loaded[0].layers.items()},
            'sheets': [{**sheet.binding(), 'bbox': sheet.bounds, 'geographic_bbox': sheet.geographic,
                        'crs_contracts': {kind: layer.sidecar for kind, layer in sheet.layers.items()},
                        'source_feature_bounds': {kind: _bounds(layer.reader.geom)
                                                  for kind, layer in sheet.layers.items()}} for sheet in loaded],
            'adjacency': [{'sheets': [left.code, right.code], 'axis': 'xy'[axis],
                           'geographic_ordinate': ordinate, 'geographic_range': [low, high]}
                          for left, right, (axis, ordinate, low, high) in pairs]}


def _bounds(geometries):
    bounds = [geometry.bounds for geometry in geometries]
    return ([min(row[0] for row in bounds), min(row[1] for row in bounds),
             max(row[2] for row in bounds), max(row[3] for row in bounds)] if bounds else None)


def validate_multisheet_schema(sheets):
    """Return ordered native field dictionaries, CRS evidence and standard adjacency."""
    return _describe(*_load(sheets))


def _seam(crs, boundary, requested, tolerance):
    axis, ordinate, low, high = boundary
    transform = Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)

    def project(value):
        xy = (ordinate, value) if axis == 0 else (value, ordinate)
        return transform.transform(*xy, errcheck=True)

    def densify(a, b, first, last, depth=0):
        mid = (a+b)/2
        point = project(mid)
        if depth == 16 or Point(point).distance(LineString([first, last])) <= max(_EPS, tolerance/64):
            return [last]
        return densify(a, mid, first, point, depth+1)+densify(mid, b, point, last, depth+1)

    first, last = project(low), project(high)
    coords = [first, *densify(low, high, first, last)]
    if requested is not None and requested != 'xy'[axis]:
        if (not isinstance(requested, dict) or set(requested) != {'xy'[axis]}
                or type(requested['xy'[axis]]) not in (int, float) or not math.isfinite(requested['xy'[axis]])):
            raise BridgeError('DBS_BOUNDARY: the selector must identify the shared x or y standard edge')
        value = requested['xy'[axis]]
        if any(abs(point[axis]-value) > tolerance+_EPS for point in coords):
            raise BridgeError('DBS_BOUNDARY: the supplied projected ordinate differs from the standard edge')
        coords = [(value, p[1]) if axis == 0 else (p[0], value) for p in coords]
    return LineString(coords)


def _paths(geometry):
    if geometry.geom_type == 'LineString':
        return {(0, 0): list(geometry.coords)}
    parts = geometry.geoms if geometry.geom_type == 'MultiPolygon' else (geometry,)
    return {(part, ring): list(shape.coords) for part, polygon in enumerate(parts)
            for ring, shape in enumerate((polygon.exterior, *polygon.interiors))}


@dataclass
class Contact:
    row: int
    path: tuple
    start: int
    stop: int
    coords: list
    along: tuple
    reverse: bool

    @property
    def geometry(self):
        return Point(self.coords[0]) if len(self.coords) == 1 else LineString(self.coords)

    def record(self, layer):
        return {'record_id': layer.ids[self.row], 'part': self.path[0], 'ring': self.path[1],
                'coordinates': [list(point) for point in self.coords], 'along_boundary_m': list(self.along)}


def _contacts(layer, seam, tolerance):
    found, paths = [], {}
    areal = layer.reader.shapeType == 'POLYGON'
    limit = tolerance+_EPS
    for row, geometry in enumerate(layer.reader.geom):
        if geometry.distance(seam) > limit:
            continue
        paths[row] = _paths(geometry)
        for key, coords in paths[row].items():
            near = [Point(point).distance(seam) <= limit for point in coords]
            if areal and any(near) and not all(near):
                start = next(i for i, match in enumerate(near[:-1]) if not match)
                coords = coords[start:-1]+coords[:start+1]
                near = near[start:-1]+near[:start+1]
                paths[row][key] = coords
            i = 0
            while i < len(coords):
                if not near[i]:
                    i += 1
                    continue
                j = i+1
                while j < len(coords) and near[j]:
                    j += 1
                if j-i > 1 or (not areal and (i == 0 or j == len(coords))):
                    points = coords[i:j]
                    first, last = (seam.project(Point(points[index])) for index in (0, -1))
                    if first != last or len(points) == 1:
                        reverse = first > last
                        found.append(Contact(row, key, i, j, points[::-1] if reverse else points,
                                             (min(first, last), max(first, last)), reverse))
                i = j
    return found, paths


def _candidates(left, right, tolerance):
    limit = tolerance+_EPS
    # Index contacts projected onto the shared boundary, including endpoint-only WLs.
    tree = STRtree([LineString([(r.along[0], 0), (r.along[1], 0)]) if len(r.coords) > 1
                   else Point(r.along[0], 0) for r in right])
    for a in left:
        lo, hi = a.along
        hits = []
        for index in tree.query(box(lo-limit, -limit, hi+limit, limit)):
            b = right[index]
            if (len(a.coords) == 1) != (len(b.coords) == 1):
                continue
            c, d = b.along
            if (max(abs(lo-c), abs(hi-d)) <= limit or min(hi, d) > max(lo, c)+_EPS):
                hits.append(int(index))
        yield sorted(hits)


def _midpoints(a, b):
    if len(a.coords) == 1:
        return [tuple((x+y)/2 for x, y in zip(a.coords[0], b.coords[0]))]
    lines = (a.geometry, b.geometry)
    fractions = {0., 1.}
    for line in lines:
        fractions.update(line.project(Point(p), normalized=True) for p in line.coords[1:-1])
    return [tuple((x+y)/2 for x, y in zip(*(line.interpolate(t, normalized=True).coords[0] for line in lines)))
            for t in sorted(fractions)]


def _measure(left, right, boundary, selected, tolerance, key_fields, snap):
    seam = _seam(left.layers['wt'].crs, boundary, selected, tolerance)
    findings, matches, edits = [], [], {}
    for kind in ('wl', 'wp'):
        a, b = left.layers[kind], right.layers[kind]
        keys = key_fields.get(kind, ()) if isinstance(key_fields, dict) else key_fields
        if (not isinstance(keys, (list, tuple)) or any(not isinstance(key, str) for key in keys)
                or set(keys)-{field['name'] for field in a.schema}):
            raise BridgeError(f'DBS_SCHEMA: unknown key field for {kind}')
        first, paths_a = _contacts(a, seam, tolerance)
        second, paths_b = _contacts(b, seam, tolerance)
        choices = list(_candidates(first, second, tolerance))
        reverse = defaultdict(list)
        for src, options in enumerate(choices):
            for dst in options:
                reverse[dst].append(src)
        changes_a, changes_b = defaultdict(list), defaultdict(list)
        paired = set()
        for i, options in enumerate(choices):
            p = first[i]
            if len(options) != 1 or len(reverse[options[0]]) != 1:
                findings.append({'code': 'EDGE_UNMATCHED', 'layer': kind, 'sheet_id': left.code,
                                 'edge': p.record(a), 'reason': 'ambiguous' if options else 'no_candidate'})
                continue
            j = options[0]
            paired.add(j)
            q = second[j]
            gap = max(p.geometry.hausdorff_distance(q.geometry), math.dist(p.coords[0], q.coords[0]),
                      math.dist(p.coords[-1], q.coords[-1]))
            difference = _diff_dicts(a.attrs(p.row), b.attrs(q.row))
            pair = {'layer': kind, 'left': p.record(a), 'right': q.record(b),
                    'gap_m': gap, 'attribute_conflicts': difference,
                    'key_conflicts': [key for key in keys if key in difference]}
            matches.append(pair)
            if gap > _EPS:
                findings.append({'code': 'EDGE_GAP', **pair})
            if difference:
                findings.append({'code': 'EDGE_ATTR_CONFLICT', **pair})
            if snap and not difference and _EPS < gap <= tolerance+_EPS:
                midpoint = _midpoints(p, q)
                for edge, target in ((p, changes_a), (q, changes_b)):
                    target[edge.row, edge.path].append((edge.start, edge.stop, midpoint[::-1] if edge.reverse else midpoint))
                pair['snapped'] = True
        for j, q in enumerate(second):
            if j not in paired:
                findings.append({'code': 'EDGE_UNMATCHED', 'layer': kind, 'sheet_id': right.code,
                                 'edge': q.record(b), 'reason': 'ambiguous' if reverse[j] else 'no_candidate'})
        for sheet, layer, paths, changes in ((left, a, paths_a, changes_a), (right, b, paths_b, changes_b)):
            if changes:
                edits[sheet.code, kind] = _apply_contacts(layer, paths, changes)
    return {'sheets': [left.code, right.code], 'boundary_axis': 'xy'[boundary[0]],
            'boundary_bounds': list(seam.bounds), 'tolerance_m': tolerance,
            'matches': matches, 'findings': findings}, edits


def _apply_contacts(layer, paths, changes):
    updated = {}
    for (row, key), replacements in changes.items():
        path = paths[row][key]
        for start, stop, midpoint in sorted(replacements, reverse=True):
            path[start:stop] = midpoint
    for row in {row for row, _ in changes}:
        path = paths[row]
        if layer.reader.shapeType == 'LINE':
            geometry = LineString(path[0, 0])
            valid = geometry.is_simple
        else:
            polygons = [Polygon(path[part, 0], [ring for (part_id, ring_id), ring in path.items()
                        if part_id == part and ring_id > 0]) for part in sorted({key[0] for key in path})]
            geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
            valid = geometry.area > 0
        if not valid or geometry.is_empty or not geometry.is_valid:
            raise BridgeError('DBS_TOPOLOGY: the proposed midpoint geometry is invalid')
        updated[row] = geometry
    return updated


def _tolerance(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BridgeError('DBS_TOLERANCE: finite nonnegative metres required')
    return value


def _owned(root, path):
    target = Path(path)
    target = (root/target).resolve() if not target.is_absolute() else target.resolve()
    if not any(target != root/folder and target.is_relative_to(root/folder) for folder in ('work', 'deliverables')):
        raise BridgeError('Writes require a project-owned destination')
    return target


def _require_fresh(loaded):
    for sheet in loaded:
        for layer in sheet.layers.values():
            side = sidecar_path(layer.path)
            if (layer.path.read_bytes() != layer.blob
                    or (sha(side.read_bytes()) if side.is_file() else None) != layer.sidecar_hash):
                raise BridgeError('STALE_SOURCE: sheet or its CRS sidecar changed')


def _write_layer(bridge, target, kind, selected, scale, request):
    first = selected[0][1]
    shape_type = _KINDS[kind]
    features, lineage = [], []
    for index, (sheet, layer, row, geometry) in enumerate(selected, 1):
        style = {key: value for key, value in _extract_style(layer.reader, row).items()
                 if key in PARAMS[shape_type] and value is not None}
        feature = {'id': str(index), 'type': 'Feature', 'geometry': mapping(geometry), 'style': style}
        if shape_type == 'POINT':
            text = annotation_text(layer.blob, layer.sections, layer.ids[row])
            if text is not None:
                feature['text'] = text
        if shape_type == 'POLYGON':
            for auxiliary in (2, 5):
                table = _attribute_table(layer.blob, layer.sections, auxiliary)
                if table is not None and table['fields']:
                    raise BridgeError('DBS_TOPOLOGY: an arc/node attribute dictionary requires explicit remapping')
        features.append(feature)
        lineage.append({'sheet_id': sheet, 'source_record_id': layer.ids[row], 'output_record_id': index})
    crs = {'definition': first.crs.to_wkt(), 'axis_order': 'xy'}
    if first.sidecar:
        crs['crs_delivery_mode'] = 'dual_sidecar'
    else:
        scale = struct.unpack_from('<d', first.blob, 143)[0]
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': features},
                     {'name': target.name, 'crs': crs, 'map_scale': scale}, request)
    vector = Path(bridge.apply(request)['output'])
    raw, _, entries = read_native(vector)
    attr = native._ATTR_HEADER_INDEX[shape_type]
    start = first.sections[attr][0]
    origin, width = first.table['data'], first.table['width']
    header = bytearray(first.blob[start:origin])
    struct.pack_into('<i', header, 12, origin-start)
    struct.pack_into('<i', header, 324, len(selected)+1)
    records = first.blob[origin:origin+width]+b''.join(layer.row(row) for _, layer, row, _ in selected)
    raw = _replace_sections(raw, entries, {attr: bytes(header)+records})
    target.write_bytes(raw)
    if first.sidecar:
        sidecar_path(target).write_text(json_text(make_crs_sidecar(target.name, raw, first.crs)), encoding='utf-8')
    _, reader, updated_sections = read_native(target)
    restored = _attribute_table(raw, updated_sections, attr)
    if (len(reader.geom) != len(selected) or restored['descriptors'] != first.table['descriptors']
            or raw[restored['data']:restored['data']+len(records)] != records):
        raise BridgeError('DBS_READBACK: native geometry or attribute records differ')
    if shape_type == 'POLYGON':
        validate_wp_topology(target)
    return lineage


def _publish(output, loaded, payload, render):
    marker = 'dbs-receipt.json'
    signature = sha(json_text(payload).encode('utf-8'))

    def read_existing():
        ledger = output/marker
        if not ledger.is_file() or ledger.is_symlink():
            raise BridgeError('OUTPUT_CONFLICT: output directory is already occupied')
        receipt = json.loads(ledger.read_text(encoding='utf-8'))
        if receipt.get('request_sha256') != signature or any(
                not (p := (output/entry['file']).resolve()).is_relative_to(output)
                or not p.is_file() or sha(p.read_bytes()) != entry['sha256'] for entry in receipt['files']):
            raise BridgeError('OUTPUT_CONFLICT: prior DBS output differs')
        return {**receipt, 'output_dir': str(output), 'reconciled': True}

    _require_fresh(loaded)
    if output.exists():
        return read_existing()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='dbs-', dir=output.parent) as temp:
        workspace = Path(temp)
        stages = workspace/'publish'
        stages.mkdir()
        report = render(Bridge(workspace/'encode'), stages)
        files = [{'file': path.relative_to(stages).as_posix(), 'sha256': sha(path.read_bytes())}
                 for path in sorted(stages.rglob('*')) if path.is_file()]
        receipt = {'request_sha256': signature, 'files': files, **report}
        (stages/marker).write_text(json_text(receipt), encoding='utf-8')
        _require_fresh(loaded)
        try:
            stages.rename(output)
        except OSError:
            if output.exists():
                return read_existing()
            raise
    return {**receipt, 'output_dir': str(output), 'reconciled': False}


def edge_match_sheets(left_sheet, right_sheet, *, boundary_x_or_y=None, tolerance,
                      policy='strict_report', key_fields=(), root=None, output_dir=None):
    """Match WL contacts and WP edge runs. Write only unambiguous equal-attribute midpoint repairs."""
    tolerance = _tolerance(tolerance)
    if policy not in ('strict_report', 'midpoint_snap_matching_attrs'):
        raise BridgeError('DBS_POLICY')
    if isinstance(key_fields, dict) and set(key_fields)-{'wl', 'wp'}:
        raise BridgeError('DBS_SCHEMA: key_fields only applies to wl and wp')
    loaded, pairs = _load([left_sheet, right_sheet])
    report, edits = _measure(*pairs[0], boundary_x_or_y, tolerance, key_fields,
                             policy == 'midpoint_snap_matching_attrs')
    report.update(policy=policy, source_sheets=[sheet.binding() for sheet in loaded])
    if not edits:
        return {**report, 'branches': []}
    if root is None:
        raise BridgeError('DBS_WRITE_ROOT_REQUIRED: supply the owning workspace for native branches')
    root = Path(root).resolve()
    signature = sha(json_text(report).encode('utf-8'))[:20]
    output = _owned(root, output_dir or f'deliverables/dbs/seam-{signature}')

    def render(bridge, stages):
        branches = []
        for sheet in loaded:
            folder = stages/sheet.code
            folder.mkdir()
            layers = {}
            for kind, layer in sheet.layers.items():
                target = folder/(sheet.code+'.'+kind)
                changed = edits.get((sheet.code, kind))
                if changed:
                    selected = [(sheet.code, layer, row, changed.get(row, geom))
                                for row, geom in enumerate(layer.reader.geom)]
                    _write_layer(bridge, target, kind, selected, sheet.scale, f'{sheet.code}-{kind}')
                else:
                    target.write_bytes(layer.blob)
                    if layer.sidecar:
                        sidecar_path(target).write_text(
                            json_text(make_crs_sidecar(target.name, layer.blob, layer.crs)), encoding='utf-8')
                layers[kind] = {'file': str(output/target.relative_to(stages)), 'expected_sha256': sha(target.read_bytes())}
            branches.append({'sheet_id': sheet.code, 'layers': layers})
        return {**report, 'branches': branches}

    return _publish(output, loaded, report, render)


def _library_path(root, library_id):
    return _owned(root, f'work/dbs/libraries/{Bridge.request_id(library_id)}.json')


def build_multisheet_library(root, library_id, sheets, *, tolerance=0., key_fields=()):
    """Persist the sheet hashes, bounds, CRS contract and reports for all adjacent pairs."""
    root = Path(root).resolve()
    path = _library_path(root, library_id)
    tolerance = _tolerance(tolerance)
    loaded, pairs = _load(sheets)
    report = {'schema': 'mapgis-agent/dbs-library/1', 'library_id': library_id,
              **_describe(loaded, pairs), 'seams': [_measure(*pair, None, tolerance, key_fields, False)[0] for pair in pairs]}
    content = json_text(report).encode('utf-8')
    _require_fresh(loaded)
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='index-', dir=path.parent) as temp:
        stage = Path(temp)/path.name
        stage.write_bytes(content)
        try:
            os.link(stage, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise BridgeError('OUTPUT_CONFLICT: this library ID already binds different content')
    return {**report, 'index': str(path), 'index_sha256': sha(content)}


def _clipped_parts(geometry, window):
    clipped = geometry.intersection(window)
    wanted = {'Point': 'Point', 'LineString': 'LineString', 'Polygon': 'Polygon', 'MultiPolygon': 'Polygon'}[geometry.geom_type]
    todo = [clipped]
    while todo:
        piece = todo.pop()
        if piece.is_empty:
            continue
        if piece.geom_type == wanted:
            yield piece
        elif hasattr(piece, 'geoms'):
            todo.extend(reversed(piece.geoms))


def export_multisheet_window(root, library_id, bbox, output_dir):
    """Clip in the ledger CRS and combine records into one native output per nonempty type."""
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise BridgeError('DBS_WINDOW: provide finite [xmin, ymin, xmax, ymax]')
    root = Path(root).resolve()
    index = _library_path(root, library_id)
    raw = index.read_bytes()
    ledger = json.loads(raw)
    if ledger.get('schema') != 'mapgis-agent/dbs-library/1' or ledger['library_id'] != library_id:
        raise BridgeError('DBS_INDEX: incompatible library ledger')
    sheets = [{'sheet_id': sheet['sheet_id'], 'layers': sheet['layers']} for sheet in ledger['sheets']]
    loaded, _ = _load(sheets)
    output = _owned(root, output_dir)
    window = box(*bbox)
    payload = {'library_id': library_id, 'index_sha256': sha(raw), 'bbox': list(bbox), 'crs': ledger['crs']}

    def render(bridge, stages):
        counts, mapping = {}, {}
        for kind in _KINDS:
            selected = [(sheet.code, layer, row, clipped)
                        for sheet in loaded for layer in (sheet.layers[kind],)
                        for row, geometry in enumerate(layer.reader.geom) if geometry.intersects(window)
                        for clipped in _clipped_parts(geometry, window)]
            counts[kind] = len(selected)
            if selected:
                mapping[kind] = _write_layer(bridge, stages/('window.'+kind), kind, selected,
                                             ledger['scale'], 'dbs-window-'+kind)
        if index.read_bytes() != raw:
            raise BridgeError('STALE_SOURCE: DBS library changed during export')
        return {**payload, 'record_counts': counts, 'lineage': mapping}

    return _publish(output, loaded, payload, render)
