"""Incomplete format sets resume through the public MCP without replaying a PDF."""
import asyncio
import json
from pathlib import Path

import pytest

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.raster import file_sha
from mapgis_agent.server import build_server
from test_plot_export import sample_eps


def interrupted_pdf(tmp_path, monkeypatch):
    import mapgis_agent.plot_export as export
    bridge = Bridge(tmp_path)
    source = tmp_path/'source.eps'
    digest = sample_eps(source)
    original = export.subprocess.Popen
    commands = []
    stopped = False

    def first_pdf_only(command, *args, **kwargs):
        nonlocal stopped
        if '-sDEVICE=tiff24nc' in command and not stopped:
            stopped = True
            raise OSError('owned stop before TIFF conversion')
        if any(v in command for v in ('-sDEVICE=pdfwrite', '-sDEVICE=tiff24nc')):
            commands.append(list(command))
        return original(command, *args, **kwargs)

    monkeypatch.setattr(export.subprocess, 'Popen', first_pdf_only)
    with pytest.raises(OSError, match='owned stop'):
        export.prepare_plot_export(bridge, str(source), digest, {'formats': ['pdf', 'tiff'], 'dpi': 144}, 'export')
    stage = Path(bridge.status('export')['stage'])
    assert (stage/'files/map.pdf').is_file() and not (stage/'complete.json').exists()
    return bridge, source, stage, commands


def test_public_reconcile_adopts_completed_pdf_and_converts_only_missing_tiff(tmp_path, monkeypatch):
    bridge, source, stage, commands = interrupted_pdf(tmp_path, monkeypatch)
    pdfs = list(stage.rglob('*.pdf'))
    frozen = {str(f): (file_sha(f), f.stat().st_mtime_ns) for f in pdfs}
    result = asyncio.run(build_server(tmp_path).call_tool('mapgis_reconcile_artifact', {'request_id': 'export'}))
    receipt = json.loads(result[0].text)
    assert receipt['state'] == 'PREPARED' and receipt['reconciled_after_interruption']
    assert receipt['reused_formats'] == ['pdf'] and receipt['newly_converted_formats'] == ['tiff']
    assert sum('-sDEVICE=pdfwrite' in c for c in commands) == 1
    assert sum('-sDEVICE=tiff24nc' in c for c in commands) == 1
    assert all((file_sha(Path(file)), Path(file).stat().st_mtime_ns) == value for file, value in frozen.items())
    output = bridge.apply('export')
    assert output['state'] == 'APPLIED' and Path(output['output']).with_suffix('.tif').is_file()
    import mapgis_agent.plot_export as export
    again = export.prepare_plot_export(bridge, str(source), sha(source.read_bytes()), {'formats': ['pdf', 'tiff'], 'dpi': 144}, 'export')
    assert again['state'] == 'APPLIED' and len(commands) == 2


@pytest.mark.parametrize('changed, expected', [
    ('source', 'STALE_SOURCE'), ('context', 'EXPORT_CONTEXT_CHANGED'),
    ('snapshot', 'EXPORT_SNAPSHOT_CHANGED'), ('pdf', 'EXPORT_MEMBER_CHANGED'),
])
def test_unsealed_export_recovery_rejects_changed_inputs_or_completed_member(tmp_path, monkeypatch, changed, expected):
    bridge, source, stage, commands = interrupted_pdf(tmp_path, monkeypatch)
    import mapgis_agent.plot_export as export
    target = {'source': source, 'context': stage/'export-context.json',
              'snapshot': stage/'source.eps', 'pdf': stage/'files/map.pdf'}[changed]
    with target.open('ab') as stream:
        stream.write(b'owned modification')
    with pytest.raises(BridgeError, match=expected):
        export.reconcile_plot_export(bridge, 'export')
    assert len(commands) == 1  # no extra PDF or TIFF converter started


def test_recovery_does_not_compete_with_a_live_producer(tmp_path, monkeypatch):
    bridge, source, stage, commands = interrupted_pdf(tmp_path, monkeypatch)
    import mapgis_agent.plot_export as export
    with bridge.connect() as db:
        db.execute("UPDATE commands SET state='RUNNING' WHERE id='export'")
    with pytest.raises(BridgeError, match='ARTIFACT_PRODUCER_RUNNING'):
        export.reconcile_plot_export(bridge, 'export')
    assert len(commands) == 1


def test_complete_pdf_without_a_parent_exit_receipt_is_adopted_by_full_render_check(tmp_path, monkeypatch):
    bridge, source, stage, commands = interrupted_pdf(tmp_path, monkeypatch)
    pointer = json.loads((stage/'formats/pdf/attempt.json').read_text(encoding='utf8'))
    folder = stage/'formats/pdf'/pointer['directory']
    for name in ('process.json', 'validated.json'):
        (folder/name).rename(folder/(name+'.before-loss'))
    candidate = folder/'map.pdf'
    before = (file_sha(candidate), candidate.stat().st_mtime_ns)
    import mapgis_agent.plot_export as export
    result = export.reconcile_plot_export(bridge, 'export')
    assert result['state'] == 'PREPARED' and result['conversions']['pdf']['adopted_by_readback']
    assert result['conversions']['pdf']['returncode'] is None
    assert (file_sha(candidate), candidate.stat().st_mtime_ns) == before
    assert sum('-sDEVICE=pdfwrite' in c for c in commands) == 1


def test_empty_unacknowledged_pdf_is_retained_and_rebuilt_in_a_different_attempt(tmp_path, monkeypatch):
    bridge, source, stage, commands = interrupted_pdf(tmp_path, monkeypatch)
    pointer = json.loads((stage/'formats/pdf/attempt.json').read_text(encoding='utf8'))
    folder = stage/'formats/pdf'/pointer['directory']
    for name in ('process.json', 'validated.json', 'map.pdf'):
        (folder/name).rename(folder/(name+'.before-loss'))
    (stage/'files/map.pdf').rename(stage/'retained-before-publication.pdf')
    candidate = folder/'map.pdf'
    candidate.write_bytes(b'')
    before = (file_sha(candidate), candidate.stat().st_mtime_ns)
    import mapgis_agent.plot_export as export
    result = export.reconcile_plot_export(bridge, 'export')
    assert result['state'] == 'PREPARED' and result['newly_converted_formats'] == ['pdf', 'tiff']
    assert result['conversions']['pdf']['retained_incomplete_candidate']['file'] == str(candidate)
    assert (file_sha(candidate), candidate.stat().st_mtime_ns) == before
    assert sum('-sDEVICE=pdfwrite' in c for c in commands) == 2
    bridge.apply('export')
    assert bridge.status('export')['output_matches']


@pytest.mark.parametrize('format', ['tiff', 'jpeg'])
def test_unacknowledged_image_requires_every_pixel_to_match_an_independent_conversion(tmp_path, monkeypatch, format):
    import mapgis_agent.plot_export as export
    bridge = Bridge(tmp_path)
    source = tmp_path/'source.eps'
    digest = sample_eps(source)
    finish = export.finish_bundle
    def stop(*args, **kwargs):
        raise OSError('owned stop before seal')
    monkeypatch.setattr(export, 'finish_bundle', stop)
    with pytest.raises(OSError, match='owned stop'):
        export.prepare_plot_export(bridge, str(source), digest, {'formats': [format], 'dpi': 144}, 'image')
    monkeypatch.setattr(export, 'finish_bundle', finish)
    stage = Path(bridge.status('image')['stage'])
    pointer = json.loads((stage/'formats'/format/'attempt.json').read_text(encoding='utf8'))
    folder = stage/'formats'/format/pointer['directory']
    for name in ('process.json', 'validated.json'):
        (folder/name).rename(folder/(name+'.before-loss'))
    candidate = folder/export.NAMES[format]
    before = (file_sha(candidate), candidate.stat().st_mtime_ns)
    result = export.reconcile_plot_export(bridge, 'image')
    assert result['state'] == 'PREPARED' and result['reused_formats'] == [format]
    assert result['conversions'][format]['returncode'] is None
    assert (file_sha(candidate), candidate.stat().st_mtime_ns) == before
    assert len(list(stage.glob('check-*/converter/process.json'))) == 1
