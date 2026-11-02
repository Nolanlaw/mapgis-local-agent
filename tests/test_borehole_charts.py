"""Editable borehole drawings keep MD/elevation, gaps and native data distinct."""
import math
from pathlib import Path
import numpy as np
import pytest
from shapely.geometry import shape
from mapgis_agent.core import Bridge,BridgeError,read_native,annotation_text
from mapgis_agent.raster import file_sha
from test_borehole_math import FRAME,COLLAR
from test_native_raster import native_settings

def dataset():
    return {'coordinate_frame':FRAME,'holes':[{'id':'ZK1','collar':COLLAR,
        'survey':[{'md':0,'inclination':0,'azimuth':90},{'md':100,'inclination':90,'azimuth':90}],
        'intervals':[{'top':0,'base':30,'unit':'S','description':'砂岩'},{'top':40,'base':100,'unit':'M','description':'泥岩'}]},
        {'id':'ZK2','collar':{'easting':1100.,'northing':2000.,'elevation':305.},
        'survey':[{'md':0,'inclination':0,'azimuth':0},{'md':100,'inclination':0,'azimuth':0}],
        'intervals':[{'top':0,'base':100,'unit':'S'}]}]}

def parameters(mode):
    return {'mode':mode,'title':'钻孔技术验证示例','vertical_scale':1000. if mode=='columns' else 500.,'column_width_mm':10.,'font_mm':3.,
        'tick_interval_metres':25.,'lithology_styles':{'S':{'fill_color':9,'pattern':1,'pattern_color':1,'pattern_height':2.,'pattern_width':2.},'M':{'fill_color':9,'pattern':2,'pattern_color':1,'pattern_height':2.,'pattern_width':2.}},
        **({'column_spacing_mm':90.} if mode=='columns' else {'horizontal_scale':1000.,'reference_elevation':300.,'sample_step_md':2.,
            'section':{'start':[1000.,2000.],'end':[1200.,2000.],'max_crossline_distance':1.}})}


def test_column_thickness_is_measured_depth_and_unlogged_gap_is_not_filled():
    from mapgis_agent.borehole_charts import borehole_chart_spec
    spec=borehole_chart_spec(dataset(),parameters('columns'));regions=spec['features']['POLYGON']['features']
    assert len(regions)==3
    first=regions[0];assert shape(first['geometry']).area==pytest.approx(300.)
    assert first['properties']['MD_TOP']==0 and first['properties']['MD_BASE']==30
    assert spec['unlogged_intervals']==[{'hole_id':'ZK1','top':30.,'base':40.}]
    texts=[v['text'] for v in spec['features']['POINT']['features'] if 'text' in v]
    assert any('未编录' in s for s in texts) and any('测深' in s and 'm' in s for s in texts)
    assert spec['drawing_contract']['vertical_basis']=='measured_depth' and spec['drawing_contract']['horizontal_basis']=='layout_spacing'


def test_section_projection_uses_elevation_and_full_minimum_curvature_samples_without_cross_hole_contacts():
    from mapgis_agent.borehole_charts import borehole_chart_spec
    spec=borehole_chart_spec(dataset(),parameters('section'));lines=spec['features']['LINE']['features']
    trajectory=next(v for v in lines if v['properties']['ROLE']=='trajectory' and v['properties']['HOLE']=='ZK1')
    end=shape(trajectory['geometry']).coords[-1];radius=100/(math.pi/2)
    np.testing.assert_allclose(end,[radius,-2*radius],atol=1e-8,rtol=0)
    assert spec['drawing_contract']['vertical_exaggeration']==2 and spec['drawing_contract']['vertical_basis']=='elevation'
    assert not any(v['properties']['ROLE']=='correlation' for v in lines)
    assert spec['trajectory_samples'] and spec['drawing_contract']['native_3d_file'] is False


def test_section_rejects_wrong_plane_and_missing_explicit_lithology_style():
    from mapgis_agent.borehole_charts import borehole_chart_spec
    p=parameters('section');p['section']={'start':[1000,2100],'end':[1200,2100],'max_crossline_distance':1.}
    with pytest.raises(BridgeError,match='BOREHOLE_SECTION_OFFSET'):borehole_chart_spec(dataset(),p)
    p=parameters('columns');p['lithology_styles'].pop('M')
    with pytest.raises(BridgeError,match='BOREHOLE_LITHOLOGY'):borehole_chart_spec(dataset(),p)


@pytest.mark.parametrize('mode',['columns','section'])
def test_three_native_layers_and_result_workbook_save_reopen_and_repeat_with_original_inputs_preserved(tmp_path,mode):
    from mapgis_agent.borehole_charts import prepare_borehole_chart
    from openpyxl import load_workbook
    b=Bridge(tmp_path);native_settings(tmp_path);data=dataset();params=parameters(mode)
    prepared=prepare_borehole_chart(b,data,params,{'name':'boreholes.wp'},'chart')
    result=b.apply('chart');primary=Path(result['output']);assert primary.suffix=='.wp'
    for suffix,kind in [('.wp','POLYGON'),('.wl','LINE'),('.wt','POINT')]:
        file=primary.with_suffix(suffix);blob,reader,sections=read_native(file)
        assert reader.shapeType==kind and len(reader.geom)>0 and prepared['native_files'][kind]['active']==len(reader.geom)
        assert prepared['native_files'][kind]['source_preserved']
        if kind=='POINT':
            text=[annotation_text(blob,sections,i) for i in range(1,len(reader.geom)+1)]
            assert any(v and '未编录' in v for v in text)
    book=load_workbook(primary.with_suffix('.xlsx'),read_only=True,data_only=True)
    assert {'孔口','测斜','分层','轨迹采样','未编录','说明'}<=set(book.sheetnames)
    assert book['分层'].max_row==4 and book['未编录'].max_row==2;book.close()
    hashes={s:file_sha(primary.with_suffix(s)) for s in ['.wp','.wl','.wt','.xlsx','.json']}
    again=prepare_borehole_chart(b,data,params,{'name':'boreholes.wp'},'chart');assert again['state']=='APPLIED'
    assert all(file_sha(primary.with_suffix(s))==value for s,value in hashes.items())


def test_workbook_source_is_guarded_until_publication(tmp_path):
    from mapgis_agent.borehole_charts import prepare_borehole_chart
    from test_borehole_tables import workbook,TABLES
    b=Bridge(tmp_path);native_settings(tmp_path);file=tmp_path/'source.xlsx';workbook(file)
    data={'coordinate_frame':FRAME,'workbook':{'file':str(file),'expected_sha256':file_sha(file),'tables':TABLES},'sort_by_depth':True}
    p=parameters('columns');p['lithology_styles']={'砂岩':p['lithology_styles']['S'],'泥岩':p['lithology_styles']['M']}
    prepare_borehole_chart(b,data,p,{'name':'from-excel.wp'},'excel')
    with file.open('ab') as stream:stream.write(b'changed owned test fixture')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('excel')


def test_resume_after_first_completed_native_save_never_runs_that_save_again(tmp_path,monkeypatch):
    import mapgis_agent.borehole_charts as chart
    b=Bridge(tmp_path);native_settings(tmp_path);original=chart.native_file_info;stopped=False
    def once(root,file,digest):
        nonlocal stopped
        result=original(root,file,digest)
        if str(file).endswith('.wp') and not stopped:stopped=True;raise OSError('owned interruption after first native save')
        return result
    monkeypatch.setattr(chart,'native_file_info',once)
    with pytest.raises(OSError,match='owned interruption'):
        chart.prepare_borehole_chart(b,dataset(),parameters('columns'),{'name':'recover.wp'},'recover')
    receipt=b.status('recover');stage=Path(receipt['stage']);encoder=Bridge(stage/'encoder')
    finished=encoder.status('polygon-save');saved=Path(finished['output']);before=(file_sha(saved),saved.stat().st_mtime_ns)
    monkeypatch.setattr(chart,'native_file_info',original)
    result=chart.reconcile_borehole_chart(b,'recover');assert result['state']=='PREPARED' and result['reconciled_after_interruption']
    b.apply('recover');assert (file_sha(saved),saved.stat().st_mtime_ns)==before
    assert encoder.status('polygon-save')['native_execution']['pid']==finished['native_execution']['pid']


def test_recovery_rejects_a_modified_intermediate_spec(tmp_path,monkeypatch):
    import mapgis_agent.borehole_charts as chart
    b=Bridge(tmp_path);native_settings(tmp_path)
    def stop(*args,**kwargs):raise OSError('stop before native work')
    monkeypatch.setattr(chart,'_advance',stop)
    with pytest.raises(OSError):chart.prepare_borehole_chart(b,dataset(),parameters('columns'),{'name':'locked.wp'},'locked')
    stage=Path(b.status('locked')['stage']);(stage/'chart-spec.json').write_text('{}',encoding='utf8')
    with pytest.raises(BridgeError,match='BOREHOLE_RECOVERY_SPEC_CHANGED'):chart.reconcile_borehole_chart(b,'locked')
