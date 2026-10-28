import json

import cv2
import numpy as np
from shapely.geometry import Point, shape

from mapgis_agent.candidates import extract_candidates
from mapgis_agent.raster import RasterWorkspace


def test_seed_cleanup_removes_ink_islands_but_keeps_explicit_thin_class(tmp_path):
    colors = [[220, 180, 240], [245, 220, 180], [170, 230, 120]]
    image = np.full((60, 80, 3), colors[0], np.uint8)
    image[:, 40:] = colors[1]
    image[20:23, 15:18] = colors[1]  # Misclassified antialiased ink island.
    image[10:50, 30:31] = colors[2]  # Explicitly protected narrow body.
    original = tmp_path / 'scan.png'
    cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service = RasterWorkspace(tmp_path / 'project')
    service.register_scan(str(original), 'scan')
    service.render_window('scan', [0, 0, 80, 60], 'tile', 80)
    result = extract_candidates(service, 'scan', 'tile', {
        'mode': 'areas', 'palette': [{'id': str(i), 'rgb': c} for i, c in enumerate(colors)],
        'color_tolerance': 1, 'min_area_px': 1, 'max_fill_px': 4,
        'min_seed_area_px': 20, 'seed_erosion_px': 1, 'protected_classes': ['2'],
    }, 'clean')
    features = json.loads(open(result['features'], encoding='utf8').read())['features']
    at_ink = [f['properties']['class_id'] for f in features if shape(f['geometry']).covers(Point(16, 21))]
    at_body = [f['properties']['class_id'] for f in features if shape(f['geometry']).covers(Point(30.5, 30))]
    assert at_ink == ['0']
    assert at_body == ['2']
    assert result['qa']['seed_cleanup']['removed_pixels'] > 9
    assert result['qa']['seed_cleanup']['protected_classes'] == ['2']
    assert result['qa']['review_status'] == 'UNREVIEWED'
