import math

import pytest
from shapely.geometry import Point

from mapgis_agent.core import BridgeError
from mapgis_agent.label_placement import DIRECTIONS, Label, candidate_positions, compare_solvers, place_labels, text_box
from mapgis_agent.object_store import file_sha256
from test_drc_lvs import write_layer


def annotations(folder, name='labels.wt'):
    return write_layer(folder, name, [Point(12, 12), Point(12.5, 12.2), Point(12.9, 12.4), Point(14, 13)],
                       style={'height': 3., 'width': 3., 'angle': 0.},
                       texts=['全新统', 'K1 unit', '花岗岩', 'Qh'])


def test_ascii_cjk_rotation_and_eight_directional_candidates():
    ascii_label = Label(1, 'AA', 0, 0, 2, 2)
    cjk_label = Label(2, '花岗', 0, 0, 2, 2)
    ax = text_box(ascii_label, padding=0)
    cx = text_box(cjk_label, padding=0)
    assert cx[2] - cx[0] == 4 and ax[2] - ax[0] == pytest.approx(2.32)
    rotated = text_box(Label(3, '花岗', 0, 0, 2, 2, angle=90), padding=0)
    assert rotated == pytest.approx((-2, 0, 0, 4))
    positions = list(candidate_positions(cjk_label, steps=2))
    assert [p.name for p in positions[:8]] == [row[0] for row in DIRECTIONS]
    assert len({(p.dx, p.dy) for p in positions[:8]}) == 8
    assert math.hypot(positions[8].dx, positions[8].dy) > math.hypot(positions[0].dx, positions[0].dy)


def test_solvers_preserve_locked_and_emit_native_move_plan(tmp_path):
    source = annotations(tmp_path)
    digest = file_sha256(source)
    comparison = compare_solvers(source, digest, locked_ids=[1], seed=19, iterations=1200)
    greedy, anneal = (comparison[key] for key in ('greedy_local', 'simulated_annealing'))
    for report in comparison.values():
        assert report['overlaps_before'] >= 3
        assert report['overlaps_after'] < report['overlaps_before']
        assert report['resolution_rate'] > 0 and report['runtime_ms'] >= 0
        assert report['positions'][0]['dx'] == report['positions'][0]['dy'] == 0
        (patch,) = report['patch_plan']
        assert patch['operation'] == 'point.move' and patch['expected_sha256'] == digest
        assert set(patch) == {'operation', 'file', 'expected_sha256', 'parameters', 'request_id'}
        assert all(set(row) == {'record_id', 'x', 'y'} and row['record_id'] != 1
                   for row in patch['parameters']['updates'])
    assert anneal['overlaps_after'] <= greedy['overlaps_after']
    repeat = place_labels(source, digest, solver='simulated_annealing', locked_ids=[1], seed=19, iterations=1200)
    assert repeat['positions'] == anneal['positions']
    assert repeat['patch_plan'] == anneal['patch_plan']
    assert file_sha256(source) == digest
    with pytest.raises(BridgeError, match='locked_ids'):
        place_labels(source, locked_ids=[999])


def test_locked_unavoidable_overlaps_stay_visible(tmp_path):
    report = place_labels(annotations(tmp_path), locked_ids=[1, 2, 3, 4])
    assert report['patch_plan'] == []
    assert report['overlaps_before'] == report['overlaps_after'] == len(report['residual_overlaps']) > 0
