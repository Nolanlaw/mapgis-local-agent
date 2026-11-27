import csv
import json
import math
from pathlib import Path
import shutil

from openpyxl import load_workbook
import pytest

from mapgis_agent.borehole import borehole_trajectory
from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.drc import run_drc_audit
from mapgis_agent.geology_section import prepare_drillhole_workflow, section_dip
from mapgis_agent.native_backend import native_file_info
from mapgis_agent.native_ui_core import digest
from mapgis_agent.private_control import _profile
from mapgis_agent.ui_macros import VerifiedUIMacroRunner


ROOT = Path(__file__).resolve().parents[1]
FRAME = {'crs': {'definition': 'EPSG:4528', 'axis_order': 'xy'},
         'vertical_unit': 'metre', 'vertical_datum': 'Synthetic test datum',
         'azimuth_reference': 'grid_north', 'inclination_reference': 'vertical_down'}
MAIN = {'class': 'MapGISSection', 'enabled': True}
DIALOG = {'class': '#32770', 'visible': True}


class ScriptedControl:
    def __init__(self, output=None, on_action=None):
        self.snapshot = {'windows': [{**MAIN, 'hwnd': 101, 'text': 'section V5'}]}
        self.actions = []
        self.output = output
        self.on_action = on_action

    def __call__(self, bridge, verb, *, session_id, **params):
        assert session_id == 'section-macro'
        if verb == 'status':
            return {'bindings': {'out': {'file': str(self.output), 'role': 'output'}}}
        if verb == 'observe':
            return {'state': 'OBSERVED', 'snapshot': self.snapshot, 'token': 'snapshot-token',
                    'snapshot_sha256': digest(self.snapshot)}
        assert verb == 'act' and params['token'] == 'snapshot-token'
        self.actions.append(params['parameters'])
        if self.on_action:
            self.on_action(self)
        return {'state': 'POSTED_NOT_COMPLETION_PROOF'}


def test_macro_rejects_preconditions_and_actions_without_postconditions(tmp_path):
    fake = ScriptedControl()
    runner = VerifiedUIMacroRunner(Bridge(tmp_path), 'section-macro', control=fake)
    action = {'operation': 'command', 'command_id': 57601, 'target': {'window': MAIN}}
    no_proof = runner.run([{'action': action}], request_id='no-proof')
    assert no_proof['state'] == 'VERIFICATION_FAILED'
    assert no_proof['error'].startswith('POSTED_NOT_COMPLETION_PROOF')
    refused = runner.run([{'preconditions': [{'window': DIALOG}], 'action': action,
                           'postconditions': [{'changed': True}]}], request_id='bad-precondition')
    assert refused['state'] == 'VERIFICATION_FAILED'
    assert refused['error'] == 'UI_MACRO_PRECONDITION'
    assert fake.actions == []


def test_macro_never_accepts_a_posted_command_without_observed_change(tmp_path):
    fake = ScriptedControl()
    runner = VerifiedUIMacroRunner(Bridge(tmp_path), 'section-macro', control=fake)
    steps = [{'action': {'operation': 'command', 'command_id': 57601, 'target': {'window': MAIN}},
              'postconditions': [{'window': DIALOG}, {'changed': True}]}]
    reply = runner.run(steps, request_id='posted', timeout_seconds=.01)
    assert reply['state'] == 'VERIFICATION_FAILED'
    assert reply['error'] == 'POSTED_NOT_COMPLETION_PROOF: postcondition failed'
    assert reply['steps'][0]['action']['state'] == 'POSTED_NOT_COMPLETION_PROOF'
    assert 'after_sha256' not in reply['steps'][0]
    assert runner.run(steps, request_id='posted', timeout_seconds=.01)['reconciled'] is True
    assert len(fake.actions) == 1


def test_macro_observes_two_steps_checks_disk_sha_and_reads_back_wt(tmp_path):
    bridge = Bridge(tmp_path)
    template = {'name': 'point.wt', 'map_scale': 1,
                'crs': {'kind': 'local', 'name': 'Test drawing', 'unit': 'millimetre',
                        'axis_order': 'xy', 'reference_sha256': 'a'*64}}
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': [
        {'id': 'point', 'geometry': {'type': 'Point', 'coordinates': [10, 5]}}]}, template, 'point')
    content = Path(bridge.apply('point')['output']).read_bytes()
    output = tmp_path/'work/output.wt'
    button = {'class': 'Button', 'id': 1}

    def advance(control):
        if len(control.actions) == 1:
            control.snapshot = {'windows': [*control.snapshot['windows'],
                {**DIALOG, 'hwnd': 201, 'children': [{**button, 'hwnd': 202}]}]}
        else:
            output.write_bytes(content)
            control.snapshot = {'windows': control.snapshot['windows'][:1]}

    fake = ScriptedControl(output, advance)
    runner = VerifiedUIMacroRunner(bridge, 'section-macro', control=fake)
    steps = [
        {'preconditions': [{'window': MAIN}], 'postconditions': [{'window': DIALOG, 'control': button}],
         'action': {'operation': 'command', 'command_id': 57601, 'target': {'window': MAIN}}},
        {'preconditions': [{'window': DIALOG, 'control': button}],
         'action': {'operation': 'button', 'target': {'window': DIALOG, 'control': button}}},
    ]
    checks = [{'binding': 'out', 'reader': 'read_native', 'expected_sha256': sha(content)}]
    receipt = runner.run(steps, checks, request_id='save-verified')
    assert receipt['state'] == 'VERIFIED'
    assert [action['hwnd'] for action in fake.actions] == [101, 202]
    assert receipt['steps'][0]['after_sha256'] == receipt['steps'][1]['before_sha256']
    assert receipt['artifacts'] == [{'file': str(output), 'sha256': sha(output.read_bytes()),
                                     'bytes': len(content), 'read_native': {'type': 'POINT', 'active': 1}}]
    assert runner.run(steps, checks, request_id='save-verified')['reconciled']
    output.write_bytes(content+b'tampered')
    changed = runner.run(steps, checks, request_id='save-verified')
    assert changed['state'] == 'VERIFICATION_FAILED' and changed['error'] == 'ARTIFACT_CHANGED'
    assert len(fake.actions) == 2


@pytest.mark.parametrize('azimuth', [0, 90, 225, 270])
def test_minimum_curvature_is_a_quarter_circle_and_preserves_direction(azimuth):
    collar = {'easting': 40500000, 'northing': 2800000, 'elevation': 175}
    course = [{'md': 0, 'inclination': 0, 'azimuth': azimuth},
              {'md': 100, 'inclination': 90, 'azimuth': azimuth}]
    result = borehole_trajectory(course, collar, FRAME, query_mds=[25, 50, 75])
    radius = 200/math.pi
    for point in [*result['survey_samples'], *result['query_samples']]:
        theta = point['md']/radius
        horizontal = radius*(1-math.cos(theta))
        assert point['true_vertical_depth'] == pytest.approx(radius*math.sin(theta), abs=1e-10)
        assert point['east_offset'] == pytest.approx(horizontal*math.sin(math.radians(azimuth)), abs=1e-10)
        assert point['north_offset'] == pytest.approx(horizontal*math.cos(math.radians(azimuth)), abs=1e-10)
        assert point['easting'] == pytest.approx(collar['easting']+point['east_offset'], abs=1e-8, rel=0)
        assert point['northing'] == pytest.approx(collar['northing']+point['north_offset'], abs=1e-8, rel=0)
        assert point['elevation'] == pytest.approx(collar['elevation']-point['true_vertical_depth'])


@pytest.mark.parametrize(('bearing', 'azimuth', 'dip', 'k', 'slope'), [
    (90, 90, 45, 3, 1), (90, 270, 45, 3, -1), (30, 120, 45, 2, 0),
    (350, 35, 45, .5, 2**-.5), (315, 90, 45, 2, -2**-.5),
])
def test_signed_apparent_and_exaggerated_dips(bearing, azimuth, dip, k, slope):
    result = section_dip(bearing, dip, azimuth, k)
    assert result['apparent_dip_degrees'] == pytest.approx(math.degrees(math.atan(slope)))
    assert result['displayed_apparent_dip_degrees'] == pytest.approx(math.degrees(math.atan(k*slope)))


def test_vertical_dip_parallel_to_section_has_no_unique_intersection():
    assert section_dip(0, 90, 180, 2)['displayed_apparent_dip_degrees'] == -90
    with pytest.raises(BridgeError, match='DIP_UNDEFINED'):
        section_dip(0, 90, 90, 2)
    with pytest.raises(BridgeError, match='DRILLHOLE_VERTICAL_EXAGGERATION'):
        section_dip(0, 45, 90, 0)


def test_section_profile_pins_the_native_executable():
    module = _profile()['modules']['section']
    assert module['executable'] == 'section.exe'
    assert module['sha256'] == 'e3e85a4756f1ec2216e88dd47274a1e42b591c5904e3be37097535d8747f82b9'


def test_editable_section_columns_exchange_and_native_reopen(tmp_path, monkeypatch):
    if not (ROOT/'config.local.json').is_file():
        pytest.skip('Configured MapGIS 6.7 runtime required')
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    from run_flagship_geology_slice import make_inputs, verify_section_open
    from verify_com6 import configure_task_root

    bridge = configure_task_root(tmp_path/'native')
    paths = make_inputs(bridge.root)
    sources = {key: {'file': str(path), 'expected_sha256': sha(path.read_bytes())} for key, path in paths.items()}
    settings = json.loads((bridge.root/'config.local.json').read_text(encoding='utf-8'))
    exe = Path(settings['mapgis_installation'])/'program/section.exe'
    assert sha(exe.read_bytes()) == _profile()['modules']['section']['sha256']
    params = {'title': 'A-A 剖面', 'section': {'start': [40500000, 2800000], 'end': [40500900, 2800000],
              'max_crossline_distance': 60}, 'horizontal_scale': 5000, 'vertical_exaggeration': 2,
              'lithology_styles': {'砂岩': {'fill_color': 3}, '板岩': {'fill_color': 5}, '花岗岩': {'fill_color': 8}}}
    request = 'test-drillhole-section'
    prepared = prepare_drillhole_workflow(bridge, sources, FRAME, params, request)
    assert prepared['state'] == 'PREPARED'
    artifact = Path(bridge.apply(request)['output'])
    report = json.loads(artifact.read_text(encoding='utf-8'))
    assert set(report['projects']) == {'columns', 'section'}
    assert report['projects']['columns']['assay_maxima'] == {'Cu': .48, 'Mo': .09}
    for mode, project in report['projects'].items():
        mpj = artifact.parent / project['file']
        assert mpj.suffix == '.mpj' and sha(mpj.read_bytes()) == project['sha256']
        assert {Path(layer['file']).suffix.lower() for layer in project['layers']} == {'.wt', '.wl', '.wp'}
        files = []
        for layer in project['layers']:
            path = artifact.parent / layer['file']
            _, reader, _ = read_native(path)
            opened = native_file_info(bridge.root, str(path), layer['sha256'])
            assert opened['mode'] == 'isolated_native_x86'
            assert opened['active'] == len(reader.geom) > 0
            files.append({'file': str(path), 'expected_sha256': layer['sha256']})
        assert run_drc_audit(files, 'LOCAL', enforce_frames=True)['summary']['error_count'] == 0, mode
    workbook = load_workbook(artifact.parent/report['exchange']['tables'], read_only=True)
    try:
        assert set(workbook.sheetnames) == {'collar', 'survey', 'lithology', 'assay', 'projected_section'}
        projected = list(workbook['projected_section'].values)
        assert projected[0][:3] == ('hole_id', 'md', 'true_vertical_depth')
        assert {row[0] for row in projected[1:]} == {'ZK01', 'ZK02'}
    finally:
        workbook.close()
    with (artifact.parent/'exchange/projected_section.csv').open(encoding='utf-8-sig', newline='') as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == len(projected)-1 and float(records[-1]['md']) == 125
    project = artifact.parent/report['exchange']['native_section_project']
    opened = verify_section_open(bridge, project)
    assert not any(opened['closed']['processes_alive'].values())
    if opened.get('macro', {}).get('error') == 'PRIVATE_UI_OBSERVATION_CHANGED':
        # The file chooser can finish updating after the snapshot. The guard
        # rejected the action, so try a fresh, owned, read-only project session.
        fresh = configure_task_root(tmp_path/'fresh-v5')
        copied = fresh.root/'work/section'
        shutil.copytree(project.parent, copied)
        opened = verify_section_open(fresh, copied/project.name)
    assert opened.get('macro', {}).get('state') == 'VERIFIED', opened.get('error') or opened.get('macro')
    assert not any(opened['closed']['processes_alive'].values())
