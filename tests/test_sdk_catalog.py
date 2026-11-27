import json

from mapgis_agent.sdk_catalog import PROFILE_PATH, sdk_describe


def test_six_libraries_and_exact_class_provenance(tmp_path):
    (tmp_path/'config.local.json').write_text(json.dumps({'mapgis_installation': str(tmp_path)}), encoding='utf-8')
    libraries = {entry['library']: entry for entry in sdk_describe(tmp_path)['libraries']}
    assert len(libraries) == 6
    assert libraries['proj60.dll']['typelib_guid'] == '{5CEBBEC3-DD0E-11D2-9527-000021DC1F51}'
    assert libraries['AttCom.dll']['typelib_version'] == '1.0'
    profile = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
    ownership = {p['name']: {c['name'] for c in p['classes']} for p in profile['files']}
    assert ownership['proj60.dll'] == {'EllipPara'}
    assert ownership['AttCom.dll'] == {'LinkAtt', 'UpLoad', 'GisObj'}
    assert 'TblArea' in ownership['MapGisBasCom1.DLL']
