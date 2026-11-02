"""Explicit soil lattices and byte-preserving sediment sample identifiers."""
from __future__ import annotations

import csv
import math
from pathlib import Path
import shutil
from typing import Literal

import numpy as np
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictFloat,StrictInt
from shapely import get_coordinates,union_all
from shapely.geometry import LineString,Point,mapping
from pymapgis.reader import _ATTR_HEADER_INDEX

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .attributes import prepare_attribute_schema
from .core import Bridge,BridgeError,json_text,read_native,native_ids,scalar,sha
from .creation import prepare_creation
from .raster import explicit_crs,file_sha


class LabelOptions(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    enabled:StrictBool=True
    offset_m:list[StrictFloat]=Field(default_factory=lambda:[2.,2.],min_length=2,max_length=2)
    height_mm:StrictFloat=Field(default=2.5,gt=0)
    font_zh:StrictInt=Field(default=1,ge=0,le=255)
    font_en:StrictInt=Field(default=0,ge=0,le=255)
    color:StrictInt=Field(default=1,ge=0,le=255)
    angle_degrees:StrictFloat=0.


class SoilGridParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    origin:list[StrictFloat]=Field(min_length=2,max_length=2)
    grid_north_azimuth_deg:StrictFloat=Field(ge=0,lt=360)
    point_spacing_m:StrictFloat=Field(gt=0)
    line_spacing_m:StrictFloat=Field(gt=0)
    base_line_number:StrictInt
    base_point_number:StrictInt
    line_rule:Literal['left_odd_right_even','left_even_right_odd','sequential']
    line_number_step:StrictInt=Field(default=1,ge=1,le=1000000000)
    point_number_step:StrictInt=Field(default=1,ge=1,le=1000000000)
    prefix:str=Field(default='',max_length=64)
    separator:str=Field(default='-',min_length=1,max_length=8)
    line_digits:StrictInt=Field(default=0,ge=0,le=12)
    point_digits:StrictInt=Field(default=0,ge=0,le=12)
    allow_negative_numbers:StrictBool=False
    include_boundary:StrictBool=True
    boundary_tolerance_m:StrictFloat=Field(ge=0)
    labels:LabelOptions=Field(default_factory=LabelOptions)
    max_candidate_points:StrictInt=Field(default=200000,ge=1,le=2000000)
    max_points:StrictInt=Field(default=20000,ge=1,le=200000)
    max_lines:StrictInt=Field(default=2000,ge=1,le=10000)


class NumberingFields(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sample_id:str='SAMPLE_ID'
    large_cell:str='BIG_GRID'
    small_cell:str='SMALL_GRID'
    sequence:str='CELL_SEQ'


class SedimentNumberingParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    origin:list[StrictFloat]=Field(min_length=2,max_length=2)
    origin_reference:Literal['upper_left_cell_centre','upper_left_outer_corner']
    large_columns:StrictInt=Field(ge=1,le=10000)
    large_rows:StrictInt=Field(ge=1,le=10000)
    large_width_m:StrictFloat=Field(gt=0)
    large_height_m:StrictFloat=Field(gt=0)
    first_large_number:StrictInt=Field(ge=0,le=2147483647)
    last_large_number:StrictInt=Field(ge=0,le=2147483647)
    boundary_rule:Literal['reject','north_west','south_east']
    boundary_tolerance_m:StrictFloat=Field(ge=0)
    prefix:str=Field(default='',max_length=64)
    large_digits:StrictInt=Field(default=0,ge=0,le=12)
    sequence_digits:StrictInt=Field(default=0,ge=0,le=12)
    sequence_start:StrictInt=Field(default=1,ge=1,le=2147483647)
    coincident_policy:Literal['error','physical_record_order']='error'
    existing_id_field:str|None=None
    reserved_sample_ids:list[str]=Field(default_factory=list,max_length=100000)
    fields:NumberingFields=Field(default_factory=NumberingFields)
    labels:LabelOptions=Field(default_factory=LabelOptions)


def _template(template):
    if (not isinstance(template,dict) or not {'name','crs','map_scale'}<=set(template)
        or set(template)-{'name','crs','map_scale','style'}):raise BridgeError('NUMBERING_TEMPLATE')
    name=template['name'];value=template['map_scale']
    if (not isinstance(name,str) or Path(name).name!=name or not name.lower().endswith('.wt')
        or any(c in name for c in '/\\:<>"|?*') or name.rstrip('. ')!=name
        or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}):
        raise BridgeError('NUMBERING_OUTPUT_NAME')
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise BridgeError('NUMBERING_MAP_SCALE')
    return name,Path(name).stem


def _source(file,expected,template,kind):
    path=Path(file).resolve(strict=True);blob,reader,sections=read_native(path)
    if sha(blob)!=expected:raise BridgeError('STALE_SOURCE')
    if reader.shapeType!=kind or not 1<=len(reader.geom)<=200000:raise BridgeError('NUMBERING_SOURCE_KIND: nonempty native '+kind+' required, at most200000active records')
    if any(g.is_empty or not g.is_valid or g.has_z or getattr(g,'has_m',False)
        or any(not math.isfinite(v) for v in g.bounds) for g in reader.geom):raise BridgeError('NUMBERING_GEOMETRY')
    crs=explicit_crs(template['crs']);actual=reader.crs or None
    if (actual is None)!=(crs is None) or actual is not None and not actual.equals(crs):
        raise BridgeError('NUMBERING_CRS_MISMATCH: retain actual native CRS; unknown native coordinates require an explicit local frame')
    if crs is None:factor={'metre':1.,'millimetre':.001}.get(template['crs'].get('unit'))
    else:
        factor=(crs.axis_info[0].unit_conversion_factor if crs.is_projected and len(crs.axis_info)>=2
            and math.isclose(crs.axis_info[0].unit_conversion_factor,crs.axis_info[1].unit_conversion_factor,rel_tol=1e-15) else None)
    if factor is None or not math.isfinite(factor) or factor<=0:raise BridgeError('NUMBERING_LINEAR_FRAME: known local/projected linear units required; no degrees or inferred units')
    return path,blob,reader,sections,float(factor)


def _identifier(value):
    if not isinstance(value,str) or not value or '\0' in value:raise BridgeError('NUMBERING_SAMPLE_ID')
    try:encoded=value.encode('gbk',errors='strict')
    except UnicodeEncodeError as exc:raise BridgeError('NUMBERING_GBK_ID: native identifiers must be representable without substitution') from exc
    if len(encoded)>240:raise BridgeError('NUMBERING_ID_LENGTH: sample identifier exceeds240GBKbytes')
    return value


def _label(feature_id,record_id,sample,x,y,labels,factor):
    return {'type':'Feature','id':feature_id,'geometry':{'type':'Point',
        'coordinates':[x+labels.offset_m[0]/factor,y+labels.offset_m[1]/factor]},
        'properties':{'SAMPLE_ID':sample,'SRC_RID':record_id},'text':sample,
        'style':{'point_type':0,'height':labels.height_mm,'width':labels.height_mm,
            'font_zh':labels.font_zh,'font_en':labels.font_en,'color':labels.color,'angle':labels.angle_degrees}}


def _layer(encoder,folder,features,template,tag):
    prepare_creation(encoder,{'type':'FeatureCollection','features':features},template,tag)
    made=encoder.apply(tag);target=folder/template['name'];shutil.copyfile(made['output'],target)
    return target


def _index(folder,stem,rows,metadata):
    with (folder/(stem+'-index.csv')).open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    metadata['row_mapping']=rows
    metadata['native_files']={file.name:file_sha(file) for file in folder.iterdir() if file.suffix.lower() in {'.wt','.wl'}}
    (folder/(stem+'.numbering.json')).write_text(json_text(metadata),encoding='utf8')


def prepare_soil_grid(bridge,file,expected_sha256,parameters,template,request_id):
    p=parameters if isinstance(parameters,SoilGridParameters) else SoilGridParameters.model_validate(parameters)
    name,stem=_template(template);path,before,reader,sections,factor=_source(file,expected_sha256,template,'POLYGON')
    if p.line_rule!='sequential' and (p.base_line_number%2 or p.line_number_step%2==0):
        raise BridgeError('SOIL_PARITY: absolute odd/even rules need an even base line and odd number step')
    area=union_all(list(reader.geom));coordinates=get_coordinates(area)
    if not math.isfinite(area.area) or area.area<=0:raise BridgeError('SOIL_AREA')
    a=math.radians(p.grid_north_azimuth_deg);sa,ca=math.sin(a),math.cos(a);ox,oy=p.origin
    dx=(coordinates[:,0]-ox)*factor;dy=(coordinates[:,1]-oy)*factor
    u=dx*ca-dy*sa;v=dx*sa+dy*ca
    extents=[float(np.min(u)),float(np.max(u)),float(np.min(v)),float(np.max(v))]
    ratios=[extents[0]/p.line_spacing_m,extents[1]/p.line_spacing_m,extents[2]/p.point_spacing_m,extents[3]/p.point_spacing_m]
    if not all(math.isfinite(q) and abs(q)<1e9 for q in ratios):raise BridgeError('SOIL_GRID_BUDGET: invalid/extreme lattice indices')
    # Include possible tolerance-near edge nodes; the actual range predicate is
    # still checked for every anchor before assigning an identifier.
    jmin=math.floor(ratios[0])-1;jmax=math.ceil(ratios[1])+1
    kmin=math.floor(ratios[2])-1;kmax=math.ceil(ratios[3])+1
    if (jmax-jmin+1)*(kmax-kmin+1)>p.max_candidate_points or jmax-jmin+1>p.max_lines:
        raise BridgeError('SOIL_GRID_BUDGET: increase explicit candidate/line bounds or use a smaller area')
    tolerance=p.boundary_tolerance_m/factor
    accepted_area=area.buffer(tolerance if p.include_boundary else -tolerance) if tolerance else area
    predicate=accepted_area.covers if p.include_boundary else accepted_area.contains
    def xy(j,k):
        right=j*p.line_spacing_m;along=k*p.point_spacing_m
        return ox+(right*ca+along*sa)/factor,oy+(-right*sa+along*ca)/factor
    def line_number(j):
        if p.line_rule=='sequential':return p.base_line_number+j*p.line_number_step
        odd_left=p.line_rule=='left_odd_right_even'
        offset=0 if j==0 else 2*abs(j)-(1 if (j<0)==odd_left else 0)
        return p.base_line_number+offset*p.line_number_step
    points=[];labels=[];index=[];lines=[];seen=set()
    for j in range(jmin,jmax+1):
        for k in range(kmin,kmax+1):
            x,y=xy(j,k)
            if not predicate(Point(x,y)):continue
            line=line_number(j);number=p.base_point_number+k*p.point_number_step
            if not p.allow_negative_numbers and min(line,number)<0:raise BridgeError('SOIL_NEGATIVE_NUMBER: adjust base numbers or explicitly allow signed labels')
            if any(not -2147483648<=value<=2147483647 for value in (line,number)):raise BridgeError('SOIL_NUMBER_RANGE')
            sample=_identifier(p.prefix+str(line).zfill(p.line_digits)+p.separator+str(number).zfill(p.point_digits))
            if sample in seen:raise BridgeError('SOIL_DUPLICATE_ID')
            seen.add(sample);rid=len(points)+1
            if rid>p.max_points:raise BridgeError('SOIL_GRID_BUDGET: output point limit exceeded')
            points.append({'type':'Feature','id':f'u{j}_v{k}','geometry':{'type':'Point','coordinates':[x,y]},
                'properties':{'SAMPLE_ID':sample,'LINE_NO':line,'POINT_NO':number,'GRID_J':j,'GRID_K':k}})
            labels.append(_label(f'point-{rid}',rid,sample,x,y,p.labels,factor))
            index.append({'output_record_id':rid,'sample_id':sample,'line_number':line,'point_number':number,
                'grid_j':j,'grid_k':k,'x_native':x,'y_native':y})
        clipped=LineString([xy(j,kmin),xy(j,kmax)]).intersection(area)
        def parts(geometry):
            if geometry.is_empty:return []
            if geometry.geom_type=='LineString':return [geometry] if geometry.length>0 else []
            return [part for child in getattr(geometry,'geoms',[]) for part in parts(child)]
        for part_number,part in enumerate(parts(clipped),1):
            if len(lines)>=p.max_lines:raise BridgeError('SOIL_GRID_BUDGET: clipped line part limit exceeded')
            lines.append({'type':'Feature','id':f'line{j}-part{part_number}','geometry':mapping(part),
                'properties':{'LINE_NO':line_number(j),'GRID_J':j,'LINE_PART':part_number}})
    if not points:raise BridgeError('NUMBERING_NO_POINTS: no lattice anchor in the declared measurement area')
    payload={'operation':'geochemistry.soil-grid','file':str(path),'expected_sha256':expected_sha256,
        'parameters':p.model_dump(),'template':template,'numbering_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],name)
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir();encoder=Bridge(stage/'encoder')
        _layer(encoder,folder,points,template|{'style':template.get('style',{'point_type':2,'radius_mm':1.,'filled':1,'pen_width':.2,'color':3,'border_color':3})},'points')
        base={k:template[k] for k in ('crs','map_scale')}
        if p.labels.enabled:_layer(encoder,folder,labels,base|{'name':stem+'-labels.wt'},'labels')
        if lines:_layer(encoder,folder,lines,base|{'name':stem+'-lines.wl','style':{'linetype':1,'width':.15,'color':1}},'lines')
        metadata={'schema':'mapgis-agent/soil-grid/1','source':str(path),'source_sha256':expected_sha256,
            'source_physical_regions':native_ids(reader),'parameters':p.model_dump(),'crs':template['crs'],
            'metres_per_native_unit':factor,'azimuth':'clockwise from declared coordinate-grid +Y; not inferred true/magnetic north',
            'line_number_formula':'sequential=base+j*step; odd/even sides use base+(2*abs(j)-oddSide)*step, centre=base',
            'point_number_formula':'base_point+k*point_step; spacings and label offsets in declared metres',
            'points':len(points),'line_parts':len(lines),'line_output':'PRESENT' if lines else 'EMPTY_VALID_NO_FABRICATED_WL',
            'source_preserved':True,'field_sampling_or_standard_compliance':'NOT_VERIFIED'}
        _index(folder,stem,index,metadata)
        return finish_bundle(bridge,request_id,folder,{'backend':'GEOS and existing native encoder','source_preserved':True,
            'metrics':{'points':len(points),'labels':len(labels) if p.labels.enabled else 0,'line_parts':len(lines)},
            'index':stem+'-index.csv','metadata':stem+'.numbering.json','coordinate_frame':metadata['crs'],
            'native_save_reopen':'separate native operation; not inferred from file readback'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def _half_cell(distance,half_size,count,p):
    tolerance=p.boundary_tolerance_m
    q=distance/half_size;nearest=round(q)
    edge=abs(distance-nearest*half_size)<=tolerance
    if edge:
        if p.boundary_rule=='reject':raise BridgeError('SAMPLE_GRID_BOUNDARY: choose an explicit owner for grid-edge anchors')
        if nearest==0:return 0
        if nearest==count:return count-1
        return nearest-1 if p.boundary_rule=='north_west' else nearest
    index=math.floor(q)
    if not 0<=index<count:raise BridgeError('SAMPLE_GRID_OUTSIDE')
    return index


def prepare_sediment_numbering(bridge,file,expected_sha256,parameters,template,request_id):
    p=parameters if isinstance(parameters,SedimentNumberingParameters) else SedimentNumberingParameters.model_validate(parameters)
    name,stem=_template(template);path,before,reader,sections,factor=_source(file,expected_sha256,template,'POINT')
    if template.get('style'):raise BridgeError('NUMBERING_PRESERVE_STYLE: sediment numbering retains all original native graphic bytes; style the new branch separately')
    cells=p.large_columns*p.large_rows
    if cells>1000000 or p.first_large_number+cells-1!=p.last_large_number:raise BridgeError('SAMPLE_GRID_RANGE: first/last numbers must match the explicit rows and columns')
    if p.boundary_tolerance_m>=min(p.large_width_m,p.large_height_m)/4:raise BridgeError('SAMPLE_GRID_TOLERANCE')
    names=p.fields.model_dump();used={str(c).casefold() for c in reader.data.columns}
    for value in names.values():
        try:length=len(value.encode('gbk'))
        except UnicodeEncodeError as exc:raise BridgeError('NUMBERING_FIELD_NAME') from exc
        if not value or length>19 or value.casefold() in used:raise BridgeError('NUMBERING_NEW_FIELDS: choose unique unused native field names')
        used.add(value.casefold())
    if p.existing_id_field is not None and p.existing_id_field not in reader.data.columns:raise BridgeError('NUMBERING_EXISTING_FIELD')
    if len(set(p.reserved_sample_ids))!=len(p.reserved_sample_ids):raise BridgeError('NUMBERING_RESERVED_IDS')
    reserved={_identifier(value) for value in p.reserved_sample_ids};taken=set(reserved)
    ox,oy=p.origin
    if p.origin_reference=='upper_left_cell_centre':
        left=ox-p.large_width_m/(2*factor);top=oy+p.large_height_m/(2*factor)
    else:left,top=ox,oy
    identities=native_ids(reader);entries=[];positions=set();existing=set()
    for offset,(rid,point) in enumerate(zip(identities,reader.geom)):
        if point.geom_type!='Point':raise BridgeError('NUMBERING_SAMPLE_ANCHORS: POINT anchor coordinates required')
        position=(point.x,point.y)
        if position in positions and p.coincident_policy=='error':raise BridgeError('SAMPLE_COINCIDENT: explicitly select physical-record tie handling for colocated samples')
        positions.add(position)
        east=(point.x-left)*factor;south=(top-point.y)*factor;tolerance=p.boundary_tolerance_m
        if not (-tolerance<=east<=p.large_width_m*p.large_columns+tolerance
            and -tolerance<=south<=p.large_height_m*p.large_rows+tolerance):
            raise BridgeError('SAMPLE_GRID_OUTSIDE: every source anchor must be in the declared grid')
        column=_half_cell(east,p.large_width_m/2,p.large_columns*2,p)
        row=_half_cell(south,p.large_height_m/2,p.large_rows*2,p)
        large=p.first_large_number+(row//2)*p.large_columns+column//2;small='ABCD'[(row%2)*2+column%2]
        old=scalar(reader.data.iloc[offset][p.existing_id_field]) if p.existing_id_field else None
        frozen=None
        if old is not None and old!='':
            if not isinstance(old,str):raise BridgeError('NUMBERING_EXISTING_TEXT_ID: existing IDs need explicit text formatting before preservation')
            if old.strip():
                frozen=_identifier(old)
                if frozen in existing:raise BridgeError('NUMBERING_DUPLICATE_EXISTING_ID')
                existing.add(frozen);taken.add(frozen)
        prefix=p.prefix+str(large).zfill(p.large_digits)+small
        entries.append({'source_record_id':rid,'output_record_id':rid,'x_native':point.x,'y_native':point.y,
            'large_cell':large,'small_cell':small,'large_column':column//2,'large_row':row//2,
            'previous_id':old,'sample_id':frozen,'sequence':'','assignment_source':'preserved_existing' if frozen else 'generated','cell_prefix':prefix})
    grouped={}
    for entry in entries:grouped.setdefault((entry['large_cell'],entry['small_cell']),[]).append(entry)
    skipped=[]
    for group in grouped.values():
        sequence=p.sequence_start
        for entry in sorted(group,key=lambda row:(row['x_native'],-row['y_native'],row['source_record_id'])):
            if entry['sample_id']:
                prefix=entry['cell_prefix'];suffix=entry['sample_id'][len(prefix):] if entry['sample_id'].startswith(prefix) else ''
                entry['sequence']=suffix if suffix.isascii() and suffix.isdigit() else ''
                continue
            while True:
                identifier=_identifier(entry['cell_prefix']+str(sequence).zfill(p.sequence_digits));number=str(sequence).zfill(p.sequence_digits)
                sequence+=1
                if identifier not in taken:break
                skipped.append(identifier)
            entry.update(sample_id=identifier,sequence=number);taken.add(identifier)
    for entry in entries:entry.pop('cell_prefix')
    fields=[{'name':str(column),'source':str(column)} for column in reader.data.columns]
    samples=[entry['sample_id'] for entry in entries];sequences=[entry['sequence'] for entry in entries]
    fields.extend([{'name':names['sample_id'],'type':'string','length':max(len(v.encode('gbk')) for v in samples),'default':''},
        {'name':names['large_cell'],'type':'int','default':0},{'name':names['small_cell'],'type':'string','length':1,'default':''},
        {'name':names['sequence'],'type':'string','length':max(1,max(map(len,sequences))),'default':''}])
    updates={str(entry['source_record_id']):{names['sample_id']:entry['sample_id'],names['large_cell']:entry['large_cell'],
        names['small_cell']:entry['small_cell'],names['sequence']:entry['sequence']} for entry in entries}
    payload={'operation':'geochemistry.sediment-numbering','file':str(path),'expected_sha256':expected_sha256,
        'parameters':p.model_dump(),'template':template,'numbering_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],name)
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir();encoder=Bridge(stage/'encoder');input_folder=encoder.root/'work/input';input_folder.mkdir(parents=True)
        snapshot=input_folder/path.name;shutil.copyfile(path,snapshot)
        prepare_attribute_schema(encoder,str(snapshot),expected_sha256,fields,'schema',attribute_updates=updates)
        made=encoder.apply('schema');target=folder/name;shutil.copyfile(made['output'],target)
        after,checked,new_sections=read_native(target)
        if native_ids(checked)!=identities or any(before[a:a+n]!=after[b:b+m] for i,((a,n),(b,m)) in enumerate(zip(sections,new_sections)) if i!=_ATTR_HEADER_INDEX['POINT']):
            raise BridgeError('NUMBERING_NON_ATTRIBUTE_SECTION_CHANGED')
        for offset,entry in enumerate(entries):
            for field,value in updates[str(entry['source_record_id'])].items():
                if checked.data.iloc[offset][field]!=value:raise BridgeError('NUMBERING_ATTRIBUTE_READBACK')
        if p.labels.enabled:
            labels=[_label(f'rid-{entry["source_record_id"]}',entry['source_record_id'],entry['sample_id'],entry['x_native'],entry['y_native'],p.labels,factor) for entry in entries]
            _layer(encoder,folder,labels,{k:template[k] for k in ('crs','map_scale')}|{'name':stem+'-labels.wt'},'labels')
        metadata={'schema':'mapgis-agent/sediment-numbering/1','source':str(path),'source_sha256':expected_sha256,'parameters':p.model_dump(),
            'coordinate_frame':template['crs'],'metres_per_native_unit':factor,'physical_record_ids_preserved':True,
            'non_attribute_sections_byte_identical':True,'source_header_crs_scale_and_graphics':'retained; template map_scale controls new labels only',
            'grid_definition':'Axis-aligned native XY; large cells left-to-right/top-to-bottom; ABCD=NW/NE/SW/SE; within-cell x ascending,y descending,thenexplicit physical-ID tie rule.',
            'edge_definition':'Declared owner for internal half-cell lines; outer rectangle edges belong to its only internal cell unless rule=reject; no outside clamping beyond explicit tolerance.',
            'unknown_existing_sequence':'Empty string, not zero; full existing ID stays unchanged even when it does not match generated syntax.',
            'reserved_ids':p.reserved_sample_ids,'skipped_allocations':skipped,'source_preserved':True,
            'source_point_semantics':'All WT anchors including symbol/text/circle/arc centres are explicitly numbered as sample positions; full original graphics remain byte-identical.',
            'field_sampling_or_standard_compliance':'NOT_VERIFIED'}
        _index(folder,stem,entries,metadata)
        return finish_bundle(bridge,request_id,folder,{'backend':'Existing byte-preserving attribute schema writer and native label encoder',
            'source_preserved':True,'physical_record_ids_preserved':True,'non_attribute_sections_byte_identical':True,
            'metrics':{'samples':len(entries),'preserved_existing_ids':len(existing),'generated_ids':len(entries)-len(existing),'skipped_reserved_or_existing':len(skipped)},
            'index':stem+'-index.csv','metadata':stem+'.numbering.json','native_save_reopen':'separate native operation; not inferred from file readback'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
