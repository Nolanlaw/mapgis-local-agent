"""Portable handoff of saved MapGIS WT/WL/WP and explicitly bound drillhole data."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import ezdxf
import geopandas as gpd
import pyogrio
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely import force_2d, total_bounds
from shapely.geometry import Point, mapping, shape

from .borehole import validate_intervals
from .cad_exchange import _geometry as read_dxf_geometry
from .comparison import _extract_style
from .core import BridgeError, annotation_text, json_text, native_ids, read_native, scalar, sha
from .crs_contract import sidecar_path
from .geology_section import _literal_rows
from .object_store import file_sha256
from .raster import explicit_crs


TARGETS = ('arcgis_pro', 'surpac', 'coreldraw', 'qgis_gpkg', 'geosciml_lite')
DIMENSIONS = ('geometry', 'topology', 'curves', 'z_m', 'crs', 'attributes', 'symbols', 'layout', 'resources')
SCHEMA = 'mapgis-interchange/v1'
MANIFEST = 'interchange-manifest.json'
BOUNDS_TOLERANCE = 1e-7
CAD_APPID = 'MAPGIS_INTERCHANGE'
SVG = 'http://www.w3.org/2000/svg'
DRILL_COLUMNS = {
    'collar': ('hole_id', 'y', 'x', 'z', 'max_depth'),
    'survey': ('hole_id', 'depth', 'dip', 'azimuth'),
    'lithology': ('hole_id', 'depth_from', 'depth_to', 'lithology', 'description'),
}


@dataclass
class _Layer:
    name: str
    source: dict
    kind: str
    crs: CRS | None
    fields: dict
    records: list[dict]

    def extent(self):
        return _extent(record['geometry'] for record in self.records)


def _extent(geometries):
    items = list(geometries)
    bounds = total_bounds(items).tolist()
    if not items or any(item.is_empty or not item.is_valid for item in items) or not all(map(math.isfinite, bounds)):
        raise BridgeError('INTERCHANGE_GEOMETRY: nonempty valid finite features required')
    return {'feature_count': len(items), 'bounds': bounds}


def _fingerprint(path):
    return {'name': path.name, 'sha256': file_sha256(path), 'bytes': path.stat().st_size}


def _json(path, data):
    path.write_text(json_text(data) + '\n', encoding='utf-8')


def _load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _load_layer(root, value, snapshots):
    spec = {'file': value} if isinstance(value, (str, Path)) else value
    if not isinstance(spec, dict) or 'file' not in spec or set(spec) - {'file', 'expected_sha256', 'layer'}:
        raise BridgeError('INTERCHANGE_SOURCE: bind file, expected_sha256 and optional layer')
    source = (root / spec['file']).resolve(strict=True)
    if source.suffix.lower() not in ('.wt', '.wl', '.wp') or not source.is_file():
        raise BridgeError('INTERCHANGE_SOURCE: expected saved WT/WL/WP')
    name = spec.get('layer') or source.stem
    if not isinstance(name, str) or not re.fullmatch(r'[^\W\d]\w{0,62}', name) or name.lower().startswith(('sqlite_', 'gpkg_')):
        raise BridgeError('INTERCHANGE_LAYER: use distinct identifier-style layer names')
    sidecar = sidecar_path(source)
    if sidecar.is_file():
        snapshots[sidecar] = _fingerprint(sidecar)
    blob, reader, sections = read_native(source)
    binding = {'name': source.name, 'sha256': sha(blob), 'bytes': len(blob)}
    if spec.get('expected_sha256', binding['sha256']) != binding['sha256']:
        raise BridgeError('STALE_SOURCE: inspect the current native file first')
    snapshots[source] = binding
    records = [
        {'id': rid, 'geometry': reader.geom[index],
         'properties': {str(key): scalar(value) for key, value in reader.data.iloc[index].items()},
         'style': _extract_style(reader, index),
         'text': annotation_text(blob, sections, rid) if reader.shapeType == 'POINT' else None}
        for index, rid in enumerate(native_ids(reader))
    ]
    layer = _Layer(name, binding, reader.shapeType, getattr(reader, 'effective_crs', reader.crs), reader.fields, records)
    layer.extent()
    return layer


def _coordinate_contract(layers, supplied, target):
    known = [layer.crs for layer in layers if layer.crs is not None]
    local = isinstance(supplied, dict) and supplied.get('kind') == 'local'
    try:
        declared = explicit_crs(supplied) if isinstance(supplied, dict) else CRS.from_user_input(supplied) if supplied else None
    except (CRSError, TypeError, ValueError) as exc:
        raise BridgeError('INTERCHANGE_CRS: use an explicit CRS or referenced local grid') from exc
    if (local and known) or (supplied is None and len(known) != len(layers)):
        raise BridgeError('INTERCHANGE_CRS: bind one explicit frame for all input layers')
    crs = declared or (known[0] if known else None)
    if crs is None:
        if not local or target in ('arcgis_pro', 'qgis_gpkg', 'geosciml_lite'):
            raise BridgeError('INTERCHANGE_CRS: target needs a geographic or projected CRS')
        return {'axis_order': 'xy', 'local': supplied, 'unit': supplied['unit'], 'crs_uri': None, 'wkt2': None}
    if not ((crs.is_projected or crs.is_geographic) and len(crs.axis_info) == 2):
        raise BridgeError('INTERCHANGE_CRS: expected a horizontal 2D CRS')
    if any(not crs.equals(other, ignore_axis_order=True) for other in known):
        raise BridgeError('INTERCHANGE_CRS: input frames differ, including any false easting')
    if target in ('coreldraw', 'surpac') and not crs.is_projected:
        raise BridgeError('INTERCHANGE_CRS: CAD and string files require planar coordinates')
    wkt = crs.to_wkt(version='WKT2_2019')
    authority = crs.to_authority(min_confidence=100)
    uri = (f'http://www.opengis.net/def/crs/{authority[0]}/0/{authority[1]}' if authority else
           'urn:mapgis:crs:sha256:' + sha(wkt.encode('utf8')))
    return {'axis_order': 'xy', 'crs_uri': uri, 'wkt2': wkt, 'unit': crs.axis_info[0].unit_name}


def _palette():
    rules = _load_json(Path(__file__).with_name('data') / 'standards' / 'cartography-rules-v1.json')
    entries = {}
    for code, row in rules['codes'].items():
        if 'recommended_rgb' not in row:
            continue
        cmyk = row['color_codes'][0]
        channels = dict(re.findall(r'([CMYK])(\d+)', cmyk))
        entries[code] = {'rgb': row['recommended_rgb'], 'cmyk_code': cmyk,
                         'cmyk_percent': [int(channels.get(channel, 0)) for channel in 'CMYK']}
    return {'standard': 'DZ/T 0179-2025', 'reference': rules['palette_reference'], 'entries': entries}


def _record_colour(record, metadata, palette):
    style, properties = record['style'], record['properties']
    color_id = style.get('fill_color', style.get('color'))
    rgb = metadata.get('colors', {}).get(str(color_id), properties.get('RGB'))
    if isinstance(rgb, str):
        try:
            rgb = [int(part.strip()) for part in rgb.split(',')]
        except ValueError as exc:
            raise BridgeError('INTERCHANGE_STYLE: RGB requires three integer channels') from exc
    if rgb is not None:
        if not isinstance(rgb, (tuple, list)) or len(rgb) != 3 or not all(type(v) is int and 0 <= v <= 255 for v in rgb):
            raise BridgeError('INTERCHANGE_STYLE: RGB requires three integer channels')
        return tuple(rgb)
    code = properties.get('CODE')
    return tuple(palette.get(str(code), {'rgb': (64, 64, 64)})['rgb'])


def _layer_styles(layers, metadata, palette):
    return {
        layer.name: {'kind': layer.kind, 'source': layer.source,
                     'features': [{'record_id': record['id'], 'native_style': record['style'],
                                   'annotation': record['text'], 'rgb': _record_colour(record, metadata, palette)}
                                  for record in layer.records]}
        for layer in layers
    }


def _gpkg_frame(layer, crs):
    names = {}
    used = {'fid', 'geometry', 'geom', '_mapgis_record', '_mapgis_label'}
    for name in layer.records[0]['properties']:
        column = name
        while column.casefold() in used:
            column = '_' + column
        names[name] = column
        used.add(column.casefold())
    rows = [
        {names[key]: value for key, value in record['properties'].items()}
        | {'_mapgis_record': record['id'], '_mapgis_label': record['text']}
        for record in layer.records
    ]
    return gpd.GeoDataFrame(rows, crs=crs, geometry=[row['geometry'] for row in layer.records]), names


def _qml(path, kind, rgb):
    symbol_type, layer_class = {'POINT': ('marker', 'SimpleMarker'), 'LINE': ('line', 'SimpleLine'),
                               'POLYGON': ('fill', 'SimpleFill')}[kind]
    qgis = ET.Element('qgis', styleCategories='Symbology')
    renderer = ET.SubElement(qgis, 'renderer-v2', type='singleSymbol')
    symbol = ET.SubElement(ET.SubElement(renderer, 'symbols'), 'symbol', type=symbol_type, name='0')
    layer = ET.SubElement(symbol, 'layer', {'class': layer_class, 'enabled': '1'})
    values = ET.SubElement(layer, 'Option', type='Map')
    ET.SubElement(values, 'Option', name='line_color' if kind == 'LINE' else 'color',
                  type='QString', value=','.join(str(channel) for channel in (*rgb, 255)))
    ET.ElementTree(qgis).write(path, encoding='utf-8', xml_declaration=True)


def _write_gpkg(folder, layers, contract, styles, qgis):
    fields = {}
    for layer in layers:
        frame, names = _gpkg_frame(layer, CRS.from_wkt(contract['wkt2']))
        pyogrio.write_dataframe(frame, folder / 'layers.gpkg', layer=layer.name, driver='GPKG')
        fields[layer.name] = {'native_fields': layer.fields, 'field_map': names,
                              'record_field': '_mapgis_record', 'annotation_field': '_mapgis_label'}
        if qgis:
            _qml(folder / f'{layer.name}.qml', layer.kind, styles[layer.name]['features'][0]['rgb'])
    _json(folder / 'field-dictionary.json', fields)


def _parts(geometry):
    return geometry.geoms if geometry.geom_type.startswith('Multi') else (geometry,)


def _strings(geometry):
    for part in _parts(geometry):
        if part.geom_type == 'Polygon':
            yield from (ring.coords for ring in (part.exterior, *part.interiors))
        else:
            yield part.coords


def _write_str(path, layers, base_z):
    with path.open('w', encoding='ascii', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('mapgis', '', 'axes=Y:Northing X:Easting Z:Elevation', ''))
        writer.writerow((0, 0, 0, 0, 0, 0, 0))
        string_no = 0
        for layer_no, layer in enumerate(layers, 1):
            for record in layer.records:
                string_no += 1
                if string_no > 32000:
                    raise BridgeError('INTERCHANGE_SURPAC: string IDs are limited to 32000')
                description = f'L{layer_no}:R{record["id"]}'
                for coordinates in _strings(record['geometry']):
                    for x, y, *rest in coordinates:
                        writer.writerow((string_no, y, x, rest[0] if rest else base_z, description))
                    writer.writerow((0, 0, 0, 0, ''))
        writer.writerow((0, 0, 0, 0, 'END'))


def _drillholes(root, definitions, snapshots):
    if not isinstance(definitions, dict) or set(definitions) != set(DRILL_COLUMNS):
        raise BridgeError('INTERCHANGE_DRILLHOLES: bind collar, survey and lithology tables')
    tables = {}
    for kind, spec in definitions.items():
        path = (root / spec['file']).resolve(strict=True)
        snapshots[path] = _fingerprint(path)
        rows, _ = _literal_rows({**spec, 'file': str(path)}, kind)
        if not rows:
            raise BridgeError('INTERCHANGE_DRILLHOLES: all three tables require actual rows')
        tables[kind] = rows

    collars = {row['hole_id']: row for row in tables['collar']}
    if len(collars) != len(tables['collar']):
        raise BridgeError('BOREHOLE_DUPLICATE')
    stations = {hole: [] for hole in collars}
    intervals = {hole: [] for hole in collars}
    for key, group in [('survey', stations), ('lithology', intervals)]:
        for row in tables[key]:
            if row['hole_id'] not in group:
                raise BridgeError('BOREHOLE_UNKNOWN')
            group[row['hole_id']].append(row)

    result = {name: [] for name in DRILL_COLUMNS}
    for hole, collar in collars.items():
        survey = stations[hole]
        depths = [row['md'] for row in survey]
        if (len(depths) < 2 or depths[0] != 0 or any(b <= a for a, b in zip(depths, depths[1:]))
                or any(not (0 <= row['azimuth'] < 360 and 0 <= row['inclination'] < 180) for row in survey)):
            raise BridgeError('INTERCHANGE_DRILLHOLES: ordered survey from MD zero and valid azimuth/inclination required')
        logged, _ = validate_intervals(
            [{key: value for key, value in row.items() if key != 'hole_id'} for row in intervals[hole]], depths[-1])
        result['collar'].append((hole, collar['northing'], collar['easting'], collar['elevation'], depths[-1]))
        result['survey'].extend((hole, row['md'], row['inclination'] - 90, row['azimuth']) for row in survey)
        result['lithology'].extend((hole, row['top'], row['base'], row['unit'], row['description']) for row in logged)
    return result


def _write_surpac(folder, root, layers, contract, metadata, snapshots):
    if contract['unit'] not in ('metre', 'meter'):
        raise BridgeError('INTERCHANGE_CRS: Surpac drilling handoff requires XY in metres')
    base_z = metadata.get('default_z')
    if any(not record['geometry'].has_z for layer in layers for record in layer.records):
        if type(base_z) not in (int, float) or not math.isfinite(base_z):
            raise BridgeError('INTERCHANGE_SURPAC: declare default_z for 2D vector features')
    tables = _drillholes(root, metadata.get('drillholes'), snapshots)
    _write_str(folder / 'mapgis.str', layers, base_z)
    for name, rows in tables.items():
        with (folder / f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(DRILL_COLUMNS[name])
            writer.writerows(rows)
    return {name: len(rows) for name, rows in tables.items()}


def _text_height(record, display_height):
    height = record['style'].get('height') or 1
    return min(display_height * .06, max(display_height * .008, float(height)))


def _cad_entities(space, geometry, record, attrs, height):
    if geometry.geom_type == 'Point':
        if record['text']:
            text = space.add_text(record['text'], dxfattribs={**attrs, 'insert': geometry.coords[0], 'height': height,
                                  'rotation': record['style'].get('angle') or 0, 'style': 'MAPGIS_TEXT'})
            return [text]
        return [space.add_point(geometry.coords[0], dxfattribs=attrs)]
    if geometry.geom_type in ('LineString', 'MultiLineString'):
        return [space.add_lwpolyline(part.coords, format='xy', dxfattribs=attrs) for part in _parts(geometry)]
    hatch = space.add_hatch(dxfattribs=attrs)
    for part in _parts(geometry):
        hatch.paths.add_polyline_path(part.exterior.coords, is_closed=True, flags=1)
        for ring in part.interiors:
            hatch.paths.add_polyline_path(ring.coords, is_closed=True)
    return [hatch]


def _svg_feature(group, geometry, record, rgb, height, radius):
    color = '#%02x%02x%02x' % tuple(rgb)
    if geometry.geom_type == 'Point':
        x, y = geometry.coords[0]
        if record['text']:
            ET.SubElement(group, 'text', x=str(x), y=str(-y), fill=color,
                          **{'font-size': str(height)}).text = record['text']
        else:
            ET.SubElement(group, 'circle', cx=str(x), cy=str(-y), r=str(radius), fill=color)
        return
    polygon = geometry.geom_type in ('Polygon', 'MultiPolygon')
    paths = []
    for coordinates in _strings(geometry):
        pairs = ' L '.join(f'{x!r} {-y!r}' for x, y in coordinates)
        paths.append('M ' + pairs + (' Z' if polygon else ''))
    ET.SubElement(group, 'path', d=' '.join(paths), fill=color if polygon else 'none',
                  stroke=color, **{'fill-rule': 'evenodd', 'stroke-width': str(radius / 2)})


def _write_cad(folder, layers, contract, styles, palette):
    doc = ezdxf.new('R2013')
    doc.units = {'metre': 6, 'meter': 6, 'millimetre': 4, 'foot': 2}.get(contract['unit'], 0)
    doc.appids.new(CAD_APPID)
    doc.styles.new('MAPGIS_TEXT', dxfattribs={'font': 'simsun.ttc'})
    space = doc.modelspace()
    bounds = _extent(record['geometry'] for layer in layers for record in layer.records)['bounds']
    x0, y0, x1, y1 = bounds
    span = max(x1 - x0, y1 - y0, 1.)
    pad = span * .05
    svg = ET.Element('svg', xmlns=SVG, viewBox=f'{x0-pad} {-y1-pad} {x1-x0+2*pad} {y1-y0+2*pad}')
    for layer in layers:
        doc.layers.new(layer.name)
        group = ET.SubElement(svg, 'g', id=layer.name)
        for record, style in zip(layer.records, styles[layer.name]['features'], strict=True):
            geometry = force_2d(record['geometry'])
            height = _text_height(record, span)
            attrs = {'layer': layer.name, 'true_color': ezdxf.colors.rgb2int(style['rgb'])}
            for entity in _cad_entities(space, geometry, record, attrs, height):
                entity.set_xdata(CAD_APPID, [(1000, str(record['id']))])
            feature = ET.SubElement(group, 'g', **{'data-record': str(record['id'])})
            _svg_feature(feature, geometry, record, style['rgb'], height, span * .004)
    doc.saveas(folder / 'mapgis.dxf')
    ET.ElementTree(svg).write(folder / 'preview.svg', encoding='utf-8', xml_declaration=True)
    _json(folder / 'dz-t-0179-2025-colors.json', palette)


def _first_code(properties, keys):
    return next((str(properties[key]) for key in keys if properties.get(key) not in (None, '')), None)


def _write_geosciml(folder, layers, contract, metadata):
    stratigraphy = ('stratigraphic_code', 'SF_CODE', 'CODE', '地层代号')
    lithology = ('lithology_code', 'LITH_CODE', '岩性代码', '岩性')
    if metadata.get('stratigraphy_field'):
        stratigraphy = (metadata['stratigraphy_field'],)
    if metadata.get('lithology_field'):
        lithology = (metadata['lithology_field'],)
    for layer in layers:
        features = []
        for record in layer.records:
            identifier = f'{layer.name}:{record["id"]}'
            properties = record['properties']
            features.append({
                'type': 'Feature', 'id': identifier, 'geometry': mapping(record['geometry']),
                'properties': {'identifier': identifier, 'stratigraphicCode': _first_code(properties, stratigraphy),
                               'lithologyCode': _first_code(properties, lithology), 'nativeAttributes': properties,
                               'source': {'sha256': layer.source['sha256'], 'physical_record_id': record['id']}},
            })
        _json(folder / f'{layer.name}.geojson', {
            'type': 'FeatureCollection', 'features': features, 'crs_uri': contract['crs_uri'],
            'crs': {'type': 'name', 'properties': {'name': contract['crs_uri']}},
            'profile': 'GeoSciML-Lite property mapping in GeoJSON with explicit source CRS',
            'bbox': layer.extent()['bounds'],
        })


def _retention(target, layers):
    has_regions = any(layer.kind == 'POLYGON' for layer in layers)
    has_curved_points = any(record['style'].get('point_type') in (2, 3)
                            for layer in layers for record in layer.records if layer.kind == 'POINT')
    geometry = ('PARTIAL', 'WT 圆／弧以定位点表示。') if has_curved_points else ('LOSSLESS', '保留已解码 XY 坐标及要素计数。')
    if target == 'surpac' and has_regions:
        geometry = ('PARTIAL', '面环输出为封闭线串，Surpac 不保留原面对象。')
    matrix = {
        'geometry': geometry,
        'topology': ('PARTIAL', '保留面环／弧段坐标，省略原生共享弧、结点 ID 与邻接关系。') if has_regions else
                    ('DROPPED', '不携带 MapGIS 原生连通和物理拓扑索引。'),
        'curves': ('PARTIAL', '存储顶点保留，WT 圆弧不转换成目标曲线对象。') if has_curved_points else
                  ('DROPPED', '仅交换已解码顶点，不重建 CAD 或 MapGIS 参数曲线。'),
        'z_m': ('PARTIAL', '钻孔高程和测深保留。二维矢量的 STR 高程使用显式填充值。') if target == 'surpac' else
               ('DROPPED', '本包以读取的平面矢量为准，不承诺原生 Z/M 字段或高程基准的保真。'),
        'crs': ('LOSSLESS', '保持 XY 数值，伴生契约记录原始 WKT2 与轴序。') if target != 'surpac' else
               ('PARTIAL', 'STR 列序为 Y 北、X 东、Z 高，坐标参考依赖伴生契约。'),
        'attributes': ('PARTIAL', '保留逻辑字段或联结标识，原生字段类型、字节和编码未完整映射。'),
        'symbols': ('PARTIAL', '保留原生样式参数和简化色值。Slib 子图、字库、花纹及线型需重新配置。'),
        'layout': ('DROPPED', '未交换 MPJ 页面、视口、图例排版和打印参数。'),
        'resources': ('DROPPED', '不复制 Slib、原厂字体、外链宿主资源。'),
    }
    return {key: {'status': status, 'reason': reason} for key, (status, reason) in matrix.items()}


def export_interchange_package(root, sources, target, output_dir, *, crs=None, metadata=None):
    """Publish a verified new folder under work/ or deliverables/.

    Bind saved native vectors with {file, expected_sha256, layer?}. Bare paths
    snapshot the current bytes. Surpac additionally needs metadata.drillholes
    (hash-bound collar/survey/lithology ledgers) and default_z for 2D vectors.
    Declared frames are labels for source coordinates. No reprojection is applied.
    """
    if target not in TARGETS:
        raise BridgeError('INTERCHANGE_TARGET')
    if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 100:
        raise BridgeError('INTERCHANGE_SOURCE: expected 1..100 saved vector layers')
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, dict) or not isinstance(metadata.get('colors', {}), dict):
        raise BridgeError('INTERCHANGE_METADATA')
    root = Path(root).resolve()
    output = (root / output_dir).resolve()
    if not any(output.is_relative_to(root / name) and output != root / name for name in ('work', 'deliverables')):
        raise BridgeError('INTERCHANGE_WRITE_ROOT')
    if output.exists():
        raise BridgeError('OUTPUT_CONFLICT')

    snapshots = {}
    layers = [_load_layer(root, source, snapshots) for source in sources]
    if len({layer.name.casefold() for layer in layers}) != len(layers):
        raise BridgeError('INTERCHANGE_LAYER: provide unique layer names')
    contract = _coordinate_contract(layers, crs, target)
    if target == 'surpac':
        contract['string_axis_order'] = ['Y=northing', 'X=easting', 'Z=elevation']
        contract['survey_dip'] = 'degrees from horizontal, negative down = inclination from vertical down minus 90'
        contract['default_z_for_2d'] = metadata.get('default_z')
    palette = _palette()
    styles = _layer_styles(layers, metadata, palette['entries'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.interchange-', dir=output.parent) as staging:
        folder = Path(staging) / 'package'
        folder.mkdir()
        _json(folder / 'crs-contract.json', contract)
        _json(folder / 'layer-styles.json', styles)
        extras = {}
        if target in ('arcgis_pro', 'qgis_gpkg'):
            _write_gpkg(folder, layers, contract, styles, qgis=target == 'qgis_gpkg')
        elif target == 'surpac':
            extras['drillhole_counts'] = _write_surpac(folder, root, layers, contract, metadata, snapshots)
        elif target == 'coreldraw':
            _write_cad(folder, layers, contract, styles, palette)
        else:
            _write_geosciml(folder, layers, contract, metadata)

        manifest = {
            'schema': SCHEMA, 'target': target, 'inputs': list(snapshots.values()),
            'outputs': [_fingerprint(path) for path in sorted(folder.iterdir())],
            'layers': [{'name': layer.name, 'source': layer.source, **layer.extent()} for layer in layers],
            'retention_matrix': _retention(target, layers), **extras,
        }
        _json(folder / MANIFEST, manifest)
        result = verify_interchange_package(folder)
        if any(_fingerprint(path) != bound for path, bound in snapshots.items()):
            raise BridgeError('STALE_SOURCE: interchange input changed during export')
        if output.exists():
            raise BridgeError('OUTPUT_CONFLICT')
        folder.rename(output)
    return {**result, 'package_dir': str(output), 'manifest': str(output / MANIFEST)}


def _read_str(path, layers):
    with path.open(encoding='ascii', newline='') as stream:
        records = list(csv.reader(stream))
    if (len(records) < 5 or len(records[0]) != 4 or list(map(float, records[1])) != [0.] * 7
            or records[-1] != ['0', '0', '0', '0', 'END']):
        raise BridgeError('INTERCHANGE_READBACK: invalid STR header, axis or END')
    groups = {item['name']: {} for item in layers}
    bindings = {}
    for number, north, east, z, description in records[2:-1]:
        identity = int(number)
        north, east, z = float(north), float(east), float(z)
        if identity == 0 and (north, east, z, description) == (0., 0., 0., ''):
            continue
        if not (0 < identity <= 32000 and all(map(math.isfinite, (north, east, z)))):
            raise BridgeError('INTERCHANGE_READBACK: invalid STR coordinate row')
        match = re.fullmatch(r'L([1-9]\d*):R([1-9]\d*)', description)
        if not match or int(match[1]) > len(layers):
            raise BridgeError('INTERCHANGE_READBACK: invalid STR physical record reference')
        name = layers[int(match[1]) - 1]['name']
        key = (name, int(match[2]))
        if bindings.setdefault(identity, key) != key:
            raise BridgeError('INTERCHANGE_READBACK: ambiguous STR identity')
        groups[name].setdefault(identity, []).append(Point(east, north))
    return {name: _group_extent(items) for name, items in groups.items()}


def _group_extent(records):
    return _extent(geometry for group in records.values() for geometry in group) | {'feature_count': len(records)}


def _read_surpac(folder, manifest):
    for name, fields in DRILL_COLUMNS.items():
        with (folder / f'{name}.csv').open(encoding='utf-8-sig', newline='') as stream:
            table = csv.DictReader(stream)
            if table.fieldnames != list(fields):
                raise BridgeError('INTERCHANGE_READBACK: invalid drillhole CSV schema')
            rows = list(table)
        if len(rows) != manifest['drillhole_counts'][name] or not rows:
            raise BridgeError('INTERCHANGE_READBACK: drillhole CSV row count differs')
        for row in rows:
            if set(row) != set(fields) or any(value is None for value in row.values()) or not row['hole_id']:
                raise BridgeError('INTERCHANGE_READBACK: invalid drillhole CSV row')
            numbers = set(fields) - {'hole_id', 'lithology', 'description'}
            if not all(math.isfinite(float(row[col])) for col in numbers):
                raise BridgeError('INTERCHANGE_READBACK: non-finite drillhole value')
    return _read_str(folder / 'mapgis.str', manifest['layers'])


def _read_cad(folder, styles):
    doc = ezdxf.readfile(folder / 'mapgis.dxf')
    if doc.audit().errors:
        raise BridgeError('INTERCHANGE_READBACK: invalid DXF structure')
    records = {layer: {} for layer in styles}
    expected = {layer: {str(item['record_id']): item for item in entry['features']} for layer, entry in styles.items()}
    for entity in doc.modelspace():
        identity = entity.get_xdata(CAD_APPID)[0].value
        layer = entity.dxf.layer
        geometry, text, _ = read_dxf_geometry(entity, None)
        style = expected[layer][identity]
        if text != (style['annotation'] or None) or list(entity.rgb) != style['rgb']:
            raise BridgeError('INTERCHANGE_READBACK: DXF annotation or color differs')
        records[layer].setdefault(identity, []).append(geometry)
    svg = ET.parse(folder / 'preview.svg').getroot()
    if svg.tag != f'{{{SVG}}}svg' or {group.get('id'): len(group) for group in svg} != {
            name: len(records_by_id) for name, records_by_id in records.items()}:
        raise BridgeError('INTERCHANGE_READBACK: malformed SVG feature groups')
    return {name: _group_extent(items) for name, items in records.items()}


def _read_gpkg(folder, names, contract, qgis):
    path = folder / 'layers.gpkg'
    if {layer for layer, _ in pyogrio.list_layers(path)} != set(names):
        raise BridgeError('INTERCHANGE_READBACK: unexpected GeoPackage layer names')
    fields = _load_json(folder / 'field-dictionary.json')
    if fields.keys() != set(names):
        raise BridgeError('INTERCHANGE_READBACK: incomplete field dictionary')
    result = {}
    for name in names:
        frame = pyogrio.read_dataframe(path, layer=name)
        if frame.crs is None or not frame.crs.equals(CRS.from_wkt(contract['wkt2']), ignore_axis_order=True):
            raise BridgeError('INTERCHANGE_READBACK: GeoPackage CRS changed')
        if not {*fields[name]['field_map'].values(), '_mapgis_record', '_mapgis_label'}.issubset(frame.columns):
            raise BridgeError('INTERCHANGE_READBACK: GeoPackage fields differ')
        if qgis and ET.parse(folder / f'{name}.qml').getroot().find('renderer-v2') is None:
            raise BridgeError('INTERCHANGE_READBACK: missing QML renderer')
        result[name] = _extent(frame.geometry)
    return result


def _read_geosciml(folder, layers, contract):
    result = {}
    for layer in layers:
        name = layer['name']
        document = _load_json(folder / f'{name}.geojson')
        if document['type'] != 'FeatureCollection' or document['crs_uri'] != contract['crs_uri']:
            raise BridgeError('INTERCHANGE_READBACK: wrong GeoSciML-Lite collection or CRS')
        for feature in document['features']:
            properties = feature['properties']
            if not {'stratigraphicCode', 'lithologyCode', 'source'} <= properties.keys():
                raise BridgeError('INTERCHANGE_READBACK: incomplete GeoSciML-Lite properties')
            if properties['source']['sha256'] != layer['source']['sha256']:
                raise BridgeError('INTERCHANGE_READBACK: GeoSciML-Lite source hash differs')
        result[name] = _extent(shape(feature['geometry']) for feature in document['features'])
    return result


def _required_members(target, names):
    shared = {'crs-contract.json', 'layer-styles.json'}
    if target in ('arcgis_pro', 'qgis_gpkg'):
        shared.update({'layers.gpkg', 'field-dictionary.json'})
        if target == 'qgis_gpkg':
            shared.update(f'{name}.qml' for name in names)
    elif target == 'surpac':
        shared.update({'mapgis.str', *(f'{name}.csv' for name in DRILL_COLUMNS)})
    elif target == 'coreldraw':
        shared.update({'mapgis.dxf', 'preview.svg', 'dz-t-0179-2025-colors.json'})
    else:
        shared.update(f'{name}.geojson' for name in names)
    return shared


def _verify(folder):
    paths = {path.name: path for path in folder.iterdir()}
    if any(path.is_symlink() or path.is_junction() or not path.is_file() for path in paths.values()):
        raise BridgeError('INTERCHANGE_INTEGRITY: package members must be direct regular files')
    manifest = _load_json(paths[MANIFEST])
    target = manifest['target']
    if manifest['schema'] != SCHEMA or target not in TARGETS:
        raise BridgeError('INTERCHANGE_INTEGRITY: unknown schema or target')
    matrix = manifest['retention_matrix']
    if set(matrix) != set(DIMENSIONS) or any(
            item['status'] not in ('LOSSLESS', 'PARTIAL', 'DROPPED') or not item['reason']
            for item in matrix.values()):
        raise BridgeError('INTERCHANGE_INTEGRITY: invalid retention declaration')
    layers = manifest['layers']
    names = [layer['name'] for layer in layers]
    if not names or len(set(names)) != len(names) or not all(re.fullmatch(r'[^\W\d]\w{0,62}', name) for name in names):
        raise BridgeError('INTERCHANGE_INTEGRITY: unsafe or duplicate layer name')
    outputs = {row['name']: row for row in manifest['outputs']}
    members = _required_members(target, names)
    if set(outputs) != members or paths.keys() != (members | {MANIFEST}) or len(outputs) != len(manifest['outputs']):
        raise BridgeError('INTERCHANGE_INTEGRITY: unexpected, unlisted or missing package member')
    if any(_fingerprint(paths[name]) != row for name, row in outputs.items()):
        raise BridgeError('INTERCHANGE_INTEGRITY: output SHA-256 or byte size differs')
    for source in manifest['inputs']:
        if not re.fullmatch('[0-9a-f]{64}', source['sha256']) or type(source['bytes']) is not int or source['bytes'] < 1:
            raise BridgeError('INTERCHANGE_INTEGRITY: invalid source provenance')
    if any(layer['source'] not in manifest['inputs'] for layer in layers):
        raise BridgeError('INTERCHANGE_INTEGRITY: layer lacks source provenance')

    contract = _load_json(paths['crs-contract.json'])
    styles = _load_json(paths['layer-styles.json'])
    if contract['axis_order'] != 'xy' or styles.keys() != set(names):
        raise BridgeError('INTERCHANGE_READBACK: inconsistent CRS or style contract')
    if target in ('arcgis_pro', 'qgis_gpkg'):
        actual = _read_gpkg(folder, names, contract, qgis=target == 'qgis_gpkg')
    elif target == 'surpac':
        if contract['string_axis_order'] != ['Y=northing', 'X=easting', 'Z=elevation']:
            raise BridgeError('INTERCHANGE_READBACK: invalid STR axis contract')
        actual = _read_surpac(folder, manifest)
    elif target == 'coreldraw':
        palette = _load_json(paths['dz-t-0179-2025-colors.json'])
        if palette['standard'] != 'DZ/T 0179-2025' or not palette['entries']:
            raise BridgeError('INTERCHANGE_READBACK: missing RGB/CMYK reference')
        actual = _read_cad(folder, styles)
    else:
        actual = _read_geosciml(folder, layers, contract)

    for layer in layers:
        observed = actual[layer['name']]
        if observed['feature_count'] != layer['feature_count'] or not all(
                math.isclose(a, b, rel_tol=0., abs_tol=BOUNDS_TOLERANCE)
                for a, b in zip(observed['bounds'], layer['bounds'], strict=True)):
            raise BridgeError('INTERCHANGE_READBACK: feature count or XY bounds differ')
    return {'verified': True, 'target': target, 'feature_count': sum(row['feature_count'] for row in actual.values()),
            'layers': actual, 'verified_outputs': sorted(outputs), 'retention_matrix': matrix}


def verify_interchange_package(package_dir):
    """Check output bytes and structurally read every member, independently of input files."""
    try:
        return _verify(Path(package_dir))
    except BridgeError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ezdxf.DXFError,
            pyogrio.errors.DataSourceError, ET.ParseError) as exc:
        raise BridgeError('INTERCHANGE_READBACK: malformed or unreadable package') from exc
