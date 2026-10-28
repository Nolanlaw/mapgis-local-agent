import asyncio
import json

import cv2
import numpy as np
import pytest

from mapgis_agent.raster import RasterWorkspace
from mapgis_agent.server import build_server


def fixture_window(tmp_path, thickness=1):
    image=np.full((120,160,3),255,np.uint8)
    cv2.line(image,(20,20),(100,90),(255,0,0),thickness)
    cv2.circle(image,(125,35),15,(0,0,0),2)
    original=tmp_path / 'scan.png'
    cv2.imencode('.png',cv2.cvtColor(image,cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service=RasterWorkspace(tmp_path / 'project')
    service.register_scan(str(original),'scan')
    window=service.render_window('scan',[10,10,140,100],'tile',140)
    return service,window


def test_trace_is_real_mcp_tool_and_source_offsets_survive(tmp_path):
    names={t.name for t in asyncio.run(build_server(tmp_path).list_tools())}
    assert 'mapgis_extract_candidates' in names
    from mapgis_agent.candidates import extract_candidates
    service,window=fixture_window(tmp_path)
    config={'mode':'lines','role':'fault','rgb_min':[180,0,0],'rgb_max':[255,80,80],
            'min_length_px':30,'simplify_px':0.7}
    result=extract_candidates(service,'scan','tile',config,'red-faults')
    features=json.loads(open(result['features'],encoding='utf-8').read())['features']
    assert len(features)==1
    assert features[0]['properties']['role']=='fault'
    assert features[0]['properties']['review_status']=='UNREVIEWED'
    xs=[p[0] for p in features[0]['geometry']['coordinates']]
    assert min(xs)==pytest.approx(20.5,abs=1)
    assert max(xs)==pytest.approx(100.5,abs=1)
    assert extract_candidates(service,'scan','tile',config,'red-faults')==result


def test_area_shapes_use_pixel_edges_and_do_not_invent_classes(tmp_path):
    from mapgis_agent.candidates import extract_candidates
    image=np.full((20,30,3),(200,200,250),np.uint8)
    image[:,15:]=(240,180,180)
    original=tmp_path / 'scan.png'
    cv2.imencode('.png',cv2.cvtColor(image,cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service=RasterWorkspace(tmp_path / 'project')
    service.register_scan(str(original),'scan')
    service.render_window('scan',[0,0,30,20],'tile',30)
    result=extract_candidates(service,'scan','tile',{'mode':'areas','palette':[
        {'id':'formation-a','rgb':[200,200,250]}, {'id':'formation-b','rgb':[240,180,180]}],
        'color_tolerance':3,'max_fill_px':0,'min_area_px':1},'areas')
    from shapely.geometry import shape
    features=json.loads(open(result['features'],encoding='utf-8').read())['features']
    assert len(features)==2
    assert sorted(shape(f['geometry']).area for f in features)==[300,300]
    assert {f['properties']['class_id'] for f in features}=={'formation-a','formation-b'}
    assert result['qa']['unknown_pixels']==0


def test_trace_refuses_changed_window_or_unvalidated_georeference(tmp_path):
    from mapgis_agent.candidates import extract_candidates
    service,window=fixture_window(tmp_path)
    with pytest.raises(ValueError,match='GEOREFERENCE_NOT_ACCEPTED'):
        extract_candidates(service,'scan','tile',{'mode':'lines','role':'fault','georeference_id':'missing'},'bad')
    with open(window['output'],'ab') as out:
        out.write(b'changed')
    with pytest.raises(ValueError,match='STALE_WINDOW'):
        extract_candidates(service,'scan','tile',{'mode':'lines','role':'fault'},'changed')


def test_thick_stroke_endpoint_uncertainty_is_reported_for_review(tmp_path):
    from mapgis_agent.candidates import extract_candidates
    service,_=fixture_window(tmp_path,3)
    result=extract_candidates(service,'scan','tile',{'mode':'lines','role':'fault',
        'rgb_min':[180,0,0],'rgb_max':[255,80,80],'min_length_px':30},'thick')
    assert result['qa']['trace_endpoint_policy']=='REVIEW_THINNING_RETRACTION'
    assert result['qa']['review_status']=='UNREVIEWED'
    assert result['qa']['automatic_endpoint_extension'] is False


def test_area_simplification_keeps_matched_shared_edges_and_reports_measured_error(tmp_path):
    from mapgis_agent.candidates import extract_candidates
    from shapely import coverage_is_valid
    from shapely.geometry import shape
    image=np.full((80,100,3),(240,180,180),np.uint8)
    for y in range(80):
        image[y,:40+y//3]=(200,200,250)
    original=tmp_path/'scan.png'
    cv2.imencode('.png',cv2.cvtColor(image,cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service=RasterWorkspace(tmp_path/'project')
    service.register_scan(str(original),'scan')
    service.render_window('scan',[0,0,100,80],'tile',100)
    config={'mode':'areas','palette':[{'id':'a','rgb':[200,200,250]},{'id':'b','rgb':[240,180,180]}],
            'color_tolerance':1,'min_area_px':1,'coverage_simplify_px':1.5}
    result=extract_candidates(service,'scan','tile',config,'smooth')
    geoms=[shape(f['geometry']) for f in json.loads(open(result['features'],encoding='utf8').read())['features']]
    assert coverage_is_valid(geoms)
    assert geoms[0].intersection(geoms[1]).area==0
    assert geoms[0].boundary.intersection(geoms[1].boundary).length>70
    assert result['qa']['coverage_simplification']['vertices_after']<result['qa']['coverage_simplification']['vertices_before']
    assert 0<result['qa']['coverage_simplification']['max_hausdorff_px']<2
    assert result['qa']['review_status']=='UNREVIEWED'


def test_pixel_t_junctions_are_noded_before_shared_edge_simplification(tmp_path):
    from mapgis_agent.candidates import extract_candidates
    from shapely import coverage_is_valid
    from shapely.geometry import shape
    colors=[[200,200,250],[240,180,180],[180,240,180]]
    image=np.full((80,100,3),colors[0],np.uint8)
    image[:40,50:]=colors[1]
    image[40:,50:]=colors[2]
    original=tmp_path/'scan.png'
    cv2.imencode('.png',cv2.cvtColor(image,cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service=RasterWorkspace(tmp_path/'project')
    service.register_scan(str(original),'scan')
    service.render_window('scan',[0,0,100,80],'tile',100)
    result=extract_candidates(service,'scan','tile',{'mode':'areas','palette':[
        {'id':str(i),'rgb':c} for i,c in enumerate(colors)],'color_tolerance':1,
        'min_area_px':1,'coverage_simplify_px':1.5},'junction')
    geoms=[shape(f['geometry']) for f in json.loads(open(result['features'],encoding='utf8').read())['features']]
    assert coverage_is_valid(geoms)
    assert sum(g.area for g in geoms)==8000
    assert result['qa']['coverage_simplification']['pixel_grid_noded'] is True
