"""Ordered native view pages remain editable, portable and recoverable."""
from pathlib import Path
import json
import pytest
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from test_project_plot_recovery import project

def parameters():
    return {'pages':[{'name':'全图','view':{'native_bounds':[-1,-1,10,9],'paper_mm':[110,100],'coordinate_frame':'native'}},
        {'name':'东侧分幅','view':{'native_bounds':[4,-1,10,9],'paper_mm':[90,150],'coordinate_frame':'native'}}],
        'dpi':150,'include_tiff':True}

@pytest.mark.parametrize('change',[lambda p:p.update(pages=[]),lambda p:p['pages'][1].update(name='全图'),
    lambda p:p.update(include_tiff=1),lambda p:p.update(dpi=71),lambda p:p['pages'][0]['view'].update(native_bounds=[0,0,0,1])])
def test_explicit_unique_pages_and_existing_view_rules_are_required(change):
    from mapgis_agent.map_series import _parameters
    p=parameters();change(p)
    with pytest.raises(BridgeError,match='MAP_SERIES|PLOT_VIEW'):_parameters(p)

def test_two_page_sizes_order_native_project_reopen_tiff_and_idempotent_publication(tmp_path,monkeypatch):
    import mapgis_agent.map_series as series
    b,p=project(tmp_path);calls=[];original=series.prepare_project_plot
    def spy(*args,**kwargs):calls.append(args[4]);return original(*args,**kwargs)
    monkeypatch.setattr(series,'prepare_project_plot',spy)
    result=series.prepare_map_series(b,p['output'],p['candidate_sha256'],parameters(),'atlas')
    assert result['page_count']==2 and len(calls)==2
    assert [page['name'] for page in result['pages']]==['全图','东侧分幅']
    assert [page['paper_mm'] for page in result['pages']]==[[110,100],[90,150]]
    output=b.apply('atlas');folder=Path(output['output']).parent
    assert {'series.pdf','series.json','pages.csv','pages.xlsx','page-001.eps','page-002.eps','page-001.png','page-002.png','page-001.tif','page-002.tif','areas.mpj'}<=set(v.name for v in folder.iterdir())
    from mapgis_agent.tables import table_info
    assert table_info(str(folder/'pages.xlsx'),file_sha(folder/'pages.xlsx'),'图幅')['row_count']==2
    assert result['native_project_reopen']['source_sha256']==p['candidate_sha256']
    assert all(Path(v['file']).parent.name=='files' for v in result['native_project_reopen']['layers'])
    metadata=json.loads((folder/'series.json').read_text(encoding='utf8'))
    assert metadata['pages'][1]['view']==parameters()['pages'][1]['view']
    before={f.name:(file_sha(f),f.stat().st_mtime_ns) for f in folder.iterdir()}
    again=series.prepare_map_series(b,p['output'],p['candidate_sha256'],parameters(),'atlas')
    assert again['state']=='APPLIED' and len(calls)==2
    assert all((file_sha(folder/name),(folder/name).stat().st_mtime_ns)==value for name,value in before.items())

def test_original_dependency_is_guarded_until_series_publication(tmp_path):
    import mapgis_agent.map_series as series
    b,p=project(tmp_path);params=parameters();params['pages']=params['pages'][:1];params['include_tiff']=False
    series.prepare_map_series(b,p['output'],p['candidate_sha256'],params,'guard')
    source=next(Path(p['output']).parent.glob('*.wp'))
    with source.open('ab') as stream:stream.write(b'owned source change')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('guard')

def test_resume_adopts_first_page_and_starts_only_the_not_issued_second_page(tmp_path,monkeypatch):
    import mapgis_agent.map_series as series
    from mapgis_agent.core import Bridge
    b,p=project(tmp_path);params=parameters();params['include_tiff']=False;calls=[];stop=True;original=series.prepare_project_plot
    def once(*args,**kwargs):
        nonlocal stop
        calls.append(args[4])
        if args[4]=='page-002-plot' and stop:stop=False;raise OSError('stop before issuing second native page')
        return original(*args,**kwargs)
    monkeypatch.setattr(series,'prepare_project_plot',once)
    with pytest.raises(OSError,match='second native page'):series.prepare_map_series(b,p['output'],p['candidate_sha256'],params,'resume')
    stage=Path(b.status('resume')['stage']);renderer=Bridge(stage/'renderer');page=renderer.status('page-001-plot')
    first=Path(page['output']);before=(file_sha(first),first.stat().st_mtime_ns,page['native_execution']['pid'])
    result=series.reconcile_map_series(b,'resume');assert result['state']=='PREPARED' and result['reconciled_after_interruption']
    assert (file_sha(first),first.stat().st_mtime_ns,renderer.status('page-001-plot')['native_execution']['pid'])==before
    assert calls.count('page-001-plot')==1 and calls.count('page-002-plot')==2
    assert b.apply('resume')['state']=='APPLIED'

def test_resume_keeps_completed_combined_pdf_bytes_and_mtime(tmp_path,monkeypatch):
    import mapgis_agent.map_series as series
    b,p=project(tmp_path);params=parameters();params['include_tiff']=False;original=series._merge_pdf;stop=True
    def once(*args,**kwargs):
        nonlocal stop
        result=original(*args,**kwargs)
        if stop:stop=False;raise OSError('stop after combined PDF committed')
        return result
    monkeypatch.setattr(series,'_merge_pdf',once)
    with pytest.raises(OSError,match='combined PDF'):series.prepare_map_series(b,p['output'],p['candidate_sha256'],params,'merged')
    stage=Path(b.status('merged')['stage']);pdf=stage/'files/series.pdf';before=(file_sha(pdf),pdf.stat().st_mtime_ns)
    result=series.reconcile_map_series(b,'merged');assert result['state']=='PREPARED'
    assert (file_sha(pdf),pdf.stat().st_mtime_ns)==before

@pytest.mark.parametrize('defect',['extra_page','reverse_order'])
def test_document_readback_rejects_wrong_page_count_or_order(tmp_path,monkeypatch,defect):
    import mapgis_agent.map_series as series
    b,p=project(tmp_path);params=parameters();params['include_tiff']=False;run=series.subprocess.run
    def broken(command,*args,**kwargs):
        if '-sDEVICE=pdfwrite' in command and any(v.endswith('series.pdf') for v in command if v.startswith('-sOutputFile=')):
            command=list(command)
            if defect=='extra_page':command.append(command[-1])
            else:command[-2:]=reversed(command[-2:])
        return run(command,*args,**kwargs)
    monkeypatch.setattr(series.subprocess,'run',broken)
    with pytest.raises(BridgeError,match='MAP_SERIES_(PAGE_COUNT|PAGE_SIZE|PIXELS)'):
        series.prepare_map_series(b,p['output'],p['candidate_sha256'],params,'bad-document')


@pytest.mark.parametrize('changed',['index','tiff'])
def test_resume_does_not_publish_a_changed_partial_index_or_tiff(tmp_path,monkeypatch,changed):
    import mapgis_agent.map_series as series
    import rasterio
    from rasterio.windows import Window
    b,p=project(tmp_path);params=parameters();params['pages']=params['pages'][:1];finish=series.finish_bundle
    def stop(*args,**kwargs):raise OSError('owned stop before outer seal')
    monkeypatch.setattr(series,'finish_bundle',stop)
    with pytest.raises(OSError,match='outer seal'):series.prepare_map_series(b,p['output'],p['candidate_sha256'],params,'changed-partial')
    files=Path(b.status('changed-partial')['stage'])/'files'
    if changed=='index':
        with (files/'pages.csv').open('ab') as stream:stream.write(b'changed owned index')
    else:
        with rasterio.open(files/'page-001.tif','r+') as raster:
            pixel=raster.read(window=Window(0,0,1,1));pixel[:]=0;raster.write(pixel,window=Window(0,0,1,1))
    monkeypatch.setattr(series,'finish_bundle',finish)
    with pytest.raises(BridgeError,match='MAP_SERIES_(INDEX|TIFF)_CHANGED'):series.reconcile_map_series(b,'changed-partial')
