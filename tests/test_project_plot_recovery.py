"""A completed native project plot survives loss before its artifact was sealed."""
import asyncio,json
from pathlib import Path
import pytest
from shapely.geometry import box,mapping
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_project import prepare_native_project
from mapgis_agent.raster import file_sha
from mapgis_agent.server import build_server
from test_native_raster import native_settings

def project(tmp_path):
    b=Bridge(tmp_path);native_settings(tmp_path)
    frame={'kind':'local','name':'plot recovery','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),
        'geometry':mapping(box(i*5,0,i*5+4,8)),'properties':{'ID':i},'style':{'fill_color':i+2,'pattern':0}} for i in [0,1]]}
    prepare_creation(b,features,{'name':'areas.wp','crs':frame,'map_scale':1000},'areas');area=b.apply('areas')
    prepare_native_project(b,[{'file':area['output'],'expected_sha256':area['candidate_sha256']}],
        {'name':'areas.mpj','coordinate_frame':'native'},'project');return b,b.apply('project')

def stop_after_native(monkeypatch):
    import mapgis_agent.native_project as native
    original=native._run_child;completed=[]
    def stop(args,folder,log_name):
        result=original(args,folder,log_name)
        if args[1:3]==['project','plot.ps']:
            completed.append(result[0]);raise OSError('owned stop after native project PS completed')
        return result
    monkeypatch.setattr(native,'_run_child',stop);return completed

def produce_interrupted(tmp_path,monkeypatch):
    import mapgis_agent.native_project as native
    b,p=project(tmp_path);completed=stop_after_native(monkeypatch)
    with pytest.raises(OSError,match='owned stop'):
        native.prepare_project_plot(b,p['output'],p['candidate_sha256'],
            {'native_bounds':[-1,-1,10,9],'paper_mm':[110,100],'coordinate_frame':'native'},'plot')
    return b,p,completed

def test_public_reconcile_uses_the_completed_native_ps_without_another_native_plot(tmp_path,monkeypatch):
    b,p,completed=produce_interrupted(tmp_path,monkeypatch);stage=Path(b.status('plot')['stage']);ps=stage/'map.ps'
    before=(file_sha(ps),ps.stat().st_mtime_ns);raw=(stage/'native/project-plot.json.stdout').read_bytes()
    result=asyncio.run(build_server(tmp_path).call_tool('mapgis_reconcile_artifact',{'request_id':'plot'}))
    restored=json.loads(result[0].text);assert restored['state']=='PREPARED' and restored['reconciled_after_interruption']
    assert restored['native_execution']['pid']==completed[0]['pid'] and restored['replayed'] is False
    applied=b.apply('plot');assert Path(applied['output']).is_file()
    assert (file_sha(ps),ps.stat().st_mtime_ns)==before
    assert (stage/'native/project-plot.json.stdout').read_bytes()==raw and len(completed)==1
    import mapgis_agent.native_project as native
    again=native.prepare_project_plot(b,p['output'],p['candidate_sha256'],
        {'native_bounds':[-1,-1,10,9],'paper_mm':[110,100],'coordinate_frame':'native'},'plot')
    assert again['state']=='APPLIED' and len(completed)==1

@pytest.mark.parametrize('changed,expected',[('context','PLOT_RECOVERY_CONTEXT_CHANGED'),('source','STALE_SOURCE'),
    ('snapshot','PLOT_PROJECT_SNAPSHOT_CHANGED'),('view','PLOT_VIEW_SNAPSHOT_CHANGED'),('postscript','PLOT_POSTSCRIPT_CHANGED')])
def test_recovery_rejects_changed_frozen_inputs_or_native_candidate(tmp_path,monkeypatch,changed,expected):
    import mapgis_agent.native_project as native
    b,p,completed=produce_interrupted(tmp_path,monkeypatch);stage=Path(b.status('plot')['stage'])
    target={'context':stage/'plot-context.json','source':Path(p['output']),
        'snapshot':next((stage/'project').glob('*.wp')),'view':stage/'view.txt','postscript':stage/'map.ps'}[changed]
    with target.open('ab') as stream:stream.write(b'owned tamper')
    with pytest.raises(BridgeError,match=expected):native.reconcile_project_plot(b,'plot')
    assert len(completed)==1

def test_recovery_rebuilds_only_partial_conversion_files_after_a_failed_preview(tmp_path,monkeypatch):
    import mapgis_agent.native_project as native
    import mapgis_agent.native_plot as plot
    b,p=project(tmp_path);original=plot._render_check;stopped=False
    def stop(executable,folder,source,paper,**kwargs):
        nonlocal stopped
        result=original(executable,folder,source,paper,**kwargs)
        if folder.name=='files' and not stopped:
            stopped=True;raise OSError('owned stop after final preview')
        return result
    monkeypatch.setattr(plot,'_render_check',stop)
    with pytest.raises(OSError,match='owned stop'):
        native.prepare_project_plot(b,p['output'],p['candidate_sha256'],
            {'native_bounds':[-1,-1,10,9],'paper_mm':[110,100],'coordinate_frame':'native'},'plot')
    stage=Path(b.status('plot')['stage']);ps=stage/'map.ps';before=(file_sha(ps),ps.stat().st_mtime_ns)
    log=(stage/'native/project-plot.json.stdout').read_bytes()
    result=native.reconcile_project_plot(b,'plot');assert result['state']=='PREPARED' and result['reconciled_after_interruption']
    assert (file_sha(ps),ps.stat().st_mtime_ns)==before and (stage/'native/project-plot.json.stdout').read_bytes()==log
    assert len(list(stage.glob('incomplete-files-*')))==1
    assert b.apply('plot')['state']=='APPLIED'
