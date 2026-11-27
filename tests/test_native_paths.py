import json

from mapgis_agent.native_paths import NativePathAlias, cleanup_aliases


def test_alias_name_has_no_hyphen_and_closes(tmp_path):
    # MapGIS 属性管理 export builds "CREATE TABLE <unbracketed path>"; Jet rejects a '-' in it.
    with NativePathAlias(tmp_path) as alias:
        assert alias.path.name.startswith('mg6_') and '-' not in alias.path.name
        assert alias.path.is_junction()
    assert not alias.path.exists()
    record = json.loads(alias.record.read_text(encoding='utf8'))
    assert record['state'] == 'CLOSED'
    cleanup_aliases(tmp_path)  # closed records, old or new prefix, are left alone
