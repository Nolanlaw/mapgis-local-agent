import json
import shutil
from pathlib import Path

import pytest

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.multimedia import prepare_multimedia
from test_attribute_schema import sample


def media_parameters(bridge):
    asset = bridge.root/'work'/'现场记录.txt'
    asset.write_bytes('石英脉宽度 1.2 m\r\n'.encode('gbk'))
    return {'fields': [{'name': '现场记录', 'type': 'multimedia_text'}],
            'assets': [{'id': 'log', 'file': str(asset), 'expected_sha256': sha(asset.read_bytes())}],
            'links': [{'record_id': 1, 'field': '现场记录', 'asset': 'log'}]}


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_bundle_moves_with_exact_media_bytes_and_atomic_nested_reconcile(tmp_path, suffix):
    bridge = Bridge(tmp_path)
    source = sample(bridge, suffix)
    p = media_parameters(bridge)
    prepared = prepare_multimedia(bridge, str(source), sha(source.read_bytes()), p, 'media')
    assert prepared['state'] == 'PREPARED'
    result = bridge.apply('media')
    output = Path(result['output'])
    reader = read_native(output)[1]
    assert reader.data['现场记录'].tolist() == ['media/log.txt'] + [''] * (len(reader.geom)-1)
    assert (output.parent/'media/log.txt').read_bytes() == Path(p['assets'][0]['file']).read_bytes()
    report = json.loads((output.parent/'multimedia-lineage.json').read_text(encoding='utf8'))
    assert report['media_directory'] == '' and report['viewer_execution'] == 'NOT_VERIFIED'
    moved = tmp_path/'relocated'
    shutil.copytree(output.parent, moved)
    view = read_native(moved/output.name)[1]
    for ref in view.data['现场记录']:
        if ref:
            assert sha((moved/ref).read_bytes()) == p['assets'][0]['expected_sha256']
    assert prepare_multimedia(bridge, str(source), sha(source.read_bytes()), p, 'media')['state'] == 'APPLIED'


def test_asset_change_after_prepare_prevents_publication(tmp_path):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    p = media_parameters(bridge)
    prepare_multimedia(bridge, str(source), sha(source.read_bytes()), p, 'media')
    Path(p['assets'][0]['file']).write_bytes(b'changed')
    with pytest.raises(BridgeError, match='STALE'):
        bridge.apply('media')
    assert not (bridge.root/'deliverables/artifacts/media').exists()


@pytest.mark.parametrize('mutation', ['unused', 'duplicate_link', 'missing_asset', 'bad_id', 'ordinary_collision'])
def test_bundle_rejects_ambiguous_or_incomplete_links(tmp_path, mutation):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    p = media_parameters(bridge)
    if mutation == 'unused': p['links'] = []
    if mutation == 'duplicate_link': p['links'] *= 2
    if mutation == 'missing_asset': p['links'][0]['asset'] = 'absent'
    if mutation == 'bad_id': p['assets'][0]['id'] = '../bad'
    if mutation == 'ordinary_collision': p['fields'][0]['name'] = 'ID'
    with pytest.raises((BridgeError, ValueError)):
        prepare_multimedia(bridge, str(source), sha(source.read_bytes()), p, 'bad')

