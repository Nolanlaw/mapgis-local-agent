"""Saved-file labels, class seams and attribute reports using existing engines."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from shapely.geometry import shape,mapping

from .core import Bridge,BridgeError,json_text,sha,read_native,require_native_crs
from .creation import prepare_creation
from .features import _native_collection
from .raster import explicit_crs
from .vector import prepare_vector_operation
from .exchange import query_attributes
from .artifacts import begin_bundle,finish_bundle,fail_bundle


def prepare_annotations(bridge:Bridge,file:str,expected_sha256:str,field:str,template:dict,request_id:str)->dict:
    if Path(template.get('name','')).suffix.lower()!='.wt':raise BridgeError('ANNOTATION_WT_REQUIRED')
    collection,source=_native_collection(file,expected_sha256,None)
    _,reader,_=read_native(Path(file));require_native_crs(reader,template.get('crs',{}),'DERIVED_CRS_MISMATCH')
    output=[]
    for f in collection['features']:
        if field not in f['properties'] or f['properties'][field] is None:raise BridgeError('ANNOTATION_FIELD_MISSING')
        geometry=shape(f['geometry'])
        point=geometry if geometry.geom_type=='Point' else geometry.interpolate(.5,normalized=True) if geometry.geom_type=='LineString' else geometry.representative_point()
        output.append({'type':'Feature','id':f['id'],'geometry':mapping(point),'properties':f['properties'],
            'text':str(f['properties'][field]),'style':template.get('style',{})})
    qa=dict(template.get('qa',{}))|{'native_source':source,'label_field':field,'placement':'point / line half-length / interior representative point'}
    return prepare_creation(bridge,{'type':'FeatureCollection','features':output},template|{'qa':qa},request_id)


def prepare_seams(bridge:Bridge,files:list[dict],parameters:dict,template:dict,request_id:str)->dict:
    if not isinstance(files,list) or not 2<=len(files)<=100:raise BridgeError('SEAM_FILES: 2..100 native inputs required')
    if set(parameters)-{'field','grid_size'} or 'field' not in parameters:raise BridgeError('SEAM_PARAMETERS')
    features=[];sources=[];kind=None
    for index,item in enumerate(files,1):
        if set(item)!={'file','expected_sha256'}:raise BridgeError('SEAM_SOURCE_PARAMETERS')
        collection,source=_native_collection(item['file'],item['expected_sha256'],None)
        _,reader,_=read_native(Path(item['file']));require_native_crs(reader,template.get('crs',{}),'DERIVED_CRS_MISMATCH')
        if kind is not None and reader.shapeType!=kind:raise BridgeError('SEAM_GEOMETRY_TYPES_DIFFER')
        kind=reader.shapeType;sources.append(source)
        features.extend({**f,'id':str(index)+':'+f['id']} for f in collection['features'])
    qa=dict(template.get('qa',{}))|{'native_sources':sources,'source_files':len(sources),
        'attribute_policy':'only the explicit class field retained; source mappings remain in QA','originals_deleted':False}
    return prepare_vector_operation(bridge,'geometry.dissolve',{'type':'FeatureCollection','features':features},
        template|{'qa':qa},parameters,request_id)


def prepare_attribute_report(bridge:Bridge,file:str,expected_sha256:str,sql:str,request_id:str,limit:int=100000)->dict:
    source=Path(file).resolve(strict=True)
    payload={'operation':'attributes.report','file':str(source),'expected_sha256':expected_sha256,'sql':sql,'limit':limit}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(source),'sha256':expected_sha256}],'report.json')
    if not new:return receipt
    try:
        result=query_attributes(str(source),expected_sha256,sql,limit)
        files=stage/'files';files.mkdir()
        (files/'report.json').write_text(json_text(result),encoding='utf8')
        with (files/'report.csv').open('x',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(result['columns']);writer.writerows(result['rows'])
        return finish_bundle(bridge,request_id,files,{'backend':'SQLite/Python csv','row_count':result['row_count'],
            'truncated':result['truncated'],'source_preserved':True,'query':sql})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
