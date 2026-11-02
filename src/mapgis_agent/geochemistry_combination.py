"""Explicit k-of-n anomaly footprints and sampled descriptive feature indices."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import re
import shutil
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,StrictFloat,StrictInt
from shapely import STRtree,get_num_coordinates,normalize,union_all
from shapely.geometry import mapping
from shapely.ops import polygonize

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .attributes import prepare_attribute_schema
from .core import Bridge,BridgeError,json_text,read_native,scalar
from .creation import prepare_creation
from .geochemistry_statistics import _write_workbook
from .raster import explicit_crs,file_sha
from .spatial import _frame,_source
from .spatial_summary import SpatialSource,prepare_spatial_summary


class ElementAnomaly(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    name:str=Field(min_length=1,max_length=16)
    regions:SpatialSource
    value_field:str
    unit:str|None
    threshold:StrictFloat=Field(gt=0)
    background:StrictFloat|None=Field(default=None,gt=0)
    weight:StrictFloat=Field(default=1.,gt=0)


class CombinationParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    domain_id:str=Field(min_length=1,max_length=120)
    minimum_elements:StrictInt=Field(ge=1,le=32)
    boundary:Literal['strict_interior','unique_inclusive','all_inclusive']
    comparison:Literal['gt','ge']
    strength_rule:Literal['weighted_mean_member_ratio','weighted_sum_member_ratio','maximum_weighted_member_ratio']
    undefined_numeric_value:StrictFloat=Field(lt=0)
    sample_id_field:str|None=None
    concentration_centres:Literal['all_observed_maxima']|None=None
    max_regions:StrictInt=Field(default=5000,ge=1,le=10000)
    max_pairs:StrictInt=Field(default=500000,ge=1,le=500000)


def _factor(frame):
    crs=explicit_crs(frame)
    if crs is None:factor={'metre':1.,'millimetre':.001}.get(frame.get('unit'))
    else:
        factor=(crs.axis_info[0].unit_conversion_factor if crs.is_projected and len(crs.axis_info)>=2
            and math.isclose(crs.axis_info[0].unit_conversion_factor,crs.axis_info[1].unit_conversion_factor,rel_tol=1e-15) else None)
    if factor is None or not math.isfinite(factor) or factor<=0:
        raise BridgeError('COMBINATION_LINEAR_FRAME: known projected/local linear units required; no square degrees or inferred units')
    return float(factor)


def _regions(unions,p):
    # Dissolve each element first. Overlapping/duplicate records never count as
    # another element. Polygonize the noded boundary arrangement, including holes.
    result=[]
    for geometry in polygonize(union_all([g.boundary for g in unions])):
        point=geometry.representative_point();members=[i for i,g in enumerate(unions) if g.covers(point)]
        if len(members)<p.minimum_elements:continue
        if not geometry.is_valid or geometry.area<=0:raise BridgeError('COMBINATION_GEOMETRY')
        result.append((geometry,members))
        if len(result)>p.max_regions:raise BridgeError('COMBINATION_LIMIT: split the domain or use an explicit larger bounded region budget')
    return sorted(result,key=lambda row:(*row[0].bounds,tuple(row[1]),normalize(row[0]).wkb_hex))


def _finite(value):
    if not math.isfinite(value):raise BridgeError('COMBINATION_NUMERIC_RANGE')
    return float(value)


def _strength(values,elements,members,rule):
    if not values:return None
    weights=[elements[i].weight for i in members]
    try:
        if rule=='weighted_mean_member_ratio':
            peak=max(weights);weights=[w/peak for w in weights];total=math.fsum(weights)
            result=math.fsum(v*(w/total) for v,w in zip(values,weights))
        elif rule=='weighted_sum_member_ratio':result=math.fsum(v*w for v,w in zip(values,weights))
        else:result=max(v*w for v,w in zip(values,weights))
    except (ValueError,OverflowError) as exc:raise BridgeError('COMBINATION_NUMERIC_RANGE') from exc
    return _finite(result)


def prepare_combination(bridge,samples,elements,parameters,template,request_id):
    samples=SpatialSource.model_validate(samples).model_dump()
    p=parameters if isinstance(parameters,CombinationParameters) else CombinationParameters.model_validate(parameters)
    elements=[e if isinstance(e,ElementAnomaly) else ElementAnomaly.model_validate(e) for e in elements]
    if (not 1<=len(elements)<=32 or p.minimum_elements>len(elements)
        or len({e.name for e in elements})!=len(elements) or len({e.value_field for e in elements})!=len(elements)
        or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,15}',e.name) or e.unit is not None and not e.unit.strip() for e in elements)):
        raise BridgeError('COMBINATION_ELEMENTS: distinct element labels/measurement fields; 1..32 explicit threshold/unit/weight definitions')
    name=template.get('name','');scale=template.get('map_scale')
    if (set(template)-{'name','crs','map_scale','style'} or not {'name','crs','map_scale'}<=set(template)
        or not isinstance(name,str) or Path(name).name!=name or Path(name).suffix.lower()!='.wp'
        or any(c in name for c in '/\\:<>"|?*') or name.rstrip('. ')!=name
        or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}
        or type(scale) not in (int,float)
        or not math.isfinite(scale) or scale<=0):raise BridgeError('COMBINATION_TEMPLATE')
    all_refs=[samples,*[e.regions.model_dump() for e in elements]]
    if sum(Path(ref['file']).stat().st_size for ref in all_refs)>1024**3:raise BridgeError('COMBINATION_LIMIT: source bytes exceed1GiB; split the inputs')
    sample_file,reader,sample_ids,points=_source(samples)
    if reader.shapeType!='POINT' or not 1<=len(points)<=200000 or len(points)*len(elements)>500000:
        raise BridgeError('COMBINATION_LIMIT: nonempty WT anchors, at most200000samples and500000selected sample-element values')
    if p.sample_id_field is not None and p.sample_id_field not in reader.data.columns:raise BridgeError('COMBINATION_FIELD: sample ID field missing')
    _frame(reader,reader,template['crs']);factor=_factor(template['crs'])
    columns=[e.value_field for e in elements]
    if any(field not in reader.data.columns for field in columns):raise BridgeError('COMBINATION_FIELD: measurement field missing')
    if any(str(field).startswith('__CM') for field in reader.data.columns):raise BridgeError('COMBINATION_FIELD: __CM prefix belongs to derived flags')
    values=[[scalar(reader.data.iloc[i][field]) for field in columns] for i in range(len(points))]
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for row in values for v in row):
        raise BridgeError('COMBINATION_MEASUREMENTS: finite nonnegative exact values required; resolve censored/missing/sentinel results explicitly, never substitute zero')
    unions=[];source_records=[];vertices=0
    for e in elements:
        file,r,ids,geometries=_source(e.regions.model_dump());_frame(reader,r,template['crs'])
        if r.shapeType!='POLYGON' or not ids:raise BridgeError('COMBINATION_REGION_SOURCE: nonempty WP required for each element')
        vertices+=sum(int(get_num_coordinates(g)) for g in geometries)
        if vertices>500000:raise BridgeError('COMBINATION_LIMIT: at most500000source vertices')
        merged=union_all(geometries)
        if merged.is_empty or not merged.is_valid:raise BridgeError('COMBINATION_GEOMETRY: invalid element union; no automatic repair')
        unions.append(merged);source_records.append((file,ids,geometries,STRtree(geometries)))
    faces=_regions(unions,p)
    if len(faces)*len(elements)>300000:raise BridgeError('COMBINATION_LIMIT: at most300000region-element rows')
    relations=[];relation_rows=[]
    for number,(geometry,members) in enumerate(faces,1):
        relation={}
        for i in members:
            file,ids,geometries,tree=source_records[i]
            owners=sorted(ids[int(j)] for j in tree.query(geometry,predicate='intersects') if geometry.intersection(geometries[int(j)]).area>0)
            relation[elements[i].name]=owners
            relation_rows.extend([number,elements[i].name,identity] for identity in owners)
            if len(relation_rows)>p.max_pairs:raise BridgeError('COMBINATION_LIMIT: source region relationships exceed max_pairs')
        relations.append(relation)
    sources=list({str(Path(ref['file']).resolve()):{'file':str(Path(ref['file']).resolve()),'sha256':ref['expected_sha256']} for ref in all_refs}.values())
    parameter_data=p.model_dump(mode='json')
    if p.concentration_centres is None:parameter_data.pop('concentration_centres')
    payload={'operation':'geochemistry.combination','protocol':2 if p.concentration_centres else 1,'samples':samples,'elements':[e.model_dump(mode='json') for e in elements],
        'parameters':parameter_data,'template':template}
    primary=name if faces else 'analysis.json'
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,primary)
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir();encoder=Bridge(stage/'encoder');features=[];matches=[];stats_reader=None;centres=[]
        sample_indices={rid:i for i,rid in enumerate(sample_ids)}
        if faces:
            base=[]
            for number,(geometry,members) in enumerate(faces,1):
                area=_finite(geometry.area*factor**2)
                base.append({'type':'Feature','id':str(number),'geometry':mapping(geometry),'properties':{
                    'C_ID':number,'DOMAIN':p.domain_id,'SUPPORT':len(members),'MEMBERS':';'.join(elements[i].name for i in members),
                    'AREA_M2':area,'AREA_KM2':area/1e6}})
            prepare_creation(encoder,{'type':'FeatureCollection','features':base},template,'geometry');geometry=encoder.apply('geometry')
            inputs=encoder.root/'work/input';inputs.mkdir(parents=True);copy=inputs/'samples.wt';shutil.copyfile(sample_file,copy)
            fields=[{'name':str(c),'source':str(c)} for c in reader.data.columns]
            fields.extend({'name':f'__CM{i:02d}','type':'int','default':0} for i in range(1,len(elements)+1))
            updates={str(rid):{f'__CM{j:02d}':int(value>=e.threshold if p.comparison=='ge' else value>e.threshold)
                for j,(value,e) in enumerate(zip(row,elements),1)} for rid,row in zip(sample_ids,values)}
            prepare_attribute_schema(encoder,str(copy),samples['expected_sha256'],fields,'sample-flags',attribute_updates=updates)
            flagged=encoder.apply('sample-flags')
            summary=[{'name':'N_SAMPLES','operation':'count'}]
            for i,e in enumerate(elements,1):
                for suffix,operation in [('MEAN','mean'),('MIN','min'),('MAX','max'),('SD','stddev')]:
                    summary.append({'name':f'E{i:02d}_{suffix}','operation':operation,'field':e.value_field,'empty_value':p.undefined_numeric_value})
                summary.append({'name':f'E{i:02d}_ABOVE','operation':'sum','field':f'__CM{i:02d}','empty_value':0.})
            prepare_spatial_summary(encoder,{'file':geometry['output'],'expected_sha256':geometry['candidate_sha256']},
                {'file':flagged['output'],'expected_sha256':flagged['candidate_sha256']},
                {'predicate':'contains' if p.boundary=='strict_interior' else 'covers','match_policy':'unique' if p.boundary=='unique_inclusive' else 'all',
                    'crs':template['crs'],'nulls':'error','columns':summary,'max_pairs':p.max_pairs},'summary')
            summarized=encoder.apply('summary');stats_reader=read_native(Path(summarized['output']))[1]
            mapping_file=Path(summarized['output']).with_name(name+'.spatial.json')
            spatial=json.loads(mapping_file.read_text(encoding='utf8'));matches=spatial['matches']
            fields=[{'name':str(c),'source':str(c)} for c in stats_reader.data.columns]
            derived=['STRENGTH',*[f'E{i:02d}_{s}' for i in range(1,len(elements)+1) for s in ('RATIO','SCALE','RNAP')]]
            fields.extend({'name':c,'type':'double','default':p.undefined_numeric_value} for c in derived)
            out_updates={}
            for index,((polygon,members),relation) in enumerate(zip(faces,relations)):
                number=index+1;n=int(stats_reader.data['N_SAMPLES'].iloc[index]);area=_finite(polygon.area*factor**2);km2=area/1e6
                row={'id':number,'members':[elements[i].name for i in members],'source_records':relation,'area_m2':area,'area_km2':km2,
                    'sample_count':n,'sample_record_ids':matches[index]['right_record_ids'],'elements':{},'strength':None,
                    'status':'NO_SAMPLES' if n==0 else 'ONE_SAMPLE' if n==1 else 'REPORTED'}
                native={}
                for i,e in enumerate(elements,1):
                    prefix=f'E{i:02d}_';mean=float(stats_reader.data[prefix+'MEAN'].iloc[index]) if n else None
                    ratio=_finite(mean/e.threshold) if mean is not None else None
                    scale_value=_finite(ratio*km2) if ratio is not None else None
                    rnap=_finite(((mean-e.background)/e.background)*km2) if mean is not None and e.background is not None else None
                    row['elements'][e.name]={'unit':e.unit,'threshold':e.threshold,'background':e.background,'weight':e.weight,
                        'mean':mean,'minimum':float(stats_reader.data[prefix+'MIN'].iloc[index]) if n else None,
                        'maximum':float(stats_reader.data[prefix+'MAX'].iloc[index]) if n else None,
                        'sample_sd':float(stats_reader.data[prefix+'SD'].iloc[index]) if n>1 else None,
                        'above_threshold':int(stats_reader.data[prefix+'ABOVE'].iloc[index]),
                        'ratio_to_threshold':ratio,'scale_threshold_km2':scale_value,'rnap_km2':rnap}
                    if p.concentration_centres and i-1 in members:
                        highest=row['elements'][e.name]['maximum']
                        maxima=[rid for rid in row['sample_record_ids'] if values[sample_indices[rid]][i-1]==highest] if n else []
                        row['elements'][e.name]['maximum_sample_record_ids']=maxima
                        if len(centres)+len(maxima)>p.max_pairs:raise BridgeError('COMBINATION_LIMIT: concentration centre rows exceed max_pairs')
                        for rid in maxima:
                            anchor=points[sample_indices[rid]]
                            centres.append([number,e.name,rid,highest,anchor.x,anchor.y])
                    native.update({prefix+'RATIO':ratio,prefix+'SCALE':scale_value,prefix+'RNAP':rnap})
                ratios=[row['elements'][elements[i].name]['ratio_to_threshold'] for i in members] if n else []
                row['strength']=_strength(ratios,elements,members,p.strength_rule);native['STRENGTH']=row['strength']
                if any(v is not None and v==p.undefined_numeric_value for v in native.values()):raise BridgeError('COMBINATION_UNDEFINED_COLLISION: choose another negative undefined_numeric_value')
                out_updates[str(number)]={k:p.undefined_numeric_value if v is None else v for k,v in native.items()};features.append(row)
            prepare_attribute_schema(encoder,summarized['output'],summarized['candidate_sha256'],fields,'indices',attribute_updates=out_updates)
            final=encoder.apply('indices');target=folder/name;shutil.copyfile(final['output'],target)
            checked=read_native(target)[1]
            if len(checked.geom)!=len(faces) or any(not g.equals(face[0]) for g,face in zip(checked.geom,faces)):
                raise BridgeError('COMBINATION_GEOMETRY_READBACK')
        if centres:
            centre_name=Path(name).stem+'-centres.wt'
            centre_features=[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[row[4],row[5]]},
                'properties':{'C_ID':row[0],'ELEMENT':row[1],'SOURCE_ID':row[2],'VALUE':row[3]},
                'style':{'point_type':2,'radius_mm':1.,'filled':1,'color':1,'border_color':1,'pen_width':.2}}
                for i,row in enumerate(centres,1)]
            prepare_creation(encoder,{'type':'FeatureCollection','features':centre_features},
                {'name':centre_name,'crs':template['crs'],'map_scale':template['map_scale']},'centres')
            created=encoder.apply('centres');shutil.copyfile(created['output'],folder/centre_name)
        sample_owners={rid:[] for rid in sample_ids}
        for match in matches:
            for rid in match['right_record_ids']:sample_owners[rid].append(match['left_record_id'])
        boundary=[]
        if faces:
            boundaries=STRtree([g.boundary for g,_ in faces])
            boundary=[rid for rid,point in zip(sample_ids,points) if len(boundaries.query(point,predicate='intersects'))]
        report={'schema':'mapgis-agent/geochemistry-combination/1','sources':sources,'samples':samples,'elements':[e.model_dump(mode='json') for e in elements],
            'parameters':parameter_data,'coordinate_frame':template['crs'],'metres_per_coordinate_unit':factor,'features':features,
            'metrics':{'regions':len(faces),'sample_records':len(points),'assigned_sample_records':sum(bool(v) for v in sample_owners.values()),
                'unassigned_sample_records':sum(not v for v in sample_owners.values()),'multiply_assigned_samples':sum(len(v)>1 for v in sample_owners.values()),
                'boundary_samples':len(boundary),'area_m2':_finite(math.fsum(f['area_m2'] for f in features))},
            'boundary_sample_record_ids':boundary,'unassigned_sample_record_ids':[rid for rid,v in sample_owners.items() if not v],
            'multiply_matched_sample_record_ids':[rid for rid,v in sample_owners.items() if len(v)>1],
            'statistics_scope':'All physical sample records in the declared domain are retained as inputs; only declared polygon memberships enter descriptive statistics. Replicates are not adjudicated or silently deduplicated; all WT anchors are explicitly sample positions.',
            'geometry_rule':'Union per element, polygonize noded boundaries, retain faces covered by at least minimum_elements distinct elements. No buffering, repair, threshold fitting, interpolation or inferred geological truth.',
            'formulas':{'ratio_to_threshold':'sample mean / declared threshold','scale_threshold_km2':'ratio_to_threshold * component area_km2',
                'rnap_km2':'(sample mean - declared background) / declared background * component area_km2; null if background not supplied',
                'strength':p.strength_rule+' of member element ratios only; supplied positive weights, renormalized within each face for the mean'},
            'undefined_statistics':'JSON/XLSX null; native double fields use the declared negative undefined_numeric_value, N_SAMPLES determines mean availability and SD needs N>=2. Measured0 stays0. RNAP additionally requires an explicit background.',
            'unit_scope':'Thresholds/backgrounds use the same literal measurement scale as the matching sample field; units are caller declarations or explicit null. No implicit unit, dry/wet, oxide/element or density conversion.',
            'interpretation_scope':'Descriptive sampled anomaly indices, not ore probability, physical metal tonnage, resource/reserve classification or verified mineralization.',
            'native_output':name if faces else 'NONE_EMPTY_COMBINATION','source_preserved':True}
        if p.concentration_centres:
            report['concentration_centres']={'definition':'All physical samples attaining the observed maximum for each MEMBER element within each declared face; all exact ties retained. No interpolation, geometric-centroid substitution or inferred deposit centre.',
                'columns':['component_id','element','physical_record_id','value','X','Y'],'rows':centres,
                'native_output':centre_name if centres else 'NONE_NO_OBSERVED_CENTRES'}
        input_rows=[[rid,scalar(reader.data.iloc[i][p.sample_id_field]) if p.sample_id_field else None,point.x,point.y,*values[i]] for i,(rid,point) in enumerate(zip(sample_ids,points))]
        feature_columns=['component_id','members','area_m2','area_km2','sample_count','strength','status']
        feature_rows=[[f['id'],';'.join(f['members']),f['area_m2'],f['area_km2'],f['sample_count'],f['strength'],f['status']] for f in features]
        element_columns=['component_id','element','member','unit','threshold','background','weight','mean','minimum','maximum','sample_sd','above_threshold','ratio_to_threshold','scale_threshold_km2','rnap_km2']
        element_rows=[[f['id'],e.name,e.name in f['members'],*[f['elements'][e.name][c] for c in element_columns[3:]]] for f in features for e in elements]
        match_rows=[[f['id'],rid] for f in features for rid in f['sample_record_ids']]
        sheets={'InputSamples':[['physical_record_id','declared_sample_id','X','Y',*columns],*input_rows],
            'Features':[feature_columns,*feature_rows],'Elements':[element_columns,*element_rows],
            'SampleMembership':[['component_id','physical_record_id'],*match_rows],
            'RegionLineage':[['component_id','element','source_physical_record_id'],*relation_rows]}
        if p.concentration_centres:sheets['ConcentrationCentres']=[report['concentration_centres']['columns'],*centres]
        _write_workbook(folder/'analysis.xlsx',sheets)
        for file,header,rows in [('features.csv',feature_columns,feature_rows),('elements.csv',element_columns,element_rows)]:
            with (folder/file).open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(header)
                writer.writerows([[json_text(v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row] for row in rows])
        (folder/'analysis.json').write_text(json_text(report),encoding='utf8')
        return finish_bundle(bridge,request_id,folder,{'backend':'GEOS and existing native creation/spatial-summary/attribute-schema writers',
            'metrics':report['metrics'],'native_output':report['native_output'],'source_preserved':True,
            'native_save_reopen':'separate original native operation, not inferred from parser readback'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
