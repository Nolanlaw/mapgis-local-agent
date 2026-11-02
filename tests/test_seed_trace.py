import cv2
import numpy as np
from pathlib import Path

from mapgis_agent.raster import RasterWorkspace


def fixture(tmp_path,image):
    p=tmp_path/'ink.png';p.write_bytes(cv2.imencode('.png',image)[1].tobytes())
    r=RasterWorkspace(tmp_path);r.register_scan(str(p),'scan');r.render_window('scan',[0,0,image.shape[1],image.shape[0]],'window',128)
    return r


def config(mode,anchors):return {'mode':mode,'role':'boundary','anchors':anchors,'rgb':[0,0,0],
    'color_tolerance':20,'snap_px':3,'stroke_width_px':[0,10],'gap_closing_px':0,'simplify_px':.1}


def test_seed_trace_stops_at_t_junction_and_guided_anchors_choose_the_route(tmp_path):
    from mapgis_agent.seed_trace import trace_scan
    image=np.full((64,64,3),255,np.uint8)
    cv2.line(image,(5,30),(58,30),(0,0,0),3);cv2.line(image,(30,5),(30,58),(0,0,0),3)
    raster=fixture(tmp_path,image)
    stopped=trace_scan(raster,'scan','window',config('until_junction',[[10.5,30.5],[20.5,30.5]]),'stop')
    assert stopped['trace_status']=='BRANCH' and len(stopped['choices'])>=2
    assert stopped['endpoint_preview'][0]<33
    guided=trace_scan(raster,'scan','window',config('guided',[[10.5,30.5],[55.5,30.5]]),'guided')
    assert guided['trace_status']=='GUIDED'
    import json
    geometry=json.loads(Path(guided['features']).read_text(encoding='utf8'))['features'][0]['geometry']
    assert geometry['coordinates'][0]==[10.5,30.5]
    assert geometry['coordinates'][-1]==[55.5,30.5]
    assert max(abs(p[1]-30.5) for p in geometry['coordinates'])<=1
    assert guided['qa']['gap_closing_applied'] is False


def test_seed_trace_reports_equal_routes_instead_of_silently_choosing_a_ring_side(tmp_path):
    from mapgis_agent.seed_trace import trace_scan
    image=np.full((64,64,3),255,np.uint8)
    cv2.rectangle(image,(10,10),(50,50),(0,0,0),1)
    raster=fixture(tmp_path,image)
    result=trace_scan(raster,'scan','window',config('guided',[[10.5,10.5],[50.5,50.5]]),'ambiguous')
    assert result['trace_status']=='AMBIGUOUS' and result['qa']['candidate_count']==2
    assert result['qa']['review_status']=='UNREVIEWED'


def test_seed_trace_does_not_treat_stair_step_diagonals_as_junctions(tmp_path):
    from mapgis_agent.seed_trace import trace_scan
    image=np.full((64,64,3),255,np.uint8)
    cv2.polylines(image,[np.array([[5,5],[30,10],[50,40]],np.int32)],False,(0,0,0),5)
    raster=fixture(tmp_path,image)
    result=trace_scan(raster,'scan','window',config('until_junction',[[12.5,6.5],[22.5,8.5]]),'curve')
    assert result['trace_status']=='END'
    assert result['endpoint_preview'][0]>45 and result['endpoint_preview'][1]>35
