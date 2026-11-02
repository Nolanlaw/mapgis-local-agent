"""Saved-TIN facet intersections feeding the original BAS60 XYZ-line renderer."""
from __future__ import annotations
import math
from types import SimpleNamespace
from pydantic import ConfigDict,Field
from .core import BridgeError,read_native,native_ids
from .scene_lines import LineOverlayInput,line_profile
from .tin_profile import _section

class TinLineOverlayInput(LineOverlayInput):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    height_offset_m:float=Field(default=0.,ge=-1_000_000,le=1_000_000)

def _transport(file,frame,bounds,xyz,faces,edges,height_offset_m,source_crs=None):
    if not math.isfinite(height_offset_m):raise BridgeError('TIN_LINE_HEIGHT_OFFSET')
    profile=line_profile(file,frame,{'bounds':bounds},source_crs)
    _,reader,_=read_native(file);origin=bounds[:2];records=[];vertices=0;counts=[]
    for identity,geometry in zip(native_ids(reader),reader.geom):
        path=[list(v) for v in geometry.coords]
        # Existing section code inserts every source vertex and every exact
        # triangle-edge crossing. No extra regular stations are needed here.
        parameters=SimpleNamespace(path=path,spacing=geometry.length+1.,outside='error')
        rows,_=_section(xyz,faces,edges,parameters)
        vertices+=len(rows)
        if vertices>200000:raise BridgeError('TIN_LINE_SAMPLED_VERTEX_BOUND')
        points=[[r['x']-origin[0],r['y']-origin[1],r['z']+height_offset_m] for r in rows]
        records.append(str(identity)+' '+str(len(points))+'\n'+''.join(' '.join(format(v,'.17g') for v in point)+'\n' for point in points))
        counts.append({'record_id':identity,'vertices':len(points)})
    blob=(f'MGTINWL1 {len(records)}\n'+''.join(records)).encode('ascii')
    description=profile|{'drape':'Saved original TIN faces; GEOS facet crossings and NumPy barycentric XYZ, original BAS60/Tin3DView draw',
        'height_offset_m':height_offset_m,'display_shift':origin,'sampled_vertices':vertices,'records':counts,
        'retriangulated':False,'native_xyz':'lines.xyz'}
    return blob,description
