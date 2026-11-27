import hashlib
import json
from zipfile import ZipFile

import numpy as np
import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.fdr import RedactionPolicy, export_fdr_bundle, verify_fdr_bundle
from mapgis_agent.map_plan import MapPlanEngine
from mapgis_agent.object_store import file_sha256
from mapgis_agent.visual_preview import _save


def _state(root):
    file = root / 'private-source.txt'
    file.write_text('outside the archive', encoding='utf8')
    spec = {'plan_id': 'fdr', 'crs': 'LOCAL', 'unit': 'local', 'scale': 50000, 'nodes': [
        {'id': 'copy', 'op': 'work.copy', 'inputs': [
            {'role': 'file', 'source_file': str(file), 'expected_sha256': file_sha256(file)}]},
        {'id': 'after', 'op': 'work.copy', 'inputs': [{'role': 'file', 'from_node': 'copy', 'output_key': 'file'}]}]}
    return MapPlanEngine(root).apply(spec, fault_inject_node='after')


@pytest.mark.parametrize('mode', ['checkpoint_recovery', 'deterministic_recompute'])
def test_fdr_modes_and_structured_redaction(tmp_path, mode):
    state = _state(tmp_path)
    archive = tmp_path / f'{mode}.mapgis-fdr.zip'
    png = tmp_path / 'neutral.png'
    _save(np.zeros((32, 40, 3), dtype=np.uint8), png, 300)
    report = {'violations': [{'coords': [40_500_001.25, 3_250_009.5],
                              'message': 'Self-intersection[40500001.25 3250009.5]',
                              'file': 'C:/Users/PrivateName/project/map.wp',
                              'attributes': {'SITE': 'no-disclosure'}}],
              'files': [{'file': '//server/private/area.wp', 'bounds': [40_500_000., 3_250_000., 40_500_003., 3_250_015.]}]}
    policy = RedactionPolicy(coordinate_offset=(40_500_000, 3_250_000), sensitive_fields=('site',),
                             previews_are_redacted=True)
    exported = export_fdr_bundle(tmp_path, archive, mode=mode, plan_id='fdr', drc_report=report,
                                 preview_paths=[png], redaction=policy)
    assert exported['verified'] and verify_fdr_bundle(archive)['structured_redaction_checked']
    with ZipFile(archive) as bundle:
        assert 'previews/preview-001.png' in bundle.namelist()
        decoded = json.loads(bundle.read('drc_report.json'))
        assert decoded['violations'][0]['coords'] == [1.25, 9.5]
        assert decoded['files'][0]['bounds'] == [0, 0, 3, 15]
        assert decoded['violations'][0]['attributes']['SITE'] == '[REDACTED]'
        assert decoded['files'][0]['file'].startswith('[PATH_')
        transcript = b''.join(bundle.read(name) for name in bundle.namelist())
        for private in (b'PrivateName', b'no-disclosure', b'40500001.25', b'3250009.5', b'private-source.txt'):
            assert private not in transcript
        exported_state = json.loads(bundle.read('plan_state.json'))
        assert exported_state['spec_sha256'] == state['spec_sha256']
        if mode == 'checkpoint_recovery':
            assert 'completed' in exported_state and exported_state['status'] == 'INTERRUPTED'
            assert 'spec' not in exported_state
        else:
            assert 'completed' not in exported_state and exported_state['spec']['nodes'][0]['inputs'][0]['source_file'].startswith('[PATH_')


def test_integrity_and_leak_detection_even_with_resealed_manifest(tmp_path):
    archive = tmp_path / 'clean.zip'
    export_fdr_bundle(tmp_path, archive, drc_report={'violations': []})
    with ZipFile(archive) as bundle:
        members = {name: bundle.read(name) for name in bundle.namelist()}
    altered = tmp_path / 'tampered.zip'
    members['drc_report.json'] = b'{"file":"E:/private/example.wp"}'
    with ZipFile(altered, 'w') as bundle:
        for name, value in members.items():
            bundle.writestr(name, value)
    with pytest.raises(BridgeError, match='FDR_INTEGRITY'):
        verify_fdr_bundle(altered)
    manifest = json.loads(members['manifest.json'])
    manifest['members']['drc_report.json'] = {'sha256': hashlib.sha256(members['drc_report.json']).hexdigest(),
                                             'bytes': len(members['drc_report.json'])}
    members['manifest.json'] = json.dumps(manifest).encode()
    with ZipFile(altered, 'w') as bundle:
        for name, value in members.items():
            bundle.writestr(name, value)
    with pytest.raises(BridgeError, match='FDR_LEAK'):
        verify_fdr_bundle(altered)


def test_unattested_previews_are_omitted(tmp_path):
    raster = tmp_path / 'map.png'
    _save(np.zeros((32, 40, 3), dtype=np.uint8), raster, 150)
    archive = tmp_path / 'declined.zip'
    result = export_fdr_bundle(tmp_path, archive, preview_paths=[raster])
    assert result['manifest']['redaction']['omitted_unattested_previews'] == 1
    with ZipFile(archive) as bundle:
        assert all(not path.startswith('previews/') for path in bundle.namelist())
