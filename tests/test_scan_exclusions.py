import json
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import shape

from mapgis_agent.raster import RasterWorkspace
from mapgis_agent.candidates import extract_candidates


def test_explicit_symbol_occlusion_does_not_become_a_geological_color_region(tmp_path):
    image=np.zeros((64,64,3),np.uint8);image[:,:32]=[0,0,255];image[:,32:]=[255,0,0]
    image[10:19,10:19]=[0,255,0]
    p=tmp_path/'scan.png';p.write_bytes(cv2.imencode('.png',image)[1].tobytes())
    raster=RasterWorkspace(tmp_path);raster.register_scan(str(p),'scan');raster.render_window('scan',[0,0,64,64],'window',64)
    config={'mode':'areas','palette':[{'id':'red','rgb':[255,0,0]},{'id':'blue','rgb':[0,0,255]},{'id':'green','rgb':[0,255,0]}],
            'color_tolerance':5,'min_area_px':0,'max_fill_px':0,'exclusion_fill_px':10,
            'exclude_regions':[{'geometry':{'type':'Polygon','coordinates':[[[10,10],[19,10],[19,19],[10,19],[10,10]]]},
                                'reason':'separate point symbol'}]}
    result=extract_candidates(raster,'scan','window',config,'areas')
    features=json.loads(Path(result['features']).read_text(encoding='utf8'))['features']
    areas={f['properties']['class_id']:shape(f['geometry']).area for f in features}
    assert areas=={'red':2048,'blue':2048}
    assert result['qa']['excluded_pixels']==81 and result['qa']['exclusion_filled_pixels']==81
    assert result['qa']['exclusion_policy']=='explicit occlusions filled from nearest visible palette; inferred pixels are recorded'
