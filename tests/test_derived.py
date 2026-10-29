import csv
from pathlib import Path

import pytest
from shapely.geometry import Polygon,box,mapping

from mapgis_agent.core import Bridge,BridgeError,read_native,sha,annotation_text,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.derived import prepare_annotations,prepare_seams,prepare_attribute_report

LOCAL={'kind':'local','name':'seam analytic','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}


def native(bridge,geometry,identity,code='T1'):
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':mapping(geometry),
        'properties':{'ID':7,'code':code,'name':'地层'}}]},
        {'name':'source.wp','crs':LOCAL,'map_scale':1000},identity)
    source=Path(bridge.apply(identity)['output'])
    return source,sha(source.read_bytes())


def test_polygon_annotation_uses_interior_not_centroid_in_hole(tmp_path):
    bridge=Bridge(tmp_path);polygon=Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(8,2),(8,8),(2,8)]])
    source,digest=native(bridge,polygon,'area')
    prepare_annotations(bridge,str(source),digest,'code',{'name':'labels.wt','crs':LOCAL,'map_scale':1000},'labels')
    blob,reader,sections=read_native(Path(bridge.apply('labels')['output']))
    assert len(reader.geom)==1 and polygon.contains(reader.geom[0])
    assert annotation_text(blob,sections,native_ids(reader)[0])=='T1'
    assert reader.data['ID'].tolist()==[7] and sha(source.read_bytes())==digest


def test_seam_grid_dissolves_same_class_and_retains_other_classes(tmp_path):
    bridge=Bridge(tmp_path);a,ha=native(bridge,box(0,0,10.004,10),'a');b,hb=native(bridge,box(10.006,0,20,10),'b')
    preview=prepare_seams(bridge,[{'file':str(a),'expected_sha256':ha},{'file':str(b),'expected_sha256':hb}],
        {'field':'code','grid_size':.1},{'name':'joined.wp','crs':LOCAL,'map_scale':1000},'join')
    _,reader,_=read_native(Path(bridge.apply('join')['output']))
    assert len(reader.geom)==1 and reader.geom[0].area==pytest.approx(200) and reader.data['code'].tolist()==['T1']
    assert preview['qa']['source_files']==2 and sha(a.read_bytes())==ha and sha(b.read_bytes())==hb


def test_attribute_report_materializes_real_aggregate_as_json_and_csv(tmp_path):
    bridge=Bridge(tmp_path);source,digest=native(bridge,box(0,0,10,10),'area')
    prepare_attribute_report(bridge,str(source),digest,'SELECT code,COUNT(*) AS n FROM features GROUP BY code','report')
    result=bridge.apply('report')
    assert bridge.status('report')['output_matches'] and result['row_count']==1
    with Path(result['output']).with_suffix('.csv').open(encoding='utf-8-sig',newline='') as stream:
        assert list(csv.reader(stream))==[['code','n'],['T1','1']]
    assert sha(source.read_bytes())==digest
