import cv2
import numpy as np
import pytest

from mapgis_agent.raster import RasterWorkspace
from mapgis_agent.core import BridgeError

CRS={'kind':'local','name':'image test frame','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64}


def test_scan_match_keeps_crop_to_source_mapping_and_unused_checkpoints(tmp_path):
    from mapgis_agent.scan_match import match_scan_windows
    rng=np.random.default_rng(3917)
    reference=np.full((800,900,3),240,np.uint8)
    for i in range(200):
        x,y=rng.integers([15,15],[880,780]);radius=int(rng.integers(3,12))
        color=tuple(int(v) for v in rng.integers(0,180,3))
        cv2.circle(reference,(int(x),int(y)),radius,color,2)
        cv2.putText(reference,str(i),(int(x)+3,int(y)+2),cv2.FONT_HERSHEY_SIMPLEX,.3,color,1)
    a=tmp_path/'source.png';b=tmp_path/'reference.png'
    a.write_bytes(cv2.imencode('.png',reference[100:700,120:720])[1].tobytes())
    b.write_bytes(cv2.imencode('.png',reference)[1].tobytes())
    raster=RasterWorkspace(tmp_path)
    raster.register_scan(str(a),'source');raster.register_scan(str(b),'reference')
    raster.render_window('source',[0,0,600,600],'source-window',600)
    raster.render_window('reference',[0,0,900,800],'reference-window',900)
    raster.prepare_georeference('reference',CRS,[{'pixel':[x,y],'world':[10*x,8000-10*y]}
        for x,y in [(0,0),(900,0),(900,800),(0,800)]],'reference-frame',
        [{'pixel':[450,400],'world':[4500,4000]}],max_error=.001)
    match=match_scan_windows(raster,{'scan_id':'source','window_id':'source-window'},
        {'scan_id':'reference','window_id':'reference-window','georeference_id':'reference-frame'},
        {'ratio_threshold':.7,'ransac_threshold_px':.05,'min_inliers':20},'match')
    assert match['metrics']['inliers']>=50 and match['metrics']['rms_reference_preview_px']<.1
    assert match['metrics']['source_inlier_hull_fraction']>.5
    train={tuple(g['pixel']) for g in match['gcps']};checks={tuple(g['pixel']) for g in match['checkpoints']}
    assert len(train)>=3 and len(checks)>=3 and not train&checks
    result=raster.prepare_georeference('source',CRS,match['gcps'],'source-frame',match['checkpoints'],max_error=1)
    assert result['accepted'],{'residual':result['residual'],'max_check':max(p['error'] for p in result['checkpoints']),
                               'coefficients':result['coefficients'],'match':match['metrics']}
    assert result['coefficients']==pytest.approx([10,0,1200,0,-10,7000],abs=.2)


def test_scan_match_refuses_unaccepted_reference_frame(tmp_path):
    from mapgis_agent.scan_match import match_scan_windows
    raster=RasterWorkspace(tmp_path)
    blank=np.zeros((64,64,3),np.uint8);p=tmp_path/'blank.png';p.write_bytes(cv2.imencode('.png',blank)[1].tobytes())
    raster.register_scan(str(p),'blank');raster.render_window('blank',[0,0,64,64],'window',64)
    with pytest.raises(BridgeError,match='GEOREFERENCE_NOT_ACCEPTED'):
        match_scan_windows(raster,{'scan_id':'blank','window_id':'window'},
            {'scan_id':'blank','window_id':'window','georeference_id':'missing'},
            {'ratio_threshold':.7,'ransac_threshold_px':2,'min_inliers':20},'blank-match')
