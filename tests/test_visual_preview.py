from pathlib import Path
import struct

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from mapgis_agent.core import BridgeError
from mapgis_agent.visual_preview import (FAULT, _read_png, _save, render_diff_heatmap,
                                         render_drc_crops, render_overview)
from test_drc_lvs import write_layer


def ppm(file):
    raw = Path(file).read_bytes()
    marker = raw.index(b'pHYs')
    return struct.unpack_from('>IIB', raw, marker + 4)


def test_overview_layer_order_cjk_marker_and_crops(tmp_path):
    wp = write_layer(tmp_path, 'geology.wp', [box(0, 0, 60, 48)])
    wl = write_layer(tmp_path, 'fault.wl', [LineString([(3, 3), (45, 30)])])
    wt = write_layer(tmp_path, 'units.wt', [Point(17, 22)], texts=['全新统 花岗岩'],
                     style={'height': 3., 'width': 3., 'angle': 4.})
    files = [wp, wl, wt]
    drc = {'violations': [{'rule_id': 'DRC_DANGLE', 'coords': [3., 3.], 'severity': 'WARNING'}]}
    output = tmp_path / 'overview.png'
    first = render_overview(files, output, width_px=820, height_px=620, drc_report=drc, title='区域地质图')
    assert first['layer_order'] == list(map(str, files)) and first['annotation_count'] == 1
    assert ppm(output) == (5906, 5906, 1)
    image = _read_png(output)
    assert image.shape == (620, 820, 3)
    x, y = first['markers'][0]['pixel']
    assert np.all(image[y, x] == FAULT)
    without = tmp_path / 'without-chinese.png'
    render_overview(files[:-1], without, width_px=820, height_px=620, drc_report=drc, title='区域地质图')
    # Compare the map interior away from the legend and header.
    delta = np.any(_read_png(without)[60:530, 80:640] != image[60:530, 80:640], axis=2)
    assert 70 < np.count_nonzero(delta) < 2500
    repeat = tmp_path / 'repeat.png'
    render_overview(files, repeat, width_px=820, height_px=620, drc_report=drc, title='区域地质图')
    assert output.read_bytes() == repeat.read_bytes()
    crops = render_drc_crops(files, drc, tmp_path / 'crops')
    assert len(crops) == 1 and ppm(crops[0]['path']) == (11811, 11811, 1)
    x, y = crops[0]['markers'][0]['pixel']
    assert np.all(_read_png(crops[0]['path'])[y, x] == FAULT)


def test_heatmap_numeric_metrics_and_size_guard(tmp_path):
    base = np.full((40, 50, 3), 120, dtype=np.uint8)
    after = base.copy()
    after[2:12, 8:13] = [110, 125, 140]
    path0, path1 = tmp_path / 'before.png', tmp_path / 'after.png'
    _save(base, path0, 150)
    _save(after, path1, 150)
    report = render_diff_heatmap(path0, path1, tmp_path / 'heatmap.png')
    assert report['changed_pixel_ratio'] == pytest.approx(50 / 2000)
    assert report['mean_abs_diff'] == pytest.approx(50 * (10 + 5 + 20) / 6000)
    assert report['max_diff'] == 20 and report['diff_bbox'] == [8, 2, 13, 12]
    assert tuple(_read_png(report['absolute_difference_png'])[2, 8]) == (10, 5, 20)
    assert np.array_equal(_read_png(report['path'])[0, 0], after[0, 0])
    with pytest.raises(BridgeError, match='PREVIEW_DIFF_OUTPUT'):
        render_diff_heatmap(path0, path1, path0)
    _save(after[:20], path1, 150)
    with pytest.raises(BridgeError, match='PREVIEW_DIFF_SIZE'):
        render_diff_heatmap(path0, path1, tmp_path / 'wrong-size.png')


def test_numpy_fallback_preserves_polygon_hole(tmp_path, monkeypatch):
    from mapgis_agent import visual_preview
    from shapely.geometry import Polygon
    monkeypatch.setattr(visual_preview, '_raster_accelerator', lambda: None)
    geom = Polygon(box(0, 0, 30, 30).exterior.coords, [box(10, 10, 20, 20).exterior.coords])
    layer = write_layer(tmp_path, 'hole.wp', [geom])
    result = render_overview([layer], tmp_path / 'fallback.png', width_px=600, height_px=470)
    raster = _read_png(result['path'])
    assert np.count_nonzero(np.all(raster == visual_preview.FILLS[0], axis=2)) > 2000
    assert tuple(raster[223, 272]) == (255, 255, 251)
