"""Hash-bound drillhole ledger, section and measured-depth logs in editable MapGIS."""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
import shutil

from openpyxl import load_workbook
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape

from .artifacts import begin_bundle, fail_bundle, finish_bundle
from .borehole import _frame, _number, apparent_dip, validate_intervals
from .borehole_charts import borehole_chart_spec
from .core import BridgeError, json_text, sha
from .creation import prepare_creation
from .native_backend import native_file_info
from .native_project import prepare_native_project, native_project_info
from .raster import file_sha
from .tables import write_literal_workbook


REQUIRED = {
    'collar': ('hole_id', 'easting', 'northing', 'elevation'),
    'survey': ('hole_id', 'md', 'inclination', 'azimuth'),
    'lithology': ('hole_id', 'top', 'base', 'unit'),
    'assay': ('hole_id', 'top', 'base', 'analyte', 'value', 'unit'),
}
NUMERIC = {'easting', 'northing', 'elevation', 'md', 'inclination', 'azimuth', 'top', 'base', 'value'}
VECTOR_TYPES = {'POLYGON': '.wp', 'LINE': '.wl', 'POINT': '.wt'}


def _literal_rows(source, section):
    if not isinstance(source, dict) or not {'file', 'expected_sha256'} <= set(source) <= {'file', 'expected_sha256', 'sheet'}:
        raise BridgeError('DRILLHOLE_LEDGER_SOURCE: '+section)
    path = Path(source['file']).resolve(strict=True)
    raw = path.read_bytes()
    if sha(raw) != source['expected_sha256']:
        raise BridgeError('STALE_SOURCE: '+path.name)
    suffix = path.suffix.lower()
    if suffix == '.json':
        rows = json.loads(raw.decode('utf-8-sig'))
        if isinstance(rows, dict):
            rows = rows.get(section)
    elif suffix == '.csv':
        table = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
        if not table.fieldnames or len(set(table.fieldnames)) != len(table.fieldnames):
            raise BridgeError('DRILLHOLE_LEDGER_COLUMNS: '+section)
        rows = []
        for row in table:
            try:
                rows.append({key: float(value) if key in NUMERIC else value for key, value in row.items()})
            except (ValueError, TypeError) as error:
                raise BridgeError('DRILLHOLE_LEDGER_NUMBER: '+section) from error
    elif suffix == '.xlsx':
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = source.get('sheet', section)
            if sheet not in workbook:
                raise BridgeError('DRILLHOLE_LEDGER_SHEET: '+str(sheet))
            cells = workbook[sheet].iter_rows()
            headers = [cell.value for cell in next(cells, ())]
            if not headers or len(set(headers)) != len(headers):
                raise BridgeError('DRILLHOLE_LEDGER_COLUMNS: '+section)
            rows = []
            for row in cells:
                if any(cell.data_type == 'f' for cell in row):
                    raise BridgeError('BOREHOLE_FORMULA: '+section)
                if any(cell.value is not None for cell in row):
                    rows.append(dict(zip(headers, (cell.value for cell in row))))
        finally:
            workbook.close()
    else:
        raise BridgeError('DRILLHOLE_LEDGER_FORMAT: expected CSV, JSON or XLSX')
    required = set(REQUIRED[section]); allowed = required | ({'description'} if section == 'lithology' else set())
    if not isinstance(rows, list) or len(rows) > 100_000:
        raise BridgeError('DRILLHOLE_LEDGER_ROWS: '+section)
    for row in rows:
        if not isinstance(row, dict) or not required <= set(row) <= allowed:
            raise BridgeError('DRILLHOLE_LEDGER_COLUMNS: '+section)
        for key in required:
            value = row[key]
            if (key in NUMERIC and not _number(value)) or (key not in NUMERIC and (not isinstance(value, str) or not value.strip())):
                raise BridgeError('DRILLHOLE_LEDGER_LITERAL: '+section+'.'+key)
    if file_sha(path) != source['expected_sha256']:
        raise BridgeError('STALE_DURING_TABLE_READ')
    return rows, {'file': str(path), 'sha256': source['expected_sha256']}


def read_drillhole_ledger(sources, coordinate_frame):
    frame = _frame(coordinate_frame)
    if not isinstance(sources, dict) or set(sources) != set(REQUIRED):
        raise BridgeError('DRILLHOLE_LEDGER: bind collar, survey, lithology and assay')
    tables, fingerprints = {}, []
    for kind, source in sources.items():
        rows, binding = _literal_rows(source, kind)
        tables[kind] = rows
        if binding not in fingerprints:
            fingerprints.append(binding)
    holes, assays, units = {}, {}, {}
    for row in tables['collar']:
        identity = row['hole_id']
        if identity in holes:
            raise BridgeError('BOREHOLE_DUPLICATE: '+identity)
        holes[identity] = {'id': identity, 'collar': {key: row[key] for key in ('easting', 'northing', 'elevation')},
                           'survey': [], 'intervals': []}
    if not 1 <= len(holes) <= 100:
        raise BridgeError('BOREHOLE_CAPACITY: 1..100 collars')
    for kind, key in [('survey', 'survey'), ('lithology', 'intervals'), ('assay', None)]:
        for row in tables[kind]:
            identity = row['hole_id']
            if identity not in holes:
                raise BridgeError('BOREHOLE_UNKNOWN: '+identity)
            if key:
                values = {name: value for name, value in row.items() if name != 'hole_id'}
                if key == 'intervals':
                    values['description'] = values.get('description') or ''
                holes[identity][key].append(values)
            else:
                analyte = row['analyte']
                if units.setdefault(analyte, row['unit']) != row['unit'] or row['value'] < 0:
                    raise BridgeError('DRILLHOLE_ASSAY_UNITS_OR_VALUE: '+analyte)
                assays.setdefault(identity, {}).setdefault(analyte, []).append(row)
    if any(len(hole['survey']) < 2 for hole in holes.values()):
        raise BridgeError('BOREHOLE_SURVEY: every hole needs a survey')
    if len(units) > 8:
        raise BridgeError('DRILLHOLE_ASSAY_TRACKS: at most eight analytes')
    for identity, members in assays.items():
        for group in members.values():
            validate_intervals([{'top': row['top'], 'base': row['base'], 'unit': row['unit']} for row in group],
                               holes[identity]['survey'][-1]['md'])
    return {'tables': tables, 'sources': fingerprints, 'holes': list(holes.values()), 'assays': assays,
            'assay_units': units, 'coordinate_frame': frame}


def section_dip(dip_direction, dip, section_azimuth, vertical_exaggeration):
    if not _number(vertical_exaggeration) or vertical_exaggeration <= 0:
        raise BridgeError('DRILLHOLE_VERTICAL_EXAGGERATION')
    record = apparent_dip(dip_direction, dip, section_azimuth)
    alpha = record['apparent_dip_degrees']
    angle = math.radians(alpha)
    displayed = alpha if abs(alpha) == 90 else math.degrees(math.atan2(vertical_exaggeration*math.sin(angle), math.cos(angle)))
    return {**record, 'vertical_exaggeration': vertical_exaggeration,
            'displayed_apparent_dip_degrees': displayed, 'positive_displayed_dip': 'down along increasing section distance'}


def _add(features, kind, geometry, role, *, hole='', text=None, color=1, font=2.5):
    rows = features[kind]['features']
    feature = {'type': 'Feature', 'id': str(len(rows)+1), 'geometry': mapping(geometry),
               'properties': {'HOLE': hole, 'ROLE': role}}
    if text is None:
        feature['style'] = {'linetype': 1, 'color': color, 'width': .25}
    else:
        feature.update(text=text, style={'color': color, 'height': font, 'width': font*.8, 'font_zh': 1})
        feature['properties']['UNIT'] = ''
    rows.append(feature)


def _frame_plot(features, font):
    bounds = []
    for collection in features.values():
        for row in collection['features']:
            x0, y0, x1, y1 = shape(row['geometry']).bounds
            if row.get('text'):
                x1 += len(row['text'].encode('gbk'))*font*.5
                y1 += font
            bounds.append((x0, y0, x1, y1))
    plot = box(min(x[0] for x in bounds)-7, min(x[1] for x in bounds)-6,
               max(x[2] for x in bounds)+8, max(x[3] for x in bounds)+6)
    _add(features, 'LINE', LineString(plot.exterior.coords), 'neatline')


def _jointed_lithology(section, width):
    """Adjacent intervals use the same offset pair at a shared MD boundary."""
    marks = {}
    trajectories = {}
    for row in section['trajectory_samples']:
        trajectories.setdefault(row['hole_id'], []).append(row)
    for hole, rows in trajectories.items():
        xy = [(row['drawing_x'], row['drawing_y']) for row in rows]
        points = []
        for index, (x, y) in enumerate(xy):
            neighbours = (xy[max(0, index-1)], xy[min(len(xy)-1, index+1)])
            dx, dy = (neighbours[1][j]-neighbours[0][j] for j in (0, 1))
            length = math.hypot(dx, dy)
            if not length:
                raise BridgeError('DRILLHOLE_SECTION_COLLAPSED: no drawable local trajectory tangent')
            a, b = -dy*width/(2*length), dx*width/(2*length)
            points.append((rows[index]['md'], (x+a, y+b), (x-a, y-b)))
        marks[hole] = points
    features = section['features']
    lines = [row for row in features['LINE']['features'] if row['properties']['ROLE'] != 'interval_border']
    for index, row in enumerate(lines, 1):
        row['id'] = str(index)
    features['LINE']['features'] = lines
    for row in features['POLYGON']['features']:
        prop = row['properties']
        pairs = [item for item in marks[prop['HOLE']] if prop['MD_TOP'] <= item[0] <= prop['MD_BASE']]
        polygon = Polygon([item[1] for item in pairs] + [item[2] for item in reversed(pairs)])
        if not polygon.is_valid or polygon.area <= 0:
            raise BridgeError('DRILLHOLE_SECTION_RIBBON: projected interval contacts cross')
        row['geometry'] = mapping(polygon)
        _add(features, 'LINE', LineString(polygon.exterior.coords), 'interval_border', hole=prop['HOLE'])


def _assay_tracks(ledger, drawing, width, spacing, font, depth_scale):
    features = drawing['features']
    bases = sorted(ledger['assay_units'])
    maxima = {tag: max((row['value'] for groups in ledger['assays'].values() for row in groups.get(tag, [])), default=0.)
              for tag in bases}
    for index, hole in enumerate(ledger['holes']):
        depth = hole['survey'][-1]['md'] * 1000 / depth_scale
        selected = ledger['assays'].get(hole['id'], {})
        for j, tag in enumerate(bases):
            start = index*spacing + width + 48 + j*32
            _add(features, 'LINE', LineString(box(start, -depth, start+24, 0).exterior.coords), 'assay_track', hole=hole['id'])
            _add(features, 'POINT', Point(start, 4+font), 'assay_heading', hole=hole['id'],
                 text=f'{tag} ({ledger["assay_units"][tag]})', font=font)
            _add(features, 'POINT', Point(start, 1.5), 'assay_scale', hole=hole['id'],
                 text=f'0 - {maxima[tag]:g}', font=font*.8)
            groups = []
            for row in selected.get(tag, []):
                x = start + 24*row['value']/maxima[tag] if maxima[tag] else start
                segment = [(x, -row[key]*1000/depth_scale) for key in ('top', 'base')]
                if groups and math.isclose(groups[-1][-1][1], segment[0][1], rel_tol=0, abs_tol=1e-8):
                    groups[-1].extend(segment)
                else:
                    groups.append(segment)
            for line in groups:
                _add(features, 'LINE', LineString(line), 'assay:'+tag, color=3+2*j, hole=hole['id'])
    return maxima


def build_section_spec(ledger, parameters):
    required = {'title', 'section', 'horizontal_scale', 'vertical_exaggeration', 'lithology_styles'}
    optional = {'column_scale', 'reference_elevation', 'sample_step_md', 'tick_interval_metres', 'attitudes'}
    if not isinstance(parameters, dict) or not required <= set(parameters) <= required | optional:
        raise BridgeError('DRILLHOLE_SECTION_PARAMETERS')
    horizontal, exaggeration = (parameters[k] for k in ('horizontal_scale', 'vertical_exaggeration'))
    if any(not _number(v) or v <= 0 for v in (horizontal, exaggeration)):
        raise BridgeError('DRILLHOLE_SECTION_PARAMETERS: positive scales required')
    vertical, width, font = horizontal/exaggeration, 8, 2.5
    depth_scale = parameters.get('column_scale', vertical)
    reference = parameters.get('reference_elevation', max(h['collar']['elevation'] for h in ledger['holes']))
    spacing = width+55+len(ledger['assay_units'])*32
    data = {'holes': ledger['holes'], 'coordinate_frame': ledger['coordinate_frame']}
    homogenous = {'column_width_mm': width, 'font_mm': font,
                   'tick_interval_metres': parameters.get('tick_interval_metres', 20),
                   'lithology_styles': parameters['lithology_styles']}
    section = borehole_chart_spec(data, {**homogenous, 'mode': 'section', 'title': parameters['title'],
        'vertical_scale': vertical, 'horizontal_scale': horizontal, 'reference_elevation': reference,
        'sample_step_md': parameters.get('sample_step_md', 5), 'section': parameters['section']})
    _jointed_lithology(section, width)
    columns = borehole_chart_spec(data, {**homogenous, 'mode': 'columns', 'title': parameters['title']+' · 钻孔柱状图',
                                      'vertical_scale': depth_scale, 'column_spacing_mm': spacing})
    columns['assay_maxima'] = _assay_tracks(ledger, columns, width, spacing, font, depth_scale)
    start, end = (parameters['section'][field] for field in ('start', 'end'))
    azimuth = math.degrees(math.atan2(end[0]-start[0], end[1]-start[1])) % 360
    attitudes = [section_dip(row['dip_direction'], row['dip'], azimuth, exaggeration)
                 for row in parameters.get('attitudes', [])]
    for report in (section, columns):
        _frame_plot(report['features'], font)
    return {'section': section, 'columns': columns, 'attitudes': attitudes, 'azimuth': azimuth,
            'source_tables': ledger['tables'], 'source_fingerprints': ledger['sources']}


def _write_exchange(spec, destination):
    destination.mkdir()
    workbook = {}
    for name, rows in spec['source_tables'].items():
        header = list(REQUIRED[name]) + (['description'] if name == 'lithology' else [])
        workbook[name] = [header, *[[row.get(field, '') for field in header] for row in rows]]
    samples = spec['section']['trajectory_samples']
    header = ['hole_id', 'md', 'true_vertical_depth', 'easting', 'northing', 'elevation',
              'section_distance', 'crossline_offset', 'drawing_x', 'drawing_y']
    workbook['projected_section'] = [header, *[[row[key] for key in header] for row in samples]]
    for name, rows in workbook.items():
        with (destination/(name+'.csv')).open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows(rows)
    write_literal_workbook(destination/'drillhole-ledger.xlsx', workbook)


def prepare_drillhole_assets(bridge, spec, request_id):
    """Publish the two native projects and their copied siblings as one bundle."""
    request_id = bridge.request_id(request_id)
    content = json_text(spec).encode('utf-8')
    signature = sha(content)
    stage, previous, fresh = begin_bundle(bridge, {'operation': 'section.drillhole_workflow', 'spec_sha256': signature},
                                           request_id, spec['source_fingerprints'], 'drillhole-workflow.json')
    if not fresh:
        return previous
    try:
        files = stage/'files'; files.mkdir()
        result = {'schema': 'mapgis-agent/drillhole-workflow/1', 'spec_sha256': signature,
                  'section_azimuth_degrees': spec['azimuth'], 'attitudes': spec['attitudes'], 'projects': {},
                  'exchange': {'tables': 'exchange/drillhole-ledger.xlsx', 'native_section_project': 'section/section.mpj'}}
        frame = {'kind': 'local', 'name': 'Drillhole drawing plane', 'unit': 'millimetre',
                 'axis_order': 'xy', 'reference_sha256': signature}
        key = 'geo-' + sha(request_id.encode())[:18]
        for mode in ('section', 'columns'):
            layers = []
            for kind, suffix in VECTOR_TYPES.items():
                identifier = key+'-'+mode+suffix[1:]
                prepare_creation(bridge, spec[mode]['features'][kind], {'name': mode+suffix, 'crs': frame,
                                 'map_scale': 1}, identifier)
                output = bridge.apply(identifier)
                layers.append({'file': output['output'], 'expected_sha256': output['candidate_sha256']})
            project_id = key+'-'+mode
            name = mode+'.mpj'
            prepare_native_project(bridge, layers, {'name': name, 'coordinate_frame': 'native'}, project_id)
            native_project = Path(bridge.apply(project_id)['output'])
            dest = files/mode
            shutil.copytree(native_project.parent, dest)
            independent = native_project_info(bridge.root, str(dest/name), file_sha(dest/name))
            reopened = []
            for layer in independent['layers']:
                layer_path = Path(layer['file'])
                if layer_path.parent != dest:
                    raise BridgeError('DRILLHOLE_PROJECT_MEMBER_OUTSIDE_BUNDLE')
                info = native_file_info(bridge.root, str(layer_path), layer['sha256'])
                reopened.append({**info, 'file': f'{mode}/{layer_path.name}'})
            result['projects'][mode] = {'file': f'{mode}/{name}', 'sha256': file_sha(dest/name),
                'fresh_project_reopen': independent['native_execution'], 'layers': reopened,
                'drawing': spec[mode]['drawing_contract'], 'unlogged_intervals': spec[mode]['unlogged_intervals'],
                'assay_maxima': spec[mode].get('assay_maxima', {})}
        _write_exchange(spec, files/'exchange')
        (files/'drillhole-workflow.json').write_text(json_text(result), encoding='utf-8')
        return finish_bundle(bridge, request_id, files, {'native_projects': sorted(result['projects']),
                             'sources_preserved': True, 'spec_sha256': signature})
    except Exception as error:
        fail_bundle(bridge, request_id, error)
        raise


def prepare_drillhole_workflow(bridge, sources, coordinate_frame, parameters, request_id):
    ledger = read_drillhole_ledger(sources, coordinate_frame)
    return prepare_drillhole_assets(bridge, build_section_spec(ledger, parameters), request_id)
