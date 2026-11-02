"""Spatial left joins and grouped fields using GEOS and existing native editors."""
from __future__ import annotations
import csv,math,shutil,statistics
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictFloat,StrictInt
from pymapgis import reader as native
from shapely import STRtree,union_all

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .attributes import prepare_attribute_schema
from .core import Bridge,BridgeError,json_text,read_native,scalar,sha,native_ids
from .raster import file_sha
from .spatial import _source,_frame

AREA_OPERATIONS={'weighted_mean','prorated_sum','coverage_area'}


class SpatialSource(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    file:str=Field(min_length=1)
    expected_sha256:str=Field(pattern='^[0-9a-f]{64}$')


class SummaryColumn(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    name:str=Field(min_length=1)
    operation:Literal['count','sum','mean','min','max','median','stddev','single','concat_unique','weighted_mean','prorated_sum','coverage_area']
    field:str|None=None
    type:Literal['int','double','string']|None=None
    length:StrictInt|None=Field(default=None,ge=1,le=32000)
    empty_value:str|StrictInt|StrictFloat|None=None
    ddof:Literal[0,1]=1


class SummaryParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    predicate:Literal['intersects','contains','contains_properly','covers','within','covered_by','touches','overlaps','crosses','equals']
    crs:dict|None=None
    match_policy:Literal['all','unique']
    nulls:Literal['error','skip']='error'
    columns:list[SummaryColumn]=Field(min_length=1,max_length=1000)
    weighting:Literal['intersection_area']|None=None
    max_pairs:StrictInt=Field(default=1000000,ge=1,le=2000000)


def _column_type(column):
    default='int' if column.operation=='count' else 'string' if column.operation=='concat_unique' else 'double'
    result=column.type or default
    if column.operation=='count' and result!='int' or column.operation=='concat_unique' and result!='string' or column.operation not in {'count','concat_unique','single'} and result=='string':
        raise BridgeError('SPATIAL_TARGET_TYPE: count=int, concat_unique=string, numeric statistics=int/double; single may explicitly use string')
    if result=='string' and column.length is None:raise BridgeError('SPATIAL_STRING_LENGTH: declare native GBK byte capacity')
    if result!='string' and column.length is not None:raise BridgeError('SPATIAL_STRING_LENGTH: only string targets have length')
    if column.operation!='stddev' and 'ddof' in column.model_fields_set:raise BridgeError('SPATIAL_DDOF: only stddev uses ddof')
    return result


def _value(column,values,nulls):
    if column.operation=='count' and column.field is None:return len(values)
    if any(v is None for v in values):
        if nulls=='error':raise BridgeError('SPATIAL_NULL_VALUE: choose nulls=skip explicitly to ignore missing values')
        values=[v for v in values if v is not None]
    if column.operation=='count':return len(values)
    if not values or column.operation=='stddev' and len(values)<=column.ddof:
        if 'empty_value' not in column.model_fields_set or column.empty_value is None:raise BridgeError('SPATIAL_EMPTY_VALUE: supply a typed empty_value for empty or undersampled groups in '+column.name)
        return column.empty_value
    if column.operation=='single':
        if len(values)!=1:raise BridgeError('SPATIAL_SINGLE_MATCH: a single-value join requires exactly one matched record')
        return values[0]
    if column.operation=='concat_unique':
        if any(not isinstance(v,str) for v in values):raise BridgeError('SPATIAL_STRING_VALUES: concat_unique requires strings without implicit numeric conversion')
        return ';'.join(sorted(set(values)))
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise BridgeError('SPATIAL_NUMERIC_VALUES: statistics require finite numeric source values')
    numbers=list(map(float,values))
    functions={'sum':math.fsum,'mean':statistics.fmean,'min':min,'max':max,'median':statistics.median,
        'stddev':statistics.stdev if column.ddof else statistics.pstdev}
    result=float(functions[column.operation](numbers))
    if not math.isfinite(result):raise BridgeError('SPATIAL_NONFINITE_STATISTIC')
    return result


def _area_value(column,values,areas,source_areas,coverage,nulls):
    if column.operation=='coverage_area':return coverage
    if any(value is None for value in values) and nulls=='error':raise BridgeError('SPATIAL_NULL_VALUE: choose nulls=skip explicitly to ignore missing values')
    rows=[(value,area,source) for value,area,source in zip(values,areas,source_areas) if value is not None]
    if not rows:return _value(column,[],nulls)
    if any(type(value) not in (int,float) or not math.isfinite(value) for value,_,_ in rows):raise BridgeError('SPATIAL_NUMERIC_VALUES: finite area-weighted source values required')
    try:
        denominator=math.fsum(area for _,area,_ in rows)
        result=math.fsum(float(value)*(area/denominator if column.operation=='weighted_mean' else area/source) for value,area,source in rows)
    except (ValueError,OverflowError,ZeroDivisionError) as exc:raise BridgeError('SPATIAL_NONFINITE_STATISTIC: area weights overflow or collapse') from exc
    if not math.isfinite(result):raise BridgeError('SPATIAL_NONFINITE_STATISTIC')
    return result


def prepare_spatial_summary(bridge,left,right,parameters,request_id):
    left=SpatialSource.model_validate(left).model_dump();right=SpatialSource.model_validate(right).model_dump()
    p=parameters if isinstance(parameters,SummaryParameters) else SummaryParameters.model_validate(parameters)
    a,ra,aid,ga=_source(left);b,rb,bid,gb=_source(right)
    if not 1<=len(aid)<=200000 or len(aid)*len(p.columns)>2000000:raise BridgeError('SPATIAL_ASSIGNMENT_LIMIT: non-empty left layer and at most 2000000 field assignments required')
    frame,units=_frame(ra,rb,p.crs);occupied={str(k).casefold() for k in ra.data.columns};types=[]
    if any(c.operation in AREA_OPERATIONS for c in p.columns) and not p.weighting:
        raise BridgeError('SPATIAL_AREA_WEIGHTING: explicitly choose weighting=intersection_area')
    source_areas=[]
    if p.weighting:
        if ra.shapeType!='POLYGON' or rb.shapeType!='POLYGON':raise BridgeError('SPATIAL_AREA_TYPE: polygon targets and sources required')
        if ra.crs and not ra.crs.is_projected:raise BridgeError('SPATIAL_AREA_FRAME: use projected or declared local planar coordinates; geographic degrees are not area weights')
        source_areas=[float(g.area) for g in gb]
        if any(not math.isfinite(v) or v<=0 for v in source_areas):raise BridgeError('SPATIAL_AREA_SOURCE: finite positive source polygon areas required')
    for column in p.columns:
        if column.name.casefold() in occupied:raise BridgeError('SPATIAL_TARGET_EXISTS: choose a new field name; existing fields are preserved')
        occupied.add(column.name.casefold());types.append(_column_type(column))
        if (column.operation not in {'count','coverage_area'} and column.field is None
                or column.field is not None and column.field not in rb.data.columns
                or column.operation=='coverage_area' and column.field is not None):raise BridgeError('SPATIAL_SUMMARY_FIELD: inspect source fields before mapping statistics; coverage_area has no source field')
    tree=STRtree(gb);groups={};owners={};pair_count=0;candidate_pairs=0;area_weights={};coverage={};touches=0
    needs_coverage=any(c.operation=='coverage_area' for c in p.columns)
    for i in sorted(range(len(aid)),key=lambda i:aid[i]):
        indexes=tree.query(ga[i],predicate='intersects' if p.predicate=='equals' else p.predicate)
        matches=sorted((int(j) for j in indexes if p.predicate!='equals' or ga[i].equals(gb[int(j)])),key=lambda j:bid[j])
        candidate_pairs+=len(matches)
        if candidate_pairs>p.max_pairs:raise BridgeError('SPATIAL_PAIR_LIMIT: split the layer or raise max_pairs within the bounded limit')
        if p.weighting:
            selected=[];areas=[];pieces=[]
            for j in matches:
                piece=ga[i].intersection(gb[j]);area=float(piece.area)
                if not math.isfinite(area) or not piece.is_valid:raise BridgeError('SPATIAL_AREA_INTERSECTION: invalid/nonfinite overlap; no implicit repair')
                if area<=0:touches+=1;continue
                selected.append(j);areas.append(area)
                if needs_coverage:pieces.append(piece)
            matches=selected;area_weights[aid[i]]=areas
            if needs_coverage:
                covered=float(union_all(pieces).area)
                if not math.isfinite(covered):raise BridgeError('SPATIAL_AREA_INTERSECTION: nonfinite union area')
                coverage[aid[i]]=covered
        pair_count+=len(matches)
        groups[aid[i]]=matches
        for j in matches:owners.setdefault(bid[j],[]).append(aid[i])
    multiple=[rid for rid,records in owners.items() if len(records)>1]
    if p.match_policy=='unique' and multiple:raise BridgeError(f'SPATIAL_MULTIPLE_OWNERS: {len(multiple)} right records match multiple left records; choose boundary predicate/resolve overlaps, or explicitly use match_policy=all')
    updates={};mapping=[]
    for rid,indexes in groups.items():
        record={}
        for column in p.columns:
            values=[scalar(rb.data.iloc[j][column.field]) if column.field is not None else None for j in indexes]
            record[column.name]=(_area_value(column,values,area_weights[rid],[source_areas[j] for j in indexes],coverage.get(rid,0.),p.nulls)
                if column.operation in AREA_OPERATIONS else _value(column,values,p.nulls))
        match={'left_record_id':rid,'right_record_ids':[bid[j] for j in indexes]}
        if p.weighting:match.update(intersection_areas=area_weights[rid],source_area_fractions=[area/source_areas[j] for area,j in zip(area_weights[rid],indexes)])
        updates[str(rid)]=record;mapping.append(match)
    fields=[{'name':str(k),'source':str(k)} for k in ra.data.columns]
    for column,kind in zip(p.columns,types):
        field={'name':column.name,'type':kind,'default':'' if kind=='string' else 0}
        if kind=='string':field['length']=column.length
        fields.append(field)
    payload={'operation':'spatial.summary','left':left,'right':right,'parameters':p.model_dump(exclude_unset=True)}
    sources=[{'file':str(a),'sha256':left['expected_sha256']},{'file':str(b),'sha256':right['expected_sha256']}]
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,a.name)
    if not new:return receipt
    try:
        encoder=Bridge(stage/'encoder');snapshot=encoder.root/'work/input'/a.name;snapshot.parent.mkdir(parents=True);shutil.copyfile(a,snapshot)
        prepare_attribute_schema(encoder,str(snapshot),left['expected_sha256'],fields,'schema');schema=encoder.apply('schema')
        encoder.prepare(schema['output'],schema['candidate_sha256'],aid,'values',attribute_updates=updates);populated=encoder.apply('values')
        files=stage/'files';files.mkdir();target=files/a.name;shutil.copyfile(populated['output'],target)
        before,original,sections=read_native(a);after,checked,new_sections=read_native(target);attribute=native._ATTR_HEADER_INDEX[ra.shapeType]
        if native_ids(checked)!=aid or any(before[x:x+n]!=after[y:y+m] for i,((x,n),(y,m)) in enumerate(zip(sections,new_sections)) if i!=attribute):raise BridgeError('SPATIAL_NON_ATTRIBUTE_SECTION_CHANGED')
        if sha(before)!=left['expected_sha256'] or file_sha(b)!=right['expected_sha256']:raise BridgeError('STALE_SOURCE')
        metrics={'left_records':len(aid),'right_records':len(bid),'pairs':pair_count,'matched_left_records':sum(bool(v) for v in groups.values()),
            'unmatched_left_records':sum(not v for v in groups.values()),'matched_right_records':len(owners),'unmatched_right_records':len(bid)-len(owners),
            'multiply_matched_right_records':len(multiple),'fields_added':len(p.columns)}
        weighting={'area_weighting':{'mode':'intersection_area','area_units':units+' squared','touches_excluded':touches,'candidate_pairs':candidate_pairs,
            'weighted_mean':'sum(value * intersection_area) / sum(intersection_area), omitting skipped nulls from both sums',
            'prorated_sum':'sum(value * intersection_area / source_polygon_area); uniform source density assumption',
            'coverage_area':'union area of positive intersections; overlapping sources counted once here',
            'overlaps':'weights are per source feature; overlapping target zones receive their own allocations'}} if p.weighting else {}
        (files/(a.name+'.spatial.json')).write_text(json_text({'schema':'mapgis-agent/spatial-summary/1','native_sha256':sha(after),
            'sources':sources,'parameters':payload['parameters'],'frame':frame,'metrics':metrics,'matches':mapping,**weighting,
            'unmatched_right_record_ids':sorted(set(bid)-set(owners))}),encoding='utf8')
        with (files/'statistics.csv').open('x',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=['physical_record_id']+[c.name for c in p.columns]);writer.writeheader()
            writer.writerows({'physical_record_id':rid,**updates[str(rid)]} for rid in aid)
        return finish_bundle(bridge,request_id,files,{'backend':'GEOS STRtree, Python statistics, existing native schema/attribute editors',
            'metrics':metrics,'predicate_direction':'left geometry against right geometry','coordinate_frame':frame,'units':units,**weighting,
            'source_preserved':True,'physical_record_ids_preserved':True,'non_attribute_sections_byte_identical':True,
            'native_save_reopen':'separate native operation; not inferred from file readback'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
