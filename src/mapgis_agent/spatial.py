"""Bounded, deterministic GEOS spatial queries over saved native records."""
from __future__ import annotations

import math
from pathlib import Path

from shapely import STRtree

from .core import BridgeError,native_ids,read_native,scalar,sha
from .raster import explicit_crs,file_sha


PREDICATES={'intersects','within','contains','covers','covered_by','contains_properly',
            'touches','overlaps','crosses','equals','disjoint','dwithin','nearest'}


def _frame(ra,rb,supplied,distance_operation=False):
    ca,cb=ra.crs or None,rb.crs or None
    if (ca is None)!=(cb is None) or ca is not None and not ca.equals(cb):raise BridgeError('SPATIAL_CRS_MISMATCH')
    if ca is None:
        if not isinstance(supplied,dict) or supplied.get('kind')!='local':raise BridgeError('SPATIAL_LOCAL_FRAME_REQUIRED')
        explicit_crs(supplied);frame=supplied;units=supplied['unit']
    else:
        if supplied is not None:
            resolved=explicit_crs(supplied)
            if resolved is None or not resolved.equals(ca):raise BridgeError('SPATIAL_CRS_MISMATCH')
        frame={'definition':ca.to_wkt(),'axis_order':'xy'};units=ca.axis_info[0].unit_name
    if distance_operation and (ca is not None and not ca.is_projected or ca is None and units=='unknown'):
        raise BridgeError('SPATIAL_DISTANCE_UNITS: projected or explicitly known local linear units required')
    return frame,units


def _source(value):
    if not isinstance(value,dict) or set(value)!={'file','expected_sha256'}:raise BridgeError('SPATIAL_SOURCE: file and expected_sha256 required')
    path=Path(value['file']).resolve(strict=True);blob,reader,_=read_native(path)
    if sha(blob)!=value['expected_sha256']:raise BridgeError('STALE_SOURCE')
    ids=native_ids(reader);geometries=list(reader.geom)
    if any(g is None or g.is_empty or not g.is_valid or g.has_z or getattr(g,'has_m',False)
           or any(not math.isfinite(v) for v in g.bounds) for g in geometries):raise BridgeError('SPATIAL_GEOMETRY: valid finite 2D records required')
    return path,reader,ids,geometries


def spatial_query(left:dict,right:dict,parameters:dict,offset:int=0,limit:int=1000)->dict:
    """Read physical-ID pairs; tree visitation order never becomes result order.

    Query one left geometry at a time, stopping after the requested page plus
    one match. This avoids allocating the full Cartesian relationship matrix.
    A truncated query has an unknown total, not an invented zero/full count.
    """
    if (not isinstance(parameters,dict) or parameters.get('predicate') not in PREDICATES
            or set(parameters)-{'predicate','crs','distance','max_distance','include_attributes'}):raise BridgeError('SPATIAL_PARAMETERS')
    if type(offset) is not int or not 0<=offset<=1000000 or type(limit) is not int or not 1<=limit<=1000:raise BridgeError('SPATIAL_PAGE')
    predicate=parameters['predicate'];attributes=parameters.get('include_attributes',False)
    if type(attributes) is not bool:raise BridgeError('SPATIAL_ATTRIBUTES')
    if ('distance' in parameters and predicate!='dwithin') or ('max_distance' in parameters and predicate!='nearest'):raise BridgeError('SPATIAL_DISTANCE_PARAMETER')
    distance=parameters.get('distance');maximum=parameters.get('max_distance')
    if predicate=='dwithin' and (type(distance) not in (int,float) or not math.isfinite(distance) or distance<0):raise BridgeError('SPATIAL_DISTANCE')
    if maximum is not None and (type(maximum) not in (int,float) or not math.isfinite(maximum) or maximum<=0):raise BridgeError('SPATIAL_DISTANCE')
    a,ra,aid,ga=_source(left);b,rb,bid,gb=_source(right)
    # The pinned reader uses an empty string for an unreferenced local grid.
    frame,units=_frame(ra,rb,parameters.get('crs'),predicate in ('nearest','dwithin'))
    tree=STRtree(gb);rows=[];seen=0;truncated=False;scanned=0
    for i in sorted(range(len(aid)),key=lambda i:aid[i]):
        if not gb:break
        geometry=ga[i];scanned+=1
        if predicate=='nearest':
            indexes,distances=tree.query_nearest(geometry,max_distance=maximum,return_distance=True,all_matches=True)
            candidates=sorted(zip(map(int,indexes),map(float,distances)),key=lambda p:bid[p[0]])
        elif predicate=='disjoint':
            touching=set(map(int,tree.query(geometry,predicate='intersects')))
            candidates=((j,None) for j in sorted(range(len(bid)),key=lambda j:bid[j]) if j not in touching)
        else:
            indexes=tree.query(geometry,predicate='intersects' if predicate=='equals' else predicate,
                               **({'distance':distance} if predicate=='dwithin' else {}))
            candidates=((j,None) for j in sorted(map(int,indexes),key=lambda j:bid[j]) if predicate!='equals' or geometry.equals(gb[j]))
        for j,separation in candidates:
            seen+=1
            if seen<=offset:continue
            if len(rows)==limit:truncated=True;break
            row={'left_record_id':aid[i],'right_record_id':bid[j]}
            if predicate in ('nearest','dwithin'):
                if separation is None:separation=float(geometry.distance(gb[j]))
                if not math.isfinite(separation):raise BridgeError('SPATIAL_NONFINITE_DISTANCE')
                row['distance']=separation
            if attributes:
                row.update(left_attributes={str(k):scalar(v) for k,v in ra.data.iloc[i].items()},
                           right_attributes={str(k):scalar(v) for k,v in rb.data.iloc[j].items()})
            rows.append(row)
        if truncated:break
    if file_sha(a)!=left['expected_sha256'] or file_sha(b)!=right['expected_sha256']:raise BridgeError('STALE_DURING_SPATIAL_QUERY')
    return {'predicate':predicate,'sources':{'left':{'file':str(a),'sha256':left['expected_sha256']},
                                           'right':{'file':str(b),'sha256':right['expected_sha256']}},
            'pairs':rows,'returned':len(rows),'offset':offset,'limit':limit,'truncated':truncated,
            'total_pairs':None if truncated else seen,'next_offset':offset+len(rows) if truncated else None,
            'left_record_ids':sorted({row['left_record_id'] for row in rows}),
            'right_record_ids':sorted({row['right_record_id'] for row in rows}),
            'scanned_left_records':scanned,'coordinate_frame':frame,'units':units,'backend':'GEOS STRtree',
            'source_preserved':True,'identity':'physical record IDs; repeated attribute ID values remain separate',
            'nearest_ties':'all equidistant records, ordered by physical IDs' if predicate=='nearest' else None}


def polygon_overlay(left:dict,right:dict,parameters:dict)->dict:
    """Reuse GeoPandas' complete overlay algorithm; return an editable collection.

    Source references are always explicit. Attribute names and unmatched/null
    values require a caller-selected mapping instead of implicit truncation or
    collisions. An empty area result is valid and does not invent a native file.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely import normalize,unary_union
    from shapely.geometry import mapping
    from .creation import PARAMS
    operations={'intersection','union','symmetric_difference','difference','identity'}
    if (not isinstance(parameters,dict) or parameters.get('operation') not in operations
            or set(parameters)-{'operation','crs','fields','max_features'}):raise BridgeError('OVERLAY_PARAMETERS')
    maximum=parameters.get('max_features',100000)
    if type(maximum) is not int or not 1<=maximum<=100000:raise BridgeError('OVERLAY_OUTPUT_LIMIT')
    a,ra,aid,ga=_source(left);b,rb,bid,gb=_source(right)
    if ra.shapeType!='POLYGON' or rb.shapeType!='POLYGON':raise BridgeError('OVERLAY_POLYGON_FILES_REQUIRED')
    frame,units=_frame(ra,rb,parameters.get('crs'))
    fields=parameters.get('fields',[]);targets={'src_left','src_right'}
    if not isinstance(fields,list):raise BridgeError('OVERLAY_FIELDS')
    for field in fields:
        if (not isinstance(field,dict) or not {'side','field','target'}<=set(field)
                or set(field)-{'side','field','target','missing','null_value'} or field['side'] not in ('left','right')):raise BridgeError('OVERLAY_FIELDS')
        reader=ra if field['side']=='left' else rb
        if field['field'] not in reader.data.columns:raise BridgeError('OVERLAY_FIELD_NOT_FOUND')
        target=field['target']
        if not isinstance(target,str) or not target or target.casefold() in {str(t).casefold() for t in targets}:raise BridgeError('OVERLAY_FIELD_COLLISION')
        targets.add(target)
    first=gpd.GeoDataFrame({'src_left':aid},geometry=ga,crs=ra.crs or None)
    second=gpd.GeoDataFrame({'src_right':bid},geometry=gb,crs=rb.crs or None)
    result=gpd.overlay(first,second,how=parameters['operation'],keep_geom_type=True,make_valid=False)
    if len(result)>maximum:raise BridgeError('OVERLAY_OUTPUT_LIMIT: clip/tile the inputs before requesting a larger collection')
    lookup={'left':dict(zip(aid,range(len(aid)))),'right':dict(zip(bid,range(len(bid))))}
    rows=[]
    for _,row in result.iterrows():
        l=0 if 'src_left' not in row or pd.isna(row['src_left']) else int(row['src_left'])
        r=0 if 'src_right' not in row or pd.isna(row['src_right']) else int(row['src_right'])
        rows.append((l,r,row.geometry))
    rows.sort(key=lambda x:(x[0],x[1],normalize(x[2]).wkb_hex))
    features=[]
    for index,(l,r,geometry) in enumerate(rows,1):
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ('Polygon','MultiPolygon'):raise BridgeError('OVERLAY_INVALID_RESULT')
        properties={'src_left':l,'src_right':r}
        for field in fields:
            identity=l if field['side']=='left' else r
            if not identity:
                if 'missing' not in field:raise BridgeError('OVERLAY_MISSING_VALUE: specify a mapped value when this side has no owner; source reference zero stays explicit')
                value=field['missing']
            else:
                reader=ra if field['side']=='left' else rb
                value=scalar(reader.data.iloc[lookup[field['side']][identity]][field['field']])
                if value is None:
                    if 'null_value' not in field:raise BridgeError('OVERLAY_NULL_POLICY: specify an explicit mapped value for a source null')
                    value=field['null_value']
            properties[field['target']]=value
        style={}
        reader,identity,side=(ra,l,'left') if l else (rb,r,'right')
        if identity:
            style={k:scalar(v) for k,v in reader.polygon_params.iloc[lookup[side][identity]].items() if k in PARAMS['POLYGON']}
        features.append({'type':'Feature','id':'overlay-'+str(index),'geometry':mapping(geometry),'properties':properties,'style':style})
    if file_sha(a)!=left['expected_sha256'] or file_sha(b)!=right['expected_sha256']:raise BridgeError('STALE_DURING_OVERLAY')
    return {'operation':parameters['operation'],'collection':{'type':'FeatureCollection','features':features},
            'empty':not features,'feature_count':len(features),'coordinate_frame':frame,
            'sources':{'left':{'file':str(a),'sha256':left['expected_sha256']},'right':{'file':str(b),'sha256':right['expected_sha256']}},
            'qa':{'backend':'GeoPandas.overlay / GEOS','geometry_policy':'polygon areas; line/point-only contacts excluded',
                  'automatic_geometry_repair':False,'attribute_policy':'only explicit fields copied; src_left/src_right=0 means no owner on that side',
                  'native_attribute_types':'map explicitly with attribute_schema after creation when needed',
                  'style_policy':'left owner, otherwise right owner','planar_area_units':units+' squared',
                  'output_union_area':float(unary_union([g for _,_,g in rows]).area) if rows else 0},
            'source_preserved':True,'native_output':'pass non-empty collection to prepare_creation; empty area results have no fabricated native file'}
