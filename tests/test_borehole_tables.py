"""Explicit workbook bindings preserve originals, station order and missing strata."""
from pathlib import Path
import pytest
from openpyxl import Workbook,load_workbook
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from test_borehole_math import FRAME

TABLES={'collars':{'sheet':'孔口','columns':{'hole_id':'孔号','easting':'东坐标','northing':'北坐标','elevation':'高程'}},
    'surveys':{'sheet':'测斜','columns':{'hole_id':'孔号','md':'孔深','inclination':'倾斜角','azimuth':'方位角'}},
    'intervals':{'sheet':'分层','columns':{'hole_id':'孔号','top':'顶深','base':'底深','unit':'地层','description':'说明'}}}


def workbook(file):
    book=Workbook();book.remove(book.active)
    data={'孔口':[['孔号','东坐标','北坐标','高程'],['ZK1',1000,2000,300],['ZK2',1100,2000,305]],
        '测斜':[['孔号','孔深','倾斜角','方位角'],['ZK1',100,30,90],['ZK2',0,0,0],['ZK1',0,30,90],['ZK2',100,0,0]],
        '分层':[['孔号','顶深','底深','地层','说明'],['ZK1',0,30,'砂岩','显式编录'],['ZK1',40,100,'泥岩','10m空白'],['ZK2',0,100,'砂岩','']]}
    for name,rows in data.items():
        ws=book.create_sheet(name)
        for row in rows:ws.append(row)
    book.save(file)


def test_explicit_bindings_sort_only_when_requested_and_keep_gap_and_row_lineage(tmp_path):
    from mapgis_agent.borehole_tables import read_borehole_workbook
    file=tmp_path/'drill.xlsx';workbook(file);before=file.read_bytes()
    with pytest.raises(BridgeError,match='BOREHOLE_SURVEY'):
        read_borehole_workbook(str(file),file_sha(file),TABLES,FRAME)
    result=read_borehole_workbook(str(file),file_sha(file),TABLES,FRAME,sort_by_depth=True)
    first,second=result['holes'];assert first['id']=='ZK1' and second['id']=='ZK2'
    assert [r['md'] for r in first['survey']]==[0,100] and first['source_rows']['surveys']==[4,2]
    assert first['unlogged_intervals']==[{'top':30.,'base':40.}]
    assert first['source_rows']['collar']==2 and result['source_preserved'] and file.read_bytes()==before


@pytest.mark.parametrize('sheet,cell,value,match',[('孔口','A3','ZK1','BOREHOLE_DUPLICATE'),('测斜','A2','unknown','BOREHOLE_UNKNOWN'),
    ('分层','B3',20,'BOREHOLE_INTERVALS'),('测斜','B2','=50+50','BOREHOLE_FORMULA'),('孔口','B2','1000','BOREHOLE_COLLAR')])
def test_bad_workbook_cells_do_not_get_silently_coerced_or_filled(tmp_path,sheet,cell,value,match):
    from mapgis_agent.borehole_tables import read_borehole_workbook
    file=tmp_path/'bad.xlsx';workbook(file);book=load_workbook(file);book[sheet][cell]=value;book.save(file)
    with pytest.raises(BridgeError,match=match):read_borehole_workbook(str(file),file_sha(file),TABLES,FRAME,sort_by_depth=True)
