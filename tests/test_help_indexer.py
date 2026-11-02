import hashlib
import importlib.util
from pathlib import Path

import pytest


def test_explicit_encoding_reads_real_legacy_chinese_report_help(tmp_path):
    """The installation's Gisbb metadata says ANSI; its text is GBK."""
    import json
    root = Path(__file__).resolve().parents[1]
    config = root / 'config.local.json'
    if not config.is_file():
        pytest.skip('installed legacy help is not available')
    source = Path(json.loads(config.read_text(encoding='utf8'))['mapgis_installation']) / 'program/Gisbb.hlp'
    if not source.is_file():
        pytest.skip('installed report help is not available')
    spec = importlib.util.spec_from_file_location('native_report_help_indexer', root / 'scripts/index_native_help.py')
    indexer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(indexer)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    result = indexer.index_module('reports', source, tmp_path, hlp_encoding='gb18030')
    index = json.loads((tmp_path / 'reports.json').read_text(encoding='utf8'))
    assert result['status'] == 'INDEXED'
    assert index['topics'][0]['title'] == '报表编辑目录'
    assert '报表编辑系统' in index['topics'][0]['text']
    assert index['encoding_override'] == 'gb18030'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    # A recipient rebuilding all indexes must get readable text without needing
    # the manual override again. This regression is tied to this exact source.
    automatic = tmp_path / 'automatic'
    automatic.mkdir()
    indexer.index_module('reports', source, automatic)
    auto_index = json.loads((automatic / 'reports.json').read_text(encoding='utf8'))
    assert auto_index['topics'][0]['title'] == '报表编辑目录'
    assert auto_index['encoding_source'] == 'exact-source-profile'


def test_chm_extracts_in_its_directory_and_resumes_an_incomplete_attempt(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/index_native_help.py'
    spec = importlib.util.spec_from_file_location('native_help_indexer', script)
    indexer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(indexer)
    source = tmp_path / 'installed help.chm'
    source.write_bytes(b'CHM extraction fixture')
    folder = tmp_path / 'package with spaces/native-help'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = folder / ('chm-raster-' + digest[:12])
    destination.mkdir(parents=True)

    class Extractor:
        def __init__(self, args, *, cwd):
            assert args[1:3] == ['-decompile', '.']
            assert Path(cwd) == destination
            (destination / 'topic.htm').write_text('<h1>校正</h1><p>先进入预览</p>', encoding='utf8')

        def wait(self, *, timeout):
            return 0

    monkeypatch.setattr(indexer.subprocess, 'Popen', Extractor)
    monkeypatch.setattr(indexer.time, 'sleep', lambda _: None)
    result = indexer.index_module('raster', source, folder)
    assert result['status'] == 'INDEXED' and result['topics'] == 1
    assert (destination / 'extracted.json').is_file()
