import numpy as np
import pytest
from pathlib import Path
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.tin_lines import _transport,TinLineOverlayInput

FRAME={'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}

def source(tmp_path,coords):
    bridge=Bridge(tmp_path/'line')
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'test',
        'properties':{'tag':'saved TIN face'},'style':{'color':6},'geometry':{'type':'LineString','coordinates':coords}}]},
        {'name':'source.wl','crs':FRAME,'map_scale':1000},'line')
    return Path(bridge.apply('line')['output'])

def model(shift=(0.,0.)):
    xyz=np.array([[0,0,0],[100,0,0],[100,100,0],[0,100,0],[50,50,20]],dtype='float64')
    xyz[:,:2]+=shift;faces=np.array([[0,1,4],[1,2,4],[2,3,4],[3,0,4]])
    edges={tuple(sorted((int(a),int(b)))):[] for face in faces for a,b in zip(face,np.roll(face,-1))}
    return xyz,faces,edges

def decode(blob):
    rows=blob.decode('ascii').splitlines();assert rows[0]=='MGTINWL1 1'
    identity,count=map(int,rows[1].split());assert identity==1 and len(rows)==count+2
    return np.array([list(map(float,v.split())) for v in rows[2:]])

def test_tin_drape_adds_each_facet_crossing_without_retriangulating(tmp_path):
    file=source(tmp_path,[[10.,50.],[90.,50.]])
    xyz,faces,edges=model();blob,profile=_transport(file,FRAME,[0.,0.,100.,100.],xyz,faces,edges,2.,FRAME)
    points=decode(blob)
    np.testing.assert_array_equal(points,[[10.,50.,6.],[50.,50.,22.],[90.,50.,6.]])
    assert profile['physical_record_ids']==[1] and profile['sampled_vertices']==3
    assert profile['height_offset_m']==2. and profile['retriangulated'] is False

def test_large_world_coordinates_become_precise_local_xyz_only_after_sampling(tmp_path):
    shift=[500000.125,3000000.375];file=source(tmp_path,[[shift[0]+10,shift[1]+50],[shift[0]+90,shift[1]+50]])
    xyz,faces,edges=model(shift)
    blob,profile=_transport(file,FRAME,[*shift,shift[0]+100,shift[1]+100],xyz,faces,edges,0.,FRAME)
    np.testing.assert_array_equal(decode(blob),[[10.,50.,4.],[50.,50.,20.],[90.,50.,4.]])
    assert profile['display_shift']==shift

def test_tin_drape_refuses_gaps_inside_bbox_instead_of_connecting_missing_faces(tmp_path):
    file=source(tmp_path,[[10.,75.],[90.,75.]])
    xyz,faces,edges=model();faces=faces[[1,3]]
    # Both endpoints are valid but middle crosses a genuine missing domain.
    with pytest.raises(BridgeError,match='OUTSIDE'):_transport(file,FRAME,[0.,0.,100.,100.],xyz,faces,edges,0.,FRAME)

def test_overlay_rejects_nan_height_offset():
    with pytest.raises(ValueError):TinLineOverlayInput(file='test.wl',expected_sha256='0'*64,coordinate_frame='native_metres',height_offset_m=float('nan'))
