from pathlib import Path
import math

import pytest
from openpyxl import Workbook,load_workbook

from mapgis_agent.core import Bridge,BridgeError,read_native,sha

CRS={'kind':'local','name':'samples','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64}


def workbook(tmp_path):
    file=tmp_path/'samples.xlsx';wb=Workbook();ws=wb.active;ws.title='Samples'
    ws.append(['ID','X','Y','W','Sn'])
    for i,z in enumerate([1,2,3,4,5,6,7,8,100,200],1):ws.append([i,i%3,i//3,z,0])
    wb.save(file);wb.close()
    return file,sha(file.read_bytes())


def test_excel_import_preserves_all_raw_samples_and_binds_the_workbook(tmp_path):
    from mapgis_agent.tables import prepare_table_points,table_info
    bridge=Bridge(tmp_path);file,digest=workbook(tmp_path)
    info=table_info(str(file),digest,'Samples',2)
    assert info['row_count']==10 and info['columns']==['ID','X','Y','W','Sn'] and len(info['preview'])==2
    params={'sheet':'Samples','x_field':'X','y_field':'Y','attribute_fields':['ID','W','Sn']}
    p=prepare_table_points(bridge,str(file),digest,params,{'name':'raw.wt','crs':CRS,'map_scale':1000},'raw')
    assert p['metrics']['points']==10
    _,reader,_=read_native(Path(bridge.apply('raw')['output']))
    assert list(reader.data['W'])==[1,2,3,4,5,6,7,8,100,200]
    assert len(reader.geom)==10
    prepare_table_points(bridge,str(file),digest,params,{'name':'raw.wt','crs':CRS,'map_scale':1000},'stale')
    file.write_bytes(file.read_bytes()+b' ')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('stale')


def test_geochemistry_upper_only_screening_uses_sample_sd_and_keeps_raw_mapping(tmp_path):
    from mapgis_agent.geochemistry import prepare_geochemistry
    import json
    bridge=Bridge(tmp_path);file,digest=workbook(tmp_path)
    p=prepare_geochemistry(bridge,str(file),digest,{'sheet':'Samples','elements':['W','Sn'],'id_field':'ID'},'stats')
    assert p['summary']['W']['background_n']==8 and p['summary']['W']['removed_n']==2
    assert p['summary']['W']['mean']==4.5
    assert p['summary']['W']['sample_sd']==pytest.approx(math.sqrt(6))
    assert p['summary']['W']['C']==pytest.approx(9.398979485566356)
    assert p['summary']['Sn']['C']==0 and p['summary']['Sn']['background_n']==10
    applied=bridge.apply('stats');analysis=json.loads(Path(applied['output']).read_text(encoding='utf8'))
    assert analysis['elements']['W']['rounds'][0]['removed_ids']==[9,10]
    assert analysis['elements']['W']['retained_ids']==list(range(1,9))
    report=load_workbook(Path(applied['output']).parent/'analysis.xlsx',read_only=True,data_only=True)
    try:
        rows=list(report['Raw'].values)
        assert len(rows)==11 and rows[-1][3]==200
        assert report['Summary']['D2'].value==4.5
    finally:report.close()
    assert sha(file.read_bytes())==digest
