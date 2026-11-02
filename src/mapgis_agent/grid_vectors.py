"""ContourPy isolines/isobands from an existing GRD, without reinterpolation."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import contourpy
from shapely.geometry import LineString,MultiPoint,Polygon,mapping
from shapely.ops import unary_union

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,read_native,require_native_crs,sha
from .creation import prepare_creation
from .native_terrain import read_grid
from .raster import explicit_crs


def _grid_sources(path,digest,frame):
    if sha(path.read_bytes())!=digest:raise BridgeError('STALE_SOURCE')
    target=explicit_crs(frame)
    sources=[{'file':str(path),'sha256':digest}]
    sidecar=Path(str(path)+'.json')
    if sidecar.exists():
        raw=sidecar.read_bytes()
        try:metadata=json.loads(raw)
        except (ValueError,UnicodeDecodeError) as exc:raise BridgeError('GRID_SIDECAR: invalid metadata') from exc
        if metadata.get('schema')!='mapgis-agent/grid/1' or metadata.get('grid_sha256')!=digest:
            raise BridgeError('GRID_SIDECAR: metadata is not bound to this grid')
        original=explicit_crs(metadata.get('crs',{}))
        if ((original is None)!=(target is None) or original is not None and not original.equals(target)
                or original is None and metadata['crs']!=frame):raise BridgeError('GRID_CRS_MISMATCH')
        sources.append({'file':str(sidecar),'sha256':sha(raw)})
    return sources


def _parts(geometry,kind):
    if geometry.is_empty:return
    if geometry.geom_type==kind:
        yield geometry
    elif hasattr(geometry,'geoms'):
        for part in geometry.geoms:yield from _parts(part,kind)


def prepare_grid_vectors(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str):
    operation=parameters.get('operation')
    allowed={'operation','levels','mask'}|({'colors'} if operation=='bands' else set())
    required=allowed-{'mask'}
    if operation not in {'contours','bands'} or not required<=set(parameters)<=allowed:
        raise BridgeError('UNEXPECTED_PARAMETERS')
    name=template.get('name','')
    if Path(name).suffix.lower()!=('.wl' if operation=='contours' else '.wp') or Path(name).name!=name:
        raise BridgeError('INVALID_TEMPLATE_NAME')
    levels=parameters['levels']
    if (not isinstance(levels,list) or len(levels)<(2 if operation=='bands' else 1)
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in levels)
            or any(a>=b for a,b in zip(levels,levels[1:]))):raise BridgeError('GRID_LEVELS: explicit increasing finite levels required')
    colors=parameters.get('colors',[])
    if operation=='bands' and (len(colors)!=len(levels)-1 or any(type(v) is not int or not 0<=v<=65535 for v in colors)):
        raise BridgeError('GRID_COLORS: one native color index per interval required')
    path=Path(file).resolve(strict=True)
    sources=_grid_sources(path,expected_sha256,template.get('crs',{}))
    xs,ys,z,grid_info=read_grid(path)
    mask=None
    if 'mask' in parameters:
        spec=parameters['mask']
        if not isinstance(spec,dict) or set(spec)!={'file','expected_sha256','mode'}:raise BridgeError('GRID_MASK')
        source=Path(spec['file']).resolve(strict=True)
        blob,reader,_=read_native(source)
        if sha(blob)!=spec['expected_sha256']:raise BridgeError('STALE_SOURCE')
        require_native_crs(reader,template['crs'],'GRID_MASK_CRS_MISMATCH')
        if spec['mode']=='convex_hull' and reader.shapeType=='POINT':mask=MultiPoint(reader.geom).convex_hull
        elif spec['mode']=='geometry' and reader.shapeType=='POLYGON':mask=unary_union(reader.geom)
        else:raise BridgeError('GRID_MASK: convex_hull WT or geometry WP required')
        if mask.is_empty or mask.area<=0:raise BridgeError('GRID_MASK_EMPTY')
        sources.append({'file':str(source),'sha256':spec['expected_sha256']})
    payload={'operation':'grid.'+operation,'file':str(path),'expected_sha256':expected_sha256,
             'parameters':parameters,'template':template,'sources':sources}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        generator=contourpy.contour_generator(x=xs,y=ys,z=z,fill_type='OuterOffset',line_type='Separate')
        features=[];empty=[];areas=[]
        def add(geometry,properties,style):
            if mask is not None:geometry=geometry.intersection(mask)
            for part in _parts(geometry,'LineString' if operation=='contours' else 'Polygon'):
                if part.is_empty or not part.is_valid:raise BridgeError('GRID_VECTOR_INVALID_GEOMETRY')
                if (part.length if operation=='contours' else part.area)<=0:continue
                features.append({'type':'Feature','id':str(len(features)+1),'geometry':mapping(part),
                                 'properties':properties,'style':style})
        if operation=='contours':
            for level in levels:
                before=len(features)
                for line in generator.lines(level):
                    if len(line)>=2:add(LineString(line),{'level':level},{'color':0,'width':.3,'linetype':1})
                if len(features)==before:empty.append(level)
        else:
            for index,(low,high,color) in enumerate(zip(levels[:-1],levels[1:],colors),1):
                before=len(features)
                vertices,offsets=generator.filled(low,high)
                for vertex,offset in zip(vertices,offsets):
                    rings=[vertex[a:b] for a,b in zip(offset[:-1],offset[1:])]
                    if rings:add(Polygon(rings[0],rings[1:]),{'class_index':index,'lower':low,'upper':high},
                                 {'fill_color':color,'pattern':0})
                area=sum(Polygon(f['geometry']['coordinates'][0],f['geometry']['coordinates'][1:]).area for f in features[before:])
                areas.append({'class_index':index,'lower':low,'upper':high,'area':area,'parts':len(features)-before})
                if len(features)==before:empty.append(index)
        if not features:raise BridgeError('GRID_EMPTY_RESULT: no geometry at the requested levels')
        # Reuse the existing native encoder and its geometry/attribute/style readback.
        encoder=Bridge(stage/'encoder')
        qa={'backend':'GDAL/ContourPy/GEOS','reinterpolated':False,'grid_shape':grid_info['shape'],
            'features':len(features),'empty_levels':empty if operation=='contours' else [],'bands':areas}
        prepare_creation(encoder,{'type':'FeatureCollection','features':features},
                         template|{'provenance':{'input_sha256':expected_sha256,**qa}},'encode')
        encoded=encoder.apply('encode')
        files=stage/'files';files.mkdir()
        shutil.copyfile(encoded['output'],files/name)
        (files/(name+'.json')).write_text(json_text({'schema':'mapgis-agent/grid-vectors/1','sources':sources,
            'parameters':parameters,'crs':template['crs'],'metrics':qa,'native_sha256':sha((files/name).read_bytes())}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'GDAL/ContourPy/GEOS','metrics':qa,'source_preserved':True})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc)
        raise
