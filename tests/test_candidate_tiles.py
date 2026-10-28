import json

import cv2
import numpy as np
import pytest
from shapely.geometry import shape

from mapgis_agent.candidates import extract_candidates, merge_candidate_tiles
from mapgis_agent.raster import RasterWorkspace


def tiled_scan(tmp_path):
    image = np.full((40, 100, 3), (200, 180, 240), np.uint8)
    image[20:, :] = (240, 210, 180)
    original = tmp_path / 'scan.png'
    cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tofile(str(original))
    service = RasterWorkspace(tmp_path / 'project')
    service.register_scan(str(original), 'scan')
    config = {'mode': 'areas', 'palette': [{'id': 'a', 'rgb': [200, 180, 240]},
        {'id': 'b', 'rgb': [240, 210, 180]}], 'color_tolerance': 1, 'min_area_px': 1}
    service.render_window('scan', [0, 0, 60, 40], 'left', 60)
    service.render_window('scan', [40, 0, 60, 40], 'right', 60)
    extract_candidates(service, 'scan', 'left', config, 'left-areas')
    extract_candidates(service, 'scan', 'right', config, 'right-areas')
    tiles = [{'request_id': 'left-areas', 'core': [0, 0, 50, 40]},
             {'request_id': 'right-areas', 'core': [50, 0, 50, 40]}]
    return service, tiles


def test_independently_extracted_overlapping_tiles_merge_without_duplicate_area(tmp_path):
    service, tiles = tiled_scan(tmp_path)
    result = merge_candidate_tiles(service, 'scan', tiles, 'merged')
    features = json.loads(open(result['features'], encoding='utf8').read())['features']
    assert len(features) == 2
    geoms = [shape(f['geometry']) for f in features]
    assert sorted(g.area for g in geoms) == [2000, 2000]
    assert geoms[0].intersection(geoms[1]).area == 0
    assert geoms[0].bounds[2] == geoms[1].bounds[2] == 100
    assert result['qa']['overlap_disagreement_px2'] == 0
    assert result['qa']['core_seam_disagreement_px2'] == 0
    assert result['qa']['edge_matched'] is True
    assert result['qa']['review_status'] == 'UNREVIEWED'
    assert merge_candidate_tiles(service, 'scan', tiles, 'merged') == result


def test_merge_rejects_overlapping_core_ownership_and_stale_output(tmp_path):
    service, tiles = tiled_scan(tmp_path)
    overlapping = [tiles[0], {**tiles[1], 'core': [49, 0, 51, 40]}]
    with pytest.raises(ValueError, match='CORE_OVERLAP'):
        merge_candidate_tiles(service, 'scan', overlapping, 'bad')
    candidate = service.root / 'work/candidates/right-areas/features.geojson'
    candidate.write_bytes(candidate.read_bytes() + b' ')
    with pytest.raises(ValueError, match='CANDIDATE_OUTPUT_CONFLICT'):
        merge_candidate_tiles(service, 'scan', tiles, 'stale')
