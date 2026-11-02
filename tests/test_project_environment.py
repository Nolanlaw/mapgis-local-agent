from pathlib import Path

import cv2
import numpy as np
import pytest

from mapgis_agent.core import BridgeError, sha
from mapgis_agent.native_project import native_project_info, prepare_project_edit, prepare_project_plot
from test_native_project_edit import project_case


def test_native_project_environment_save_reopen_transform_and_source_preservation(tmp_path):
    bridge, project, _ = project_case(tmp_path)
    before = project.read_bytes()
    initial = native_project_info(tmp_path, str(project), sha(before))
    assert initial['environment']['scale'] == [1, 1]
    # R(90) * diag(2, 1) * (x + 10, y + 5) puts the original region
    # [0,10]^2 at [-15,-5] x [20,40]. This checks order as well as rotation.
    environment = {'translation': [10, 5], 'scale': [2, 1],
                   'rotation_degrees': 90, 'paper_mm': [120, 90]}
    prepared = prepare_project_edit(bridge, str(project), sha(before),
        [{'source_index': 3}], 'environment', environment=environment)
    saved = bridge.apply('environment')
    fresh = native_project_info(tmp_path, saved['output'], saved['candidate_sha256'])
    assert fresh['environment'] == environment
    assert prepared['native_execution']['pid'] != prepared['native_reopen']['pid']
    assert project.read_bytes() == before
    # The supplied view is in transformed project coordinates, while vector
    # coordinates and bytes remain in their original native frame.
    plotted = prepare_project_plot(bridge, saved['output'], saved['candidate_sha256'],
        {'native_bounds': [-20, 15, 0, 45], 'paper_mm': [100, 150],
         'coordinate_frame': 'native'}, 'environment-plot')
    output = bridge.apply('environment-plot')
    pixels = cv2.imdecode(np.frombuffer(Path(output['output']).with_name('preview.png').read_bytes(), np.uint8), 1)
    height, width = pixels.shape[:2]
    assert np.any(pixels[int(height*.50), int(width*.50)] < 245)
    assert np.all(pixels[int(height*.50), int(width*.10)] >= 245)
    assert np.all(pixels[int(height*.10), int(width*.50)] >= 245)
    assert plotted['environment'] == environment
    assert fresh['layers'][0]['sha256'] == initial['layers'][2]['sha256']
    # A later layer-only edit must retain the existing environment.
    retained = prepare_project_edit(bridge, saved['output'], saved['candidate_sha256'],
        [{'source_index': 1, 'state': 2}], 'retained-environment')
    assert retained['environment'] == environment


@pytest.mark.parametrize('environment', [
    {'scale': [0, 1]}, {'scale': [1, -1]}, {'translation': [float('nan'), 0]},
    {'rotation_degrees': float('inf')}, {'paper_mm': [0, 100]}, {'unknown': 1},
])
def test_invalid_project_environment_is_rejected_without_changing_original(tmp_path, environment):
    bridge, project, _ = project_case(tmp_path)
    before = project.read_bytes()
    with pytest.raises(BridgeError, match='PROJECT_ENVIRONMENT'):
        prepare_project_edit(bridge, str(project), sha(before),
            [{'source_index': 3}], 'bad-environment', environment=environment)
    assert project.read_bytes() == before
