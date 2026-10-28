import hashlib
import importlib.util
from pathlib import Path


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
