"""Check native WP region walks against the pinned reader's arc ownership.

The existing mapgis2shp reader supplies geometry and topology decoding. This
checks the separate native drawing walk, which a geometry-only read can miss.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
import struct

from pymapgis.reader import _read_topology_table

from .core import BridgeError, native_ids, read_native


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
