"""Check native WP region walks against the pinned reader's arc ownership.

The existing mapgis2shp reader supplies geometry and topology decoding. This
checks the separate native drawing walk, which a geometry-only read can miss.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
import struct

from pymapgis.reader import _read_topology_table, _read_lines, _read_line_params, _read_crs
from pymapgis import reader as native

from .core import BridgeError, native_ids, read_native, sha, scalar
from .attributes import _attribute_table
from .attribute_codec import decode as decode_attribute


def _wp_arc_records(file: str | Path):
    blob, reader, sections = read_native(Path(file))
    if reader.shapeType != 'POLYGON':
        raise BridgeError('TOPOLOGY: WP required')
    _, factor = _read_crs(io.BytesIO(blob))
    geometry = _read_lines(io.BytesIO(blob), *sections[0], *sections[1], factor)
    topology = _read_topology_table(io.BytesIO(blob), *sections[3])
    parameters = _read_line_params(io.BytesIO(blob), *sections[0])
    records = {}
    if len(geometry) != len(topology):
        raise BridgeError('TOPOLOGY: arc/owner count differs')
    for aid, (line, row) in enumerate(zip(geometry, topology), 1):
        if not blob[sections[0][0] + aid * 57]:
            continue
        records[aid] = {'arc_id': aid, 'owner_region_ids': sorted({int(v) for v in row[2:4] if v}),
            'node_ids': [int(v) for v in row[:2]], 'coordinates': [list(p) for p in line.coords],
            'style': {k: scalar(v) for k, v in parameters.iloc[aid - 1].items()
                      if not isinstance(scalar(v), float) or math.isfinite(scalar(v))}}
    return blob, reader, sections, records


def _wp_nodes(records:dict)->dict:
    """WP node identities come from the arc topology, not the network table."""
    nodes={}
    for aid,arc in records.items():
        if arc['node_ids']==[0,0] and arc['coordinates'][0]==arc['coordinates'][-1]:continue
        for identity,index in zip(arc['node_ids'],(0,-1)):
            if identity<1:raise BridgeError('NODE_TOPOLOGY: active arc has no endpoint node identity')
            point=arc['coordinates'][index]
            node=nodes.setdefault(identity,{'node_id':identity,'coordinates':list(point),
                                          'incident_arc_ids':set(),'owner_region_ids':set()})
            if any(not math.isclose(a,b,rel_tol=0,abs_tol=1e-7) for a,b in zip(point,node['coordinates'])):
                raise BridgeError('NODE_TOPOLOGY: incident arc endpoints disagree')
            node['incident_arc_ids'].add(aid);node['owner_region_ids'].update(arc['owner_region_ids'])
    for node in nodes.values():
        for key in ('incident_arc_ids','owner_region_ids'):node[key]=sorted(node[key])
    return nodes


def read_wp_arcs(file: str, expected_sha256: str, offset: int = 0, limit: int = 20,
                 arc_ids: list[int] | None = None) -> dict:
    """Read saved WP arc identity, ownership and geometry in source map units."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise BridgeError('TOPOLOGY_PAGE: offset >= 0, limit 1..100')
    blob, _, sections, records = _wp_arc_records(file)
    if sha(blob) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    topology = validate_wp_topology(file)
    ids = sorted(records)
    if arc_ids is not None:
        if (not isinstance(arc_ids, list) or not arc_ids or len(arc_ids) != len(set(arc_ids))
                or any(type(v) is not int or v not in records for v in arc_ids)):
            raise BridgeError('TOPOLOGY_ARC_IDS: unique active arc IDs required')
        ids = sorted(arc_ids)
    if Path(file).read_bytes() != blob:
        raise BridgeError('STALE_DURING_TOPOLOGY_READ')
    selected = ids[offset:offset + limit]
    selected_nodes={nid for aid in selected for nid in records[aid]['node_ids']}
    try:
        nodes=_wp_nodes(records);node_status='VALIDATED_ENDPOINT_IDENTITIES'
    except BridgeError as exc:
        nodes={};node_status=str(exc)
    attribute_tables={}
    for kind,index,rows in [('arc',2,records),('node',5,nodes)]:
        table=_attribute_table(blob,sections,index)
        attribute_tables[kind]={'fields':[f.name for f in table['fields']] if table else [],
                               'stored_rows':table['count']-1 if table else 0,'identity':'physical '+kind+' ID'}
        for identity,row in rows.items():
            data=table['data']+identity*table['width'] if table else 0
            exists=table and identity<table['count'] and blob[data]==1
            row['attributes']={f.name:scalar(decode_attribute(blob[data:data+table['width']],f)) for f in table['fields']} if exists else None
    return {'file': str(Path(file).resolve()), 'source_sha256': expected_sha256, 'topology': topology,
        'arcs': [records[aid] for aid in selected], 'total': len(ids),
        'nodes':[nodes[nid] for nid in sorted(selected_nodes) if nid in nodes],'node_topology':node_status,
        'attribute_tables':attribute_tables,'unassigned_closed_arc_ids':[aid for aid in selected if records[aid]['node_ids']==[0,0]],
        'node_scope':'nodes touching the returned arcs; incident_arc_ids and owner_region_ids cover the whole file',
        'next_offset': offset + len(selected) if offset + len(selected) < len(ids) else None,
        'coordinate_frame': 'source map units; raw drawing scale already applied',
        'identity': 'arc_id is independent of region physical IDs and attribute ID values'}


def validate_wp_topology(file: str | Path) -> dict:
    try:
        blob, reader, sections = read_native(Path(file))
        if reader.shapeType != 'POLYGON':
            raise BridgeError('TOPOLOGY: WP required')
        arc_start, arc_bytes = sections[0]
        coord_start, coord_bytes = sections[1]
        param_start, param_bytes = sections[8]
        if arc_bytes % 57 or param_bytes % 40 or sections[3][1] != arc_bytes // 57 * 24:
            raise BridgeError('TOPOLOGY: inconsistent native record tables')
        active = {rid for rid in range(1,param_bytes//40) if blob[param_start+rid*40] == 1}
        if sorted(active) != native_ids(reader):
            raise BridgeError('TOPOLOGY: active region flags and arc ownership differ')
        topology = _read_topology_table(io.BytesIO(blob),*sections[3])
        arcs, owned = {}, {rid:set() for rid in active}
        shared, orphan = 0, 0
        for aid, row in enumerate(topology,1):
            flag = blob[arc_start+aid*57]
            owners = {int(pid) for pid in row[2:4] if pid}
            if not owners <= active or flag not in (0,1) or owners and not flag:
                raise BridgeError(f'TOPOLOGY: dangling or inactive arc {aid}')
            if not flag:
                continue
            count, offset = struct.unpack_from('<2i',blob,arc_start+aid*57+10)
            if count < 2 or offset < 0 or offset + count*16 > coord_bytes:
                raise BridgeError(f'TOPOLOGY: coordinate pool for arc {aid}')
            points = [struct.unpack_from('<2d',blob,coord_start+offset+i*16) for i in range(count)]
            if not all(math.isfinite(n) for p in points for n in p):
                raise BridgeError('TOPOLOGY: nonfinite coordinate')
            arcs[aid] = points
            for rid in owners:
                owned[rid].add(aid)
            shared += len(owners)==2
            orphan += not owners
        rings = 0
        same = lambda a,b: all(math.isclose(x,y,rel_tol=0,abs_tol=1e-7) for x,y in zip(a,b))
        for rid in sorted(active):
            count, offset = struct.unpack_from('<2i',blob,param_start+rid*40+1)
            if count < 2 or offset < 0 or offset + count*4 > coord_bytes:
                raise BridgeError(f'TOPOLOGY: native walk for region {rid}')
            walk = struct.unpack_from(f'<{count}i',blob,coord_start+offset)
            used, ring, vertices, region_rings = set(), [], 0, 0
            for signed in (*walk[1:],0):
                if not signed:
                    if not ring or not same(ring[0],ring[-1]):
                        raise BridgeError(f'TOPOLOGY: open or empty ring in region {rid}')
                    region_rings += 1
                    ring = []
                    continue
                aid = abs(signed)
                if aid not in arcs or aid not in owned[rid]:
                    raise BridgeError(f'TOPOLOGY: invalid arc {aid} in region {rid}')
                points = arcs[aid] if signed>0 else arcs[aid][::-1]
                if ring and not same(ring[-1],points[0]):
                    raise BridgeError(f'TOPOLOGY: disconnected walk in region {rid}')
                ring.extend(points)
                vertices += len(points)
                used.add(aid)
            if used != owned[rid] or walk[0] != vertices+region_rings:
                raise BridgeError(f'TOPOLOGY: walk and ownership differ in region {rid}')
            rings += region_rings
        if any(g.is_empty or not g.is_valid for g in reader.geom):
            raise BridgeError('TOPOLOGY: invalid polygon geometry')
        return {'status':'PASS','regions':len(active),'active_arcs':len(arcs),
                'shared_arcs':shared,'orphan_arcs':orphan,'rings':rings,
                'checks':['active owners','native signed walks','closed rings','geometry validity']}
    except BridgeError:
        raise  # Keep the exact topology finding; BridgeError subclasses ValueError.
    except (ValueError,IndexError,KeyError,struct.error) as exc:
        raise BridgeError('TOPOLOGY: invalid WP structure') from exc
