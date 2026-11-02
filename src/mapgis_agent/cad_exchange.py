"""DXF interoperability via ezdxf; native metadata travels in a bound sidecar.

Coordinates, entity geometry and text are read back. Proprietary MapGIS symbols
and line patterns are not guessed as CAD graphics. No DWG converter is implied.
"""
from __future__ import annotations
import copy,json,math,re,shutil,struct
from collections import Counter
from itertools import islice
from pathlib import Path
from typing import Literal
import ezdxf
from ezdxf.path import make_path,from_hatch
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictFloat,StrictInt
from pyproj import CRS
from shapely.affinity import scale
from shapely.geometry import Point,LineString,Polygon,mapping,shape
from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,read_native
from .creation import PARAMS,REGION_INDEX_FIELDS,prepare_creation
from .cad_text import FontBinding,TEXT_TYPES,load_fonts,outline_entities
from .features import _native_collection
from .raster import explicit_crs,file_sha

UNITS={'unitless':(0,None),'inch':(1,.0254),'foot':(2,.3048),'millimetre':(4,.001),'centimetre':(5,.01),'metre':(6,1.)}
Unit=Literal['unitless','inch','foot','millimetre','centimetre','metre']
KINDS={'POINT':'point','TEXT':'point','MTEXT':'point','LINE':'line','LWPOLYLINE':'line','POLYLINE':'line','ARC':'line','CIRCLE':'line','ELLIPSE':'line','SPLINE':'line','HATCH':'polygon'}

class DxfSource(BaseModel):
    model_config=ConfigDict(extra='forbid')
    file:str
    expected_sha256:str
    layer:str|None=None

class DxfExportParameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    crs:dict
    units:Unit
    colors:dict[str,list[StrictInt]]=Field(default_factory=dict)
    font:str='simsun.ttf'

class DxfImportParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    kind:Literal['point','line','polygon']
    layers:list[str]=Field(min_length=1,max_length=1000)
    source_units:Unit
    curve_tolerance:StrictFloat|None=Field(default=None,gt=0)
    restore_bound_metadata:bool=True
    layout:str=Field(default='Model',min_length=1)
    expand_blocks:StrictBool=False
    paper_coordinate_units_confirmed:StrictBool=False
    max_expanded_entities:StrictInt=Field(default=200000,ge=1,le=200000)
    max_block_depth:StrictInt=Field(default=16,ge=1,le=32)
    apply_block_clipping:StrictBool=False
    compose_viewports:StrictBool=False
    viewport_model_layers:list[str]|None=Field(default=None,min_length=1,max_length=1000)
    text_mode:Literal['native','outline']='native'
    font_bindings:list[FontBinding]|None=Field(default=None,min_length=1,max_length=100)


def _layout(doc,name):
    try:layout=doc.layouts.get(name)
    except KeyError as exc:raise BridgeError('DXF_LAYOUT: select an existing exact layout name') from exc
    if len(layout)>200000:raise BridgeError('DXF_ENTITY_LIMIT: selected layout exceeds200000rootentities')
    return layout


def _layout_entities(layout,p):
    """Root-layer selection, raw block traversal and copied leaf transforms.

    Compose matrices before transforming leaves so that a nested INSERT need
    not represent a sheared coordinate system and layer0 inheritance is kept.
    This avoids the silent-skipping and nested-context loss of generic explode.
    """
    from ezdxf import transform as cad_transform
    from ezdxf.math import Matrix44
    from ezdxf.xclip import XClip
    emitted=0;root_counts=Counter();excluded=Counter();doc=layout.doc
    fonts=load_fonts(p.font_bindings) if p.text_mode=='outline' else None;text_budget=[0]
    stats={'root_entities':root_counts,'excluded_nonselected_block_entities':excluded,'expanded_entities':0}
    clipping={'active_block_clips':0,'disabled_block_clips':0,'fully_clipped_entities':0,'hidden_entities':0,
        'split_entities':0,'discarded_lower_dimension_parts':0,'viewports':[]}
    stats['clipping']=clipping

    def visible(entity,layer,frozen):
        if frozen is None:return True
        try:entry=doc.layers.get(layer)
        except ezdxf.DXFTableEntryError:entry=None
        hidden=(entity.dxf.get('invisible',0) or layer.casefold() in frozen
            or entry is not None and (entry.is_off() or entry.is_frozen() or not entry.dxf.get('plot',1)))
        if hidden:clipping['hidden_entities']+=1
        return not hidden

    def block_clip(reference,matrix,inherited):
        xclip=XClip(reference)
        if not xclip.has_clipping_path:return inherited
        if not xclip.is_clipping_enabled:
            clipping['disabled_block_clips']+=1;return inherited
        if not p.apply_block_clipping:raise BridgeError('DXF_XCLIP: explicitly request apply_block_clipping=true')
        spatial=xclip.get_spatial_filter();dxf=spatial.dxf
        if (dxf.has_front_clipping_plane or dxf.has_back_clipping_plane
            or tuple(dxf.origin)!=(0.,0.,0.) or tuple(dxf.extrusion)!=(0.,0.,1.)
            or tuple(spatial.transform_matrix)!=tuple(Matrix44())):
            raise BridgeError('DXF_XCLIP_PROFILE: depth/OCS/additional filter transformations require separately verified boundary decoding')
        path=xclip.get_block_clipping_path()
        if path.is_inverted_clip:raise BridgeError('DXF_XCLIP_INVERTED: original inverted-boundary semantics need separate verified input')
        polygon=_clip_polygon(_points(matrix.transform_vertices(path.vertices)))
        clipping['active_block_clips']+=1
        return polygon if inherited is None else inherited.intersection(polygon)

    def preflight(reference,stack=()):
        name=reference.dxf.name.casefold()
        if name in stack:raise BridgeError('DXF_BLOCK_CYCLE: recursive block references are not expanded')
        if len(stack)>=p.max_block_depth:raise BridgeError('DXF_BLOCK_DEPTH')
        block=reference.block()
        if block is None:raise BridgeError('DXF_BLOCK_MISSING: '+reference.dxf.name)
        if block.block.dxf.flags&12:raise BridgeError('DXF_XREF: external block references require a separate verified materialization')
        xclip=XClip(reference)
        if xclip.has_clipping_path and xclip.is_clipping_enabled and not p.apply_block_clipping:
            raise BridgeError('DXF_XCLIP: an active block clipping path must not be silently ignored')
        count=0
        for entity in block:
            count+=preflight(entity,stack+(name,)) if entity.dxftype()=='INSERT' else 1
            if count*reference.mcount>p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT')
        return count*reference.mcount

    def leaf(entity,matrix,effective_layer,root_handle,path,clip=None,context=None,frozen=None,source_handle=None):
        nonlocal emitted
        if not visible(entity,effective_layer,frozen):return
        kind=entity.dxftype()
        if p.text_mode=='outline' and kind in TEXT_TYPES:
            if kind=='ATTDEF' and not entity.is_const:excluded[kind]+=1;return
            if kind in {'ATTRIB','ATTDEF'} and entity.is_invisible:excluded[kind]+=1;return
            for part,(converted,details) in enumerate(outline_entities(entity,matrix,p.kind,fonts,p.curve_tolerance,text_budget)):
                emitted+=1
                if emitted>p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT')
                stats['expanded_entities']=emitted
                yield converted,{'root_handle':root_handle,'origin_handle':source_handle or entity.dxf.handle,
                    'virtual_key':'/'.join(path)+f':text{part}','effective_layer':effective_layer,
                    'source_type':kind,**(context or {}),**details},clip
            return
        if kind=='ATTDEF':
            if p.kind=='point':raise BridgeError('DXF_BLOCK_ATTRIBUTES: constant/variable attribute definitions require an explicit text workflow')
            excluded[kind]+=1;return
        if kind not in KINDS:raise BridgeError('DXF_UNSUPPORTED_ENTITY: '+kind+' in selected block/layout')
        if KINDS[kind]!=p.kind:
            excluded[kind]+=1;return
        if kind in {'TEXT','MTEXT'}:
            a=matrix.transform_direction((1,0,0));b=matrix.transform_direction((0,1,0))
            if (not math.isclose(a.magnitude,b.magnitude,rel_tol=1e-12,abs_tol=0)
                or abs(a.dot(b))>a.magnitude*b.magnitude*1e-12 or a.x*b.y-a.y*b.x<0):
                raise BridgeError('DXF_BLOCK_TEXT_TRANSFORM: nonuniform/sheared/mirrored text needs an explicit verified outline/native text conversion')
        log,entities=cad_transform.copies([entity],matrix)
        if len(log) or not entities:raise BridgeError('DXF_BLOCK_TRANSFORM: '+ '; '.join(log.messages()))
        for part,converted in enumerate(entities):
            emitted+=1
            if emitted>p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT')
            stats['expanded_entities']=emitted
            yield converted,{'root_handle':root_handle,'origin_handle':entity.dxf.handle,
                'virtual_key':'/'.join(path)+(f':part{part}' if len(entities)>1 else ''),
                'effective_layer':effective_layer,'source_type':kind,**(context or {})},clip

    def walk(reference,parent,inherited_layer,root_handle,path,stack=(),clip=None,context=None,frozen=None):
        if not visible(reference,inherited_layer,frozen):return
        block=reference.block();name=reference.dxf.name.casefold()
        if name in stack:raise BridgeError('DXF_BLOCK_CYCLE')
        instances=reference.multi_insert() if reference.mcount>1 else [reference]
        for cell,instance in enumerate(instances):
            current=Matrix44.chain(instance.matrix44(),parent)
            instance_path=path+(f'{reference.dxf.handle}:cell{cell}',)
            current_clip=block_clip(instance,current,clip)
            attached_tags={attribute.dxf.tag.casefold() for attribute in instance.attribs}
            for entity in block:
                layer=entity.dxf.layer if entity.dxf.layer!='0' else inherited_layer
                if p.text_mode=='outline' and entity.dxftype()=='ATTDEF' and entity.dxf.tag.casefold() in attached_tags:
                    excluded['ATTDEF_replaced_by_attached_value']+=1;continue
                if entity.dxftype()=='INSERT':
                    yield from walk(entity,current,layer,root_handle,instance_path,stack+(name,),current_clip,context,frozen)
                else:
                    yield from leaf(entity,current,layer,root_handle,instance_path+(entity.dxf.handle,),current_clip,context,frozen)
            if instance.attribs:
                if p.text_mode=='outline':
                    for index,attribute in enumerate(instance.attribs):
                        source=reference.attribs[index];layer=attribute.dxf.layer if attribute.dxf.layer!='0' else inherited_layer
                        # Attached ATTRIB positions are already in the owning layout,
                        # unlike the block-local ATTDEF; apply only the parent matrix.
                        yield from leaf(attribute,parent,layer,root_handle,instance_path+(f'ATTRIB:{source.dxf.handle}',),
                            current_clip,context,frozen,source.dxf.handle)
                else:
                    if p.kind=='point':raise BridgeError('DXF_BLOCK_ATTRIBUTES: attached attribute text requires an explicit verified text workflow')
                    excluded['ATTRIB']+=len(instance.attribs)

    def stream():
        planned=0
        for entity in layout:
            if entity.dxf.layer not in p.layers:continue
            kind=entity.dxftype();root_counts[kind]+=1
            if kind=='VIEWPORT' and p.compose_viewports:
                if entity.dxf.id==1 or entity.dxf.status==0 or entity.dxf.flags&131072:
                    clipping['viewports'].append({'handle':entity.dxf.handle,'status':'OFF_OR_PAPER_SYSTEM_VIEWPORT'});continue
                matrix,clip,description=_viewport_projection(entity,p.curve_tolerance)
                clipping['viewports'].append(description)
                frozen={s.casefold() for s in entity.frozen_layers}
                if not visible(entity,entity.dxf.layer,frozen):continue
                for model in doc.modelspace():
                    if model.dxf.layer not in p.viewport_model_layers:continue
                    if not visible(model,model.dxf.layer,frozen):continue
                    context={'viewport_handle':entity.dxf.handle}
                    prefix=('viewport:'+entity.dxf.handle,)
                    if model.dxftype()=='INSERT' and p.expand_blocks:
                        planned+=preflight(model)
                        if planned>p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT')
                        yield from walk(model,matrix,model.dxf.layer,model.dxf.handle,prefix,clip=clip,context=context,frozen=frozen)
                    else:
                        yield from leaf(model,matrix,model.dxf.layer,model.dxf.handle,prefix+(model.dxf.handle,),clip,context,frozen)
                continue
            if kind=='INSERT' and p.expand_blocks:
                planned+=preflight(entity)
                if planned>p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT')
                yield from walk(entity,Matrix44(),entity.dxf.layer,entity.dxf.handle,())
            elif kind in TEXT_TYPES and p.text_mode=='outline':
                yield from leaf(entity,Matrix44(),entity.dxf.layer,entity.dxf.handle,(entity.dxf.handle,))
            else:yield entity,None,None
    return stream(),stats


def _clip_polygon(points):
    polygon=Polygon(points)
    if polygon.is_empty or not polygon.is_valid or polygon.area<=0:
        raise BridgeError('DXF_CLIP_BOUNDARY: a finite simple nonzero-area planar polygon is required')
    return polygon


def _viewport_projection(viewport,tolerance):
    """Top WCS view only; DXF12 is a DCS centre, DXF17 a WCS target."""
    dxf=viewport.dxf;direction=tuple(dxf.view_direction_vector);target=tuple(dxf.view_target_point)
    values=(*direction,*target,*dxf.center,*dxf.view_center_point,dxf.width,dxf.height,dxf.view_height,dxf.view_twist_angle)
    if (not all(math.isfinite(v) for v in values) or dxf.status<0 or dxf.flags&(1|2|4|8|16|2048)
        or dxf.get('render_mode',0)!=0 or direction[0]!=0 or direction[1]!=0 or direction[2]<=0
        or target[2]!=0 or dxf.center.z!=0 or min(dxf.width,dxf.height,dxf.view_height)<=0):
        raise BridgeError('DXF_VIEWPORT_PROFILE: only finite active top-WCS orthographic 2D views without depth clipping/rendering are verified')
    polygon=_clip_polygon(_points(viewport.clipping_rect_corners()))
    if dxf.flags&65536:
        boundary=viewport.doc.entitydb.get(dxf.get('clipping_boundary_handle','0'))
        if boundary is None:raise BridgeError('DXF_VIEWPORT_CLIP: missing nonrectangular boundary')
        if boundary.dxf.owner!=dxf.owner:raise BridgeError('DXF_VIEWPORT_CLIP: boundary must belong to the same paper layout')
        if boundary.dxftype() not in {'LWPOLYLINE','POLYLINE','CIRCLE','ELLIPSE','SPLINE'}:
            raise BridgeError('DXF_VIEWPORT_CLIP: unsupported boundary entity')
        path=make_path(boundary)
        points=_path_points(path,tolerance)
        if not path.is_closed:raise BridgeError('DXF_VIEWPORT_CLIP: boundary must be closed')
        polygon=_clip_polygon(points)
    return viewport.get_transformation_matrix(),polygon,{'handle':dxf.handle,'status':'COMPOSED_TOP_WCS_2D',
        'model_to_paper_scale':dxf.height/dxf.view_height,'twist_degrees':dxf.view_twist_angle,
        'view_center_DCS':list(dxf.view_center_point),'target_WCS':list(target),'clip_bounds':list(polygon.bounds),
        'frozen_layers':list(viewport.frozen_layers),'rendering':'geometry and declared annotation only; CAD lineweights/fonts/viewport overrides not inferred'}


def _clipped_parts(geometry,clip,kind,stats):
    if clip is None:return [geometry]
    clipped=geometry.intersection(clip)
    expected={'point':'Point','line':'LineString','polygon':'Polygon'}[kind]
    def parts(value):
        if value.is_empty:return []
        if value.geom_type==expected:return [value]
        if hasattr(value,'geoms'):return [part for child in value.geoms for part in parts(child)]
        stats['discarded_lower_dimension_parts']+=1;return []
    result=parts(clipped)
    if not result:stats['fully_clipped_entities']+=1
    elif len(result)>1:stats['split_entities']+=1
    return sorted(result,key=lambda g:(g.bounds,g.wkb_hex)) if len(result)>1 else result


def _unit(crs):
    resolved=explicit_crs(crs)
    if resolved is None:return {'metre':1.,'millimetre':.001,'unknown':None}[crs['unit']]
    if not resolved.is_projected or len(resolved.axis_info)<2 or not math.isclose(resolved.axis_info[0].unit_conversion_factor,resolved.axis_info[1].unit_conversion_factor):raise BridgeError('DXF_PLANAR_FRAME: use projected or explicitly local linear XY')
    return resolved.axis_info[0].unit_conversion_factor


def _factor(source,target):
    if source is None or target is None:
        if source is not target:raise BridgeError('DXF_UNIT_CONTEXT: cannot infer a linear scale between unitless and known units')
        return 1.
    return source/target


def _members(path):
    if path.suffix.lower()!='.dxf':raise BridgeError('DXF_REQUIRED: DWG needs a separately verified converter')
    result={path.name:file_sha(path)};sidecar=path.with_name(path.name+'.json')
    if sidecar.is_file():
        result[sidecar.name]=file_sha(sidecar);value=json.loads(sidecar.read_text(encoding='utf8'))
        snapshots=value.get('native_snapshots',[]) if value.get('schema')=='mapgis-agent/dxf/1' else []
        if not isinstance(snapshots,list) or len(snapshots)>100:raise BridgeError('DXF_SNAPSHOT_MANIFEST')
        for snapshot in snapshots:
            name=snapshot.get('name','')
            if not re.fullmatch(r'source-\d{4}\.(wt|wl|wp)',name):raise BridgeError('DXF_SNAPSHOT_NAME')
            member=path.parent/name
            if not member.is_file() or member.is_symlink() or file_sha(member)!=snapshot['sha256']:raise BridgeError('DXF_SNAPSHOT_CHANGED')
            result[name]=snapshot['sha256']
    return result


def _document(path):
    try:doc=ezdxf.readfile(path)
    except (ezdxf.DXFError,UnicodeError) as exc:raise BridgeError('DXF_READ_FAILED: no in-place recovery or repair was performed') from exc
    if len(doc.modelspace())>200000:raise BridgeError('DXF_ENTITY_LIMIT: select a smaller drawing')
    return doc


def dxf_source_info(file,expected_sha256=None,layout='Model'):
    path=Path(file).resolve(strict=True);members=_members(path)
    if expected_sha256 is not None and members[path.name]!=expected_sha256:raise BridgeError('STALE_SOURCE')
    doc=_document(path);layers={};counts=Counter();unsupported=Counter()
    selected_layout=_layout(doc,layout)
    for e in selected_layout:
        kind=e.dxftype();counts[kind]+=1;layers.setdefault(e.dxf.layer,Counter())[kind]+=1
        if kind not in KINDS:unsupported[kind]+=1
    if _members(path)!=members:raise BridgeError('STALE_SOURCE')
    return {'file':str(path),'source_sha256':members[path.name],'members':members,'dxf_version':doc.dxfversion,'units_code':doc.units,
        'units':next((k for k,v in UNITS.items() if v[0]==doc.units),'OTHER_EXPLICIT_UNIT_REQUIRED'),
        'entities_by_type':dict(sorted(counts.items())),'layers':[{'name':k,'entities_by_type':dict(sorted(v.items()))} for k,v in sorted(layers.items())],
        'unsupported_modelspace_entities':dict(unsupported) if selected_layout.name=='Model' else None,
        'unsupported_selected_layout_entities':dict(unsupported),'paper_layouts':[v.name for v in doc.layouts if v.name!='Model'],
        'selected_layout':{'name':selected_layout.name,'is_modelspace':selected_layout.name=='Model',
            'coordinate_units':'documentINSUNITS checked on modelspace import; paper drawing units require explicit caller confirmation'},
        'block_definitions':[{'name':b.name,'entities':len(b),'flags':b.block.dxf.flags} for b in doc.blocks
            if not b.name.startswith(('*Model_Space','*Paper_Space'))],
        'backend':'ezdxf1.4.5; raw layout/root-entity inventory; explicit prepare expansion uses copied WCS leaves; no external XREF/image loading, DWG conversion or guessed georeference','source_preserved':True}


def _points(vertices):
    values=list(islice(vertices,200002))
    if len(values)>200000:raise BridgeError('DXF_VERTEX_LIMIT')
    result=[]
    for v in values:
        xyz=list(v)
        if len(xyz)<2 or not all(math.isfinite(x) for x in xyz) or len(xyz)>2 and abs(xyz[2])>1e-9:raise BridgeError('DXF_2D_REQUIRED: nonzero/nonfinite elevation must not be silently flattened')
        result.append(tuple(xyz[:2]))
    return result


def _path_points(path,tolerance):
    if path.has_curves and tolerance is None:raise BridgeError('DXF_CURVE_TOLERANCE: supply positive chord tolerance in the declared DXF source units')
    return _points(path.flattening(tolerance if tolerance is not None else 1e-8,segments=4))


def _geometry(entity,tolerance):
    kind=entity.dxftype();text=None;style=None
    if kind=='POINT':geometry=Point(_points([entity.dxf.location])[0])
    elif kind in {'TEXT','MTEXT'}:
        if kind=='TEXT' and (entity.dxf.get('halign',0) or entity.dxf.get('valign',0) or entity.dxf.get('text_generation_flag',0)):
            raise BridgeError('DXF_TEXT_ALIGNMENT: non-left-baseline or mirrored TEXT needs an explicit layout conversion')
        insert=entity.ocs().to_wcs(entity.dxf.insert) if kind=='TEXT' else entity.dxf.insert
        geometry=Point(_points([insert])[0]);text=entity.dxf.text if kind=='TEXT' else entity.plain_text()
        height=entity.dxf.height if kind=='TEXT' else entity.dxf.char_height
        if kind=='MTEXT' and entity.dxf.hasattr('text_direction'):raise BridgeError('DXF_MTEXT_DIRECTION: convert the directional layout explicitly before native annotation import')
        style={'height':height,'width':height*(entity.dxf.get('width',1.) if kind=='TEXT' else 1.),'angle':entity.dxf.get('rotation',0.),'font_zh':1,'font_en':0,'color':1}
    elif kind=='LINE':geometry=LineString(_points([entity.dxf.start,entity.dxf.end]))
    elif kind in {'ARC','CIRCLE','ELLIPSE','SPLINE'}:
        if tolerance is None:raise BridgeError('DXF_CURVE_TOLERANCE: supply positive source-unit flattening tolerance')
        geometry=LineString(_points(entity.flattening(tolerance)))
    elif kind in {'LWPOLYLINE','POLYLINE'}:
        if kind=='POLYLINE' and (entity.is_polygon_mesh or entity.is_poly_face_mesh):raise BridgeError('DXF_2D_REQUIRED')
        geometry=LineString(_path_points(make_path(entity),tolerance))
    elif kind=='HATCH':
        if entity.dxf.hatch_style!=0:raise BridgeError('DXF_HATCH_STYLE: only normal nested-boundary semantics are currently verified')
        geometry=Polygon()
        for path in from_hatch(entity):
            points=_path_points(path,tolerance)
            if len(points)<4 or points[0]!=points[-1]:raise BridgeError('DXF_HATCH_CLOSED_BOUNDARY')
            polygon=Polygon(points)
            if not polygon.is_valid or polygon.is_empty:raise BridgeError('DXF_INVALID_HATCH')
            geometry=geometry.symmetric_difference(polygon)
        if geometry.geom_type not in {'Polygon','MultiPolygon'}:raise BridgeError('DXF_INVALID_HATCH')
    else:raise BridgeError('DXF_UNSUPPORTED_ENTITY: '+kind)
    if geometry.is_empty or not geometry.is_valid:raise BridgeError('DXF_INVALID_GEOMETRY')
    return geometry,text,style


def _geometry_same(a,b):
    # HATCH ring assembly can rotate a closed ring or omit a collinear vertex.
    # Exact GEOS topological equality proves the same boundary/holes without
    # increasing any coordinate tolerance. The bound sidecar retains vertices.
    if a.equals(b):return True
    if a.geom_type in {'MultiPoint','MultiLineString','MultiPolygon'} and len(a.geoms)==1:a=a.geoms[0]
    if b.geom_type in {'MultiPoint','MultiLineString','MultiPolygon'} and len(b.geoms)==1:b=b.geoms[0]
    tolerance=max(1e-8,max(abs(v) for v in (*a.bounds,*b.bounds))*1e-14)
    return a.equals_exact(b,tolerance,normalize=True)


def prepare_dxf_export(bridge,files,parameters,request_id):
    p=parameters if isinstance(parameters,DxfExportParameters) else DxfExportParameters.model_validate(parameters)
    if Path(p.name).suffix.lower()!='.dxf':raise BridgeError('DXF_OUTPUT_NAME')
    if not 1<=len(files)<=100:raise BridgeError('DXF_SOURCE_FILES')
    unit=_unit(p.crs);factor=_factor(unit,UNITS[p.units][1]);sources=[];collections=[];used=set()
    for value in files:
        v=DxfSource.model_validate(value);collection,info=_native_collection(v.file,v.expected_sha256,None)
        layer=v.layer or Path(v.file).stem
        if not ezdxf.lldxf.validator.is_valid_layer_name(layer) or layer.casefold() in used:raise BridgeError('DXF_LAYER_NAME: provide unique valid CAD layer names')
        used.add(layer.casefold())
        if info['crs_wkt'] and (p.crs.get('kind')=='local' or not CRS(info['crs_wkt']).equals(CRS(p.crs['definition']))):raise BridgeError('DXF_COORDINATE_FRAME: native CRS and explicit source context differ')
        sources.append({'file':str(Path(v.file).resolve(strict=True)),'sha256':v.expected_sha256});collections.append((layer,collection,info))
    if sum(len(c['features']) for _,c,_ in collections)>200000:raise BridgeError('DXF_ENTITY_LIMIT')
    payload={'operation':'exchange.dxf_export','files':[DxfSource.model_validate(v).model_dump() for v in files],'parameters':p.model_dump(),'dxf_protocol':2}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,p.name)
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir();output=folder/p.name;doc=ezdxf.new('R2013');doc.units=UNITS[p.units][0]
        doc.styles.new('MAPGIS_TEXT',dxfattribs={'font':p.font});space=doc.modelspace();records={};losses=Counter();snapshots=[]
        for source_index,(layer,collection,info) in enumerate(collections,1):
            snapshot_name=f'source-{source_index:04d}'+Path(info['path']).suffix.lower()
            shutil.copyfile(info['path'],folder/snapshot_name)
            if file_sha(folder/snapshot_name)!=info['sha256']:raise BridgeError('STALE_SOURCE')
            from pymapgis.reader import _CRS_SCALE_OFFSET
            raw,_,_=read_native(folder/snapshot_name)
            saved_scale=struct.unpack_from('<d',raw,_CRS_SCALE_OFFSET)[0] if info['crs_wkt'] else None
            info={**info,'snapshot_name':snapshot_name,'native_map_scale':saved_scale}
            snapshots.append({'name':snapshot_name,'sha256':info['sha256'],'layer':layer,'map_scale':saved_scale,'physical_record_ids':info['physical_record_ids']})
            doc.layers.new(layer)
            for feature in collection['features']:
                geometry=scale(shape(feature['geometry']),xfact=factor,yfact=factor,origin=(0,0));native_style=feature['style']
                color=native_style.get('fill_color' if geometry.geom_type in {'Polygon','MultiPolygon'} else 'color');common={'layer':layer,'color':7}
                if str(color) in p.colors:
                    rgb=p.colors[str(color)]
                    if len(rgb)!=3 or any(not 0<=v<=255 for v in rgb):raise BridgeError('DXF_RGB')
                    common['true_color']=ezdxf.colors.rgb2int(rgb)
                else:losses['native_color_rendered_as_CAD_black_white']+=1
                entities=[]
                if geometry.geom_type=='Point':
                    if feature.get('text') is not None:
                        h=float(native_style['height'])*factor
                        if not math.isfinite(h) or h<=0:raise BridgeError('DXF_TEXT_HEIGHT')
                        entities=[space.add_text(feature['text'],dxfattribs={**common,'insert':geometry.coords[0],'height':h,
                            'width':float(native_style['width'])/float(native_style['height']),'rotation':float(native_style.get('angle',0.)),'style':'MAPGIS_TEXT'})]
                        losses['native_font_and_anchor_approximated_by_left_baseline_TEXT']+=1
                    else:entities=[space.add_point(geometry.coords[0],dxfattribs=common)];losses['native_symbol_rendered_as_POINT']+=1
                elif geometry.geom_type in {'LineString','MultiLineString'}:
                    parts=list(geometry.geoms) if geometry.geom_type=='MultiLineString' else [geometry]
                    for part in parts:entities.append(space.add_lwpolyline(list(part.coords),format='xy',dxfattribs=common))
                    losses['native_line_pattern_width_not_rendered']+=1
                else:
                    parts=list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry]
                    entity=space.add_hatch(color=7,dxfattribs=common)
                    for part in parts:
                        entity.paths.add_polyline_path(list(part.exterior.coords),is_closed=True,flags=1)
                        for ring in part.interiors:entity.paths.add_polyline_path(list(ring.coords),is_closed=True,flags=0)
                    entities=[entity]
                    if native_style.get('pattern',0):losses['native_fill_pattern_rendered_as_solid_HATCH']+=1
                for entity in entities:records[entity.dxf.handle]={'source':info,'feature':feature,'layer':layer,'entity_type':entity.dxftype()}
        doc.saveas(output);reopened=_document(output);expected=dict(records)
        for entity in reopened.modelspace():
            record=expected.pop(entity.dxf.handle,None)
            if record is None:raise BridgeError('DXF_ENTITY_READBACK_DIFF')
            actual,text,_=_geometry(entity,None);feature=record['feature'];desired=scale(shape(feature['geometry']),xfact=factor,yfact=factor,origin=(0,0))
            # A multipart line is represented by several CAD entities; each
            # handle binds just its actual part, retaining the source identity.
            if desired.geom_type=='MultiLineString':
                part=next((part for part in desired.geoms if _geometry_same(part,actual)),None)
                if part is None:raise BridgeError('DXF_GEOMETRY_READBACK_DIFF')
                record['feature']=copy.deepcopy(feature);record['feature']['geometry']=mapping(scale(part,xfact=1/factor,yfact=1/factor,origin=(0,0)))
            elif not _geometry_same(desired,actual):raise BridgeError('DXF_GEOMETRY_READBACK_DIFF')
            if feature.get('text')!=text:raise BridgeError('DXF_TEXT_READBACK_DIFF')
        if expected:raise BridgeError('DXF_ENTITY_READBACK_DIFF')
        sidecar={'schema':'mapgis-agent/dxf/1','dxf_sha256':file_sha(output),'crs':p.crs,'units':p.units,'native_to_dxf_scale':factor,'native_snapshots':snapshots,
            'entities':records,'visual_losses':dict(losses),'metadata_scope':'original native attributes/styles/text per handle; only restored for the exact complete DXF and matching coordinate context'}
        output.with_name(output.name+'.json').write_text(json_text(sidecar),encoding='utf8')
        return finish_bundle(bridge,request_id,folder,{'backend':'ezdxf1.4.5','entities':len(records),'native_to_dxf_scale':factor,
            'units':p.units,'geometry_text_readback':True,'visual_losses':dict(losses),'source_preserved':True,'native_metadata_in_bound_sidecar':True})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def prepare_dxf_import(bridge,file,expected_sha256,expected_members,parameters,template,request_id):
    p=parameters if isinstance(parameters,DxfImportParameters) else DxfImportParameters.model_validate(parameters)
    outlined=p.text_mode=='outline'
    if outlined and (not p.font_bindings or p.kind=='point'):
        raise BridgeError('DXF_FONT_BINDING: outline mode needs explicit font_bindings and line/polygon output')
    if not outlined and p.font_bindings is not None:raise BridgeError('DXF_FONT_BINDING: bindings require text_mode=outline')
    value=template.get('map_scale')
    if not {'name','crs','map_scale'}<=set(template)<= {'name','crs','map_scale','style'} or type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise BridgeError('DXF_NATIVE_TEMPLATE: name, crs and positive map_scale required; optional style only')
    path=Path(file).resolve(strict=True);members=_members(path)
    if members[path.name]!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if members!=expected_members:raise BridgeError('STALE_DXF_MEMBER: inspect dxf_source_info and pass all members')
    if template.get('name','').lower().endswith({'point':'.wt','line':'.wl','polygon':'.wp'}[p.kind]) is False:raise BridgeError('DXF_NATIVE_KIND')
    doc=_document(path);layout=_layout(doc,p.layout);layers={e.dxf.layer for e in layout}
    if len(set(p.layers))!=len(p.layers) or not set(p.layers)<=layers:raise BridgeError('DXF_LAYER_SELECTION')
    paper=layout.name!='Model'
    if p.compose_viewports:
        model_layers={entity.dxf.layer for entity in doc.modelspace()}
        if not paper:raise BridgeError('DXF_VIEWPORT_LAYOUT: viewport composition requires a selected paper layout')
        if (not p.viewport_model_layers or len(set(p.viewport_model_layers))!=len(p.viewport_model_layers)
            or not set(p.viewport_model_layers)<=model_layers):
            raise BridgeError('DXF_VIEWPORT_LAYERS: explicitly select existing modelspace root-entity layers')
    elif p.viewport_model_layers is not None:raise BridgeError('DXF_VIEWPORT_LAYERS: viewport_model_layers requires compose_viewports=true')
    if paper:
        if not p.paper_coordinate_units_confirmed:raise BridgeError('DXF_PAPER_UNITS: explicitly confirm source_units as the selected paper drawing coordinates; plot setup is not inferred as geometry units')
        if template['crs'].get('kind')!='local':raise BridgeError('DXF_PAPER_FRAME: paper drawing coordinates require a separate explicit local frame, not an inferred Earth CRS')
    elif doc.units!=UNITS[p.source_units][0]:raise BridgeError('DXF_UNIT_CONTEXT: explicit source_units must match the DXF header')
    composition=p.apply_block_clipping or p.compose_viewports
    advanced=paper or p.expand_blocks or composition or outlined
    factor=_factor(UNITS[p.source_units][1],_unit(template['crs']));witness=None;sidecar=path.with_name(path.name+'.json')
    if sidecar.is_file() and p.restore_bound_metadata:
        witness=json.loads(sidecar.read_text(encoding='utf8'))
        if witness.get('schema')!='mapgis-agent/dxf/1' or witness.get('dxf_sha256')!=expected_sha256:raise BridgeError('DXF_SIDECAR_MISMATCH: changed CAD drawings require explicit restore_bound_metadata=false and lose original native metadata')
        if advanced:raise BridgeError('DXF_BOUND_LAYOUT: block/layout conversion requires restore_bound_metadata=false; exact original snapshot restoration is the unchanged modelspace workflow')
        a=explicit_crs(witness['crs']);b=explicit_crs(template['crs'])
        if (a is None)!=(b is None) or a is None and witness['crs']!=template['crs'] or a is not None and not a.equals(b):raise BridgeError('DXF_COORDINATE_FRAME')
    features=[];identity=[];curves=0;selected_counts=Counter();ignored_counts=Counter();selected_records=[]
    stream,expansion=_layout_entities(layout,p)
    for entity,origin,clip in stream:
        entity_type=entity.dxftype()
        if entity_type not in KINDS:raise BridgeError('DXF_UNSUPPORTED_ENTITY: '+entity_type+' in selected layer; choose other layers or convert explicitly')
        if KINDS[entity_type]!=p.kind:ignored_counts[entity_type]+=1;continue
        geometry,text,style=_geometry(entity,p.curve_tolerance)
        if clip is not None and text is not None:
            raise BridgeError('DXF_CLIPPED_TEXT: text anchors cannot prove glyph extents; use a separately verified outline conversion')
        if entity_type in {'ARC','CIRCLE','ELLIPSE','SPLINE'} or entity_type in {'LWPOLYLINE','POLYLINE'} and entity.has_arc:curves+=1
        record=witness['entities'].get(entity.dxf.handle) if witness else None
        if witness and record is None:raise BridgeError('DXF_SIDECAR_ENTITY_MISSING')
        for part_index,part in enumerate(_clipped_parts(geometry,clip,p.kind,expansion['clipping']),1):
            part=scale(part,xfact=factor,yfact=factor,origin=(0,0))
            if record:
                feature=copy.deepcopy(record['feature'])
                if not _geometry_same(shape(feature['geometry']),part) or feature.get('text')!=text:raise BridgeError('DXF_BOUND_GEOMETRY_DIFF')
                selected_records.append(record)
            else:
                feature={'type':'Feature','properties':{'ID':len(features)+1,'CAD_LAYER':origin['effective_layer'] if origin else entity.dxf.layer,
                    'CAD_TYPE':entity_type,'CAD_HANDLE':origin['origin_handle'] if origin else entity.dxf.handle}}
                if origin:feature['properties'].update(CAD_ROOT=origin['root_handle'],CAD_VKEY=origin['virtual_key'])
                if origin and 'viewport_handle' in origin:feature['properties']['CAD_VIEW']=origin['viewport_handle']
                if origin and 'source_text' in origin:
                    feature['properties']['CAD_TYPE']=origin['source_type']
                    feature['properties']['CAD_TPART']=origin['text_part']
                    try:encoded=origin['source_text'].encode('gbk')
                    except UnicodeEncodeError:encoded=None
                    if encoded is not None and len(encoded)<=240:feature['properties']['CAD_TEXT']=origin['source_text']
                if clip is not None:feature['properties']['CAD_PART']=part_index
                if text is not None:
                    native_style={**style,'height':style['height']*factor,'width':style['width']*factor};feature.update(text=text,style=native_style)
            if len(features)>=p.max_expanded_entities:raise BridgeError('DXF_EXPANDED_ENTITY_LIMIT: clipped output record budget exceeded')
            feature.update(id=str(len(features)+1),geometry=feature['geometry'] if record else mapping(part));features.append(feature);selected_counts[entity_type]+=1
            identity.append({'handle':origin['origin_handle'] if origin else entity.dxf.handle,'output_record_id':len(features),'source_record_id':record['feature']['id'] if record else None,
                'source_native_file':record['source']['path'] if record else None})
            if origin:identity[-1].update(origin)
            if clip is not None:identity[-1]['clip_part']=part_index
    ignored_counts.update(expansion['excluded_nonselected_block_entities'])
    if not features:raise BridgeError('DXF_NO_SELECTED_GEOMETRY')
    native_snapshot=None
    if witness and witness.get('native_snapshots'):
        snapshot_names={record['source']['snapshot_name'] for record in selected_records}
        if len(snapshot_names)!=1:raise BridgeError('DXF_BOUND_LAYER_RESTORE: restore one complete original native layer per request; use standard import explicitly to merge')
        name=next(iter(snapshot_names));entry=next(v for v in witness['native_snapshots'] if v['name']==name)
        if [int(v['feature']['id']) for v in selected_records]!=entry['physical_record_ids']:raise BridgeError('DXF_BOUND_LAYER_RESTORE: selection must retain the complete original native layer')
        if template.get('style') or entry['map_scale'] is not None and not math.isclose(template.get('map_scale',0),entry['map_scale'],rel_tol=0,abs_tol=1e-9):raise BridgeError('DXF_BOUND_TEMPLATE: exact layer restoration retains original scale/styles; choose restore_bound_metadata=false for standard conversion')
        native_snapshot=path.parent/name
        for item,record in zip(identity,selected_records):item['output_record_id']=int(record['feature']['id'])
    else:
        native_kind={'point':'POINT','line':'LINE','polygon':'POLYGON'}[p.kind]
        for feature in features:
            extra=set(feature.get('style',{}))-PARAMS[native_kind]
            if extra-(REGION_INDEX_FIELDS if p.kind=='polygon' else set()):raise BridgeError('DXF_NATIVE_STYLE_PROFILE: unsupported native graphic fields '+','.join(sorted(extra)))
            feature['style']={k:v for k,v in feature.get('style',{}).items() if k in PARAMS[native_kind]}
    sources=[{'file':str(path.parent/k),'sha256':v} for k,v in members.items()]
    if outlined:sources.extend({'file':str(Path(v.file).resolve(strict=True)),'sha256':v.expected_sha256} for v in p.font_bindings)
    excluded_parameters=set() if advanced else {'layout','expand_blocks','paper_coordinate_units_confirmed','max_expanded_entities','max_block_depth'}
    if not composition:excluded_parameters|={'apply_block_clipping','compose_viewports','viewport_model_layers'}
    if not outlined:excluded_parameters|={'text_mode','font_bindings'}
    serialized=p.model_dump(exclude=excluded_parameters)
    payload={'operation':'exchange.dxf_import','file':str(path),'expected_sha256':expected_sha256,'expected_members':members,
        'parameters':serialized,'template':template,'dxf_protocol':5 if outlined else 4 if composition else 3 if advanced else 2}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir();target=folder/template['name']
        if native_snapshot:
            shutil.copyfile(native_snapshot,target)
            if file_sha(target)!=file_sha(native_snapshot):raise BridgeError('DXF_SNAPSHOT_CHANGED')
        else:
            encoder=Bridge(stage/'encoder');prepare_creation(encoder,{'type':'FeatureCollection','features':features},template,'native')
            made=encoder.apply('native');shutil.copyfile(made['output'],target)
        result={'schema':'mapgis-agent/dxf-import/1','native_sha256':file_sha(target),'source_sha256':expected_sha256,'crs':template['crs'],
            'dxf_to_native_scale':factor,'source_record_mapping':identity,'selected_entities':dict(selected_counts),'other_kinds_in_selected_layers':dict(ignored_counts),
            'curve_approximation':{'tolerance_source_units':p.curve_tolerance,'curved_entities':curves,
                'algorithm':'ezdxf entity.flattening for analytic curves/splines; path.flattening for bulges/HATCH boundaries; its Bezier conversion error is separate from chord tolerance'},'metadata_restored':bool(witness),
            'restoration_mode':'EXACT_NATIVE_SNAPSHOT_AFTER_DXF_GEOMETRY_CHECK' if native_snapshot else 'STANDARD_DXF_TO_NEW_NATIVE',
            'visual_mapping':'Native bound styles restored when present; otherwise template defaults, text height/width/rotation and explicit native fonts. CAD patterns/blocks/fonts are not guessed.'}
        if advanced:result.update(layout={'name':layout.name,'domain':'paper_drawing_coordinates' if paper else 'model_WCS',
            'source_units':p.source_units,'paper_units_confirmed':p.paper_coordinate_units_confirmed,
            'viewports':'Explicit selected top-WCS 2D model content composed and clipped in paper units' if p.compose_viewports else 'not composed; a selectedVIEWPORT is explicitlyunsupported'},block_expansion=dict(expansion))
        if composition:result['clipping']=dict(expansion['clipping'])
        if outlined:result['text_outlines']={'font_bindings':[v.model_dump() for v in p.font_bindings],
            'curve_tolerance_final_drawing_units':p.curve_tolerance,'sources':'Full UTF8 source text and exactfontSHA in source_record_mapping; native CAD_TEXT only when GBK<=240bytes.',
            'layout':'Single-line TEXT/ATTRIB/constantATTDEF cmap/horizontaladvance, declared alignment/OCS, copied block matrices. No kerning/GSUB/GPOS or richMTEXT layout; no font fallback or originalCADrender equivalence.'}
        target.with_name(target.name+'.cad.json').write_text(json_text(result),encoding='utf8')
        return finish_bundle(bridge,request_id,folder,{'backend':'ezdxf1.4.5 and existing native writer','metadata_restored':bool(witness),'source_preserved':True,
            'restoration_mode':result['restoration_mode'],
            'dxf_to_native_scale':factor,'curve_approximation':result['curve_approximation'],'selected_entities':dict(selected_counts),
            'other_kinds_in_selected_layers':dict(ignored_counts),'source_record_mapping':identity,'native_save_reopen':'separate native operation; no inferred external editor validation',
            **({'layout':result['layout'],'block_expansion':result['block_expansion']} if advanced else {}),
            **({'clipping':result['clipping']} if composition else {}),
            **({'text_outlines':result['text_outlines']} if outlined else {})})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
