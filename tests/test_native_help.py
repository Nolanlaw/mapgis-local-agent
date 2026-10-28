"""Installed documentation is searchable, source-bound, and never execution proof."""
import hashlib
import json

import pytest


def test_help_search_reads_full_selected_topic_and_rejects_stale_source(tmp_path):
    from mapgis_agent.native_help import search_native_help

    program = tmp_path / 'mapgis/program'
    program.mkdir(parents=True)
    source = program / 'Mpedit60.hlp'
    source.write_bytes(b'installed help fixture')
    (tmp_path / 'config.local.json').write_text(json.dumps({'mapgis_installation': str(program.parent)}))
    folder = tmp_path / 'work/native-help'
    folder.mkdir(parents=True)
    (folder / 'editor.json').write_text(json.dumps({
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'source': str(source), 'parse_errors': [],
        'topics': [{'id': '0', 'title': '自动剪断', 'text': '剪断造区线。开放断层另存。'},
                   {'id': '1', 'title': None, 'text': '图例板编辑。'}]}, ensure_ascii=False), encoding='utf8')
    result = search_native_help(tmp_path, 'editor', '剪断')
    assert result['matches'][0]['topic_id'] == '0'
    assert result['execution_verified'] is False
    assert search_native_help(tmp_path, 'editor', topic_id='0')['topic']['text'] == '剪断造区线。开放断层另存。'
    untitled = search_native_help(tmp_path, 'editor', '图例')
    assert untitled['matches'][0]['topic_id'] == '1' and untitled['matches'][0]['title']
    source.write_bytes(b'a different installed build')
    assert search_native_help(tmp_path, 'editor', '剪断')['status'] == 'STALE_INDEX'


def test_help_missing_and_unknown_modules_are_explicit(tmp_path):
    from mapgis_agent.native_help import search_native_help

    assert search_native_help(tmp_path, 'network', '最短路径')['status'] == 'NOT_CONFIGURED'
    with pytest.raises(ValueError, match='UNKNOWN_MODULE'):
        search_native_help(tmp_path, '../../private')
