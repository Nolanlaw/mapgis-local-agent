"""Seed following must stop at branches and retain explicit reversible decisions."""
import cv2,json
import numpy as np
import pytest
from mapgis_agent.raster import RasterWorkspace
from mapgis_agent.seed_following import seed_trace
from mapgis_agent.core import BridgeError


def fixture(tmp_path,image):
    file=tmp_path/'input.png';cv2.imencode('.png',cv2.cvtColor(image,cv2.COLOR_RGB2BGR))[1].tofile(str(file))
    service=RasterWorkspace(tmp_path/'project');service.register_scan(str(file),'scan')
    window=service.render_window('scan',[0,0,image.shape[1],image.shape[0]],'window',max(image.shape[:2]))
    return service,window['output_sha256']


def base(seed):return {'seed':seed,'rgb_min':[180,0,0],'rgb_max':[255,30,30]}


def test_crossing_requires_explicit_branch_then_rewind_can_choose_another(tmp_path):
    image=np.full((41,61,3),255,np.uint8)
    cv2.line(image,(5,20),(55,20),(255,0,0),1);cv2.line(image,(30,5),(30,35),(255,0,0),1)
    service,fingerprint=fixture(tmp_path,image);parameters=base([5.5,20.5])
    first=seed_trace(service,'scan','window',fingerprint,parameters)
    assert first['state']=='PAUSED_BRANCH' and len(first['choices'])==3
    assert first['geometry']['coordinates'][-1]==[30.5,20.5]
    branch=next(c['id'] for c in first['choices'] if c['end_pixel'][1]==5.5)
    selected=seed_trace(service,'scan','window',fingerprint,parameters,[branch],first['graph_sha256'])
    assert selected['state']=='ENDPOINT' and selected['geometry']['coordinates'][-1]==[30.5,5.5]
    undone=seed_trace(service,'scan','window',fingerprint,parameters,[branch],first['graph_sha256'],1)
    assert undone['state']=='PAUSED_STEP_LIMIT' and undone['geometry']==first['geometry']
    other=next(c['id'] for c in first['choices'] if c['end_pixel'][0]==55.5)
    changed=seed_trace(service,'scan','window',fingerprint,parameters,[other],first['graph_sha256'])
    assert changed['geometry']['coordinates'][-1]==[55.5,20.5]
    assert service._load('scan')['source_sha256']==first['source_sha256']
    with pytest.raises(BridgeError,match='TRACE_GRAPH_CHANGED'):
        seed_trace(service,'scan','window',fingerprint,{**parameters,'rgb_min':[190,0,0]},[branch],first['graph_sha256'])


def test_color_width_and_explicit_gap_bridge_never_silently_connect_text(tmp_path):
    image=np.full((51,81,3),255,np.uint8)
    cv2.line(image,(5,10),(30,10),(255,0,0),1);cv2.line(image,(34,10),(70,10),(255,0,0),1)
    cv2.line(image,(5,35),(70,35),(255,0,0),9);cv2.putText(image,'X',(40,25),0,.5,(0,0,0),1)
    service,fingerprint=fixture(tmp_path,image)
    first=seed_trace(service,'scan','window',fingerprint,{**base([5.5,10.5]),'max_halfwidth_px':2.})
    assert first['state']=='ENDPOINT' and first['geometry']['coordinates'][-1]==[30.5,10.5]
    linked=seed_trace(service,'scan','window',fingerprint,{**base([5.5,10.5]),'max_halfwidth_px':2.,'bridges':[
        {'start':[30.5,10.5],'end':[34.5,10.5],'reason':'explicit inspected scan break'}]})
    assert linked['geometry']['coordinates'][-1]==[70.5,10.5] and linked['explicit_gap_bridges_used']==1
    assert linked['automatic_gap_closing'] is False
    with pytest.raises(BridgeError,match='TRACE_SEED_NOT_ON_LINE'):
        seed_trace(service,'scan','window',fingerprint,{**base([40.5,35.5]),'snap_radius_px':1.,'max_halfwidth_px':2.})


def test_interior_seed_both_directions_and_pixel_offsets_are_explicit(tmp_path):
    image=np.full((20,30,3),255,np.uint8);cv2.line(image,(2,10),(25,10),(255,0,0),1)
    service,fingerprint=fixture(tmp_path,image)
    first=seed_trace(service,'scan','window',fingerprint,base([10.5,10.5]))
    assert first['state']=='PAUSED_BRANCH' and first['geometry'] is None and len(first['choices'])==2
    assert first['coordinate_frame']=='source_pixel'
    first_choice=first['choices'][0]['id']
    with pytest.raises(BridgeError,match='TRACE_GRAPH_HASH_REQUIRED'):
        seed_trace(service,'scan','window',fingerprint,base([10.5,10.5]),[first_choice])
    with pytest.raises(BridgeError,match='TRACE_UNUSED_DECISIONS'):
        seed_trace(service,'scan','window',fingerprint,base([2.5,10.5]),['bad'],first['graph_sha256'])


def test_closed_loop_from_interior_seed_keeps_whole_loop(tmp_path):
    image=np.full((35,45,3),255,np.uint8);cv2.rectangle(image,(5,5),(35,25),(255,0,0),1)
    service,fingerprint=fixture(tmp_path,image);parameters=base([15.5,5.5])
    initial=seed_trace(service,'scan','window',fingerprint,parameters)
    assert len(initial['choices'])==2
    for option in initial['choices']:
        traced=seed_trace(service,'scan','window',fingerprint,parameters,[option['id']],initial['graph_sha256'])
        coords=traced['geometry']['coordinates']
        assert coords[0]==coords[-1]==[15.5,5.5]
        assert len(set(map(tuple,coords)))==100 and traced['state']=='ENDPOINT'
