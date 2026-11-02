"""Explicit hash-bound collar, survey and interval workbook mappings."""
from __future__ import annotations
from pathlib import Path
from openpyxl import load_workbook
from .core import BridgeError
from .raster import file_sha
from .tables import read_table
from .borehole import borehole_trajectory,validate_intervals,_frame

FIELDS={'collars':{'hole_id','easting','northing','elevation'},'surveys':{'hole_id','md','inclination','azimuth'},'intervals':{'hole_id','top','base','unit'}}


def _formula_guard(path,tables):
    book=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    try:
        for kind,spec in tables.items():
            if spec['sheet'] not in book.sheetnames:raise BridgeError('BOREHOLE_SHEET: '+spec['sheet'])
            ws=book[spec['sheet']];ws.reset_dimensions();iterator=ws.iter_rows();header=[c.value for c in next(iterator,())]
            names=list(spec['columns'].values())
            if any(name not in header or header.count(name)!=1 for name in names):raise BridgeError('BOREHOLE_COLUMNS: every mapped workbook header must exist once')
            used=[header.index(name) for name in names]
            for number,row in enumerate(iterator,2):
                if number>200002:raise BridgeError('BOREHOLE_CAPACITY: split source worksheet beyond200000 rows')
                if any(i<len(row) and row[i].data_type=='f' for i in used):raise BridgeError(f'BOREHOLE_FORMULA: {spec["sheet"]} row{number}; supply actual measured values or an explicitly recalculated value-only copy, not an unverified formula cache')
    finally:book.close()


def read_borehole_workbook(file,expected_sha256,tables,coordinate_frame,*,sort_by_depth=False):
    frame=_frame(coordinate_frame)
    if type(sort_by_depth) is not bool or not isinstance(tables,dict) or set(tables)!=set(FIELDS):raise BridgeError('BOREHOLE_TABLES: explicit collars/surveys/intervals bindings and boolean sort_by_depth required')
    for kind,spec in tables.items():
        if (not isinstance(spec,dict) or set(spec)!={'sheet','columns'} or not isinstance(spec['sheet'],str) or not spec['sheet'] or not isinstance(spec['columns'],dict)
                or not FIELDS[kind]<=set(spec['columns'])<=(FIELDS[kind]|({'description'} if kind=='intervals' else set()))
                or any(not isinstance(v,str) or not v for v in spec['columns'].values()) or len(set(spec['columns'].values()))!=len(spec['columns'])):
            raise BridgeError('BOREHOLE_TABLES: exact sheet and unique field-to-column mappings required')
    path=Path(file).resolve(strict=True)
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if path.suffix.lower()!='.xlsx':raise BridgeError('BOREHOLE_TABLES: XLSX input required')
    _formula_guard(path,tables)
    source_tables={}
    for kind,spec in tables.items():
        _,table=read_table(str(path),expected_sha256,spec['sheet']);indexes={key:table['columns'].index(name) for key,name in spec['columns'].items()}
        source_tables[kind]=[(number,{key:values[i] for key,i in indexes.items()}) for number,values in zip(table['worksheet_rows'],table['rows'])]
    holes={}
    for number,row in source_tables['collars']:
        identity=row['hole_id']
        if not isinstance(identity,str) or not identity.strip():raise BridgeError(f'BOREHOLE_ID: explicit string collar identity required at row{number}')
        if identity in holes:raise BridgeError(f'BOREHOLE_DUPLICATE: collar{identity}')
        holes[identity]={'id':identity,'collar':{k:row[k] for k in ('easting','northing','elevation')},'survey':[],'intervals':[],
            'source_rows':{'collar':number,'surveys':[],'intervals':[]}}
    if len(holes)>100:raise BridgeError('BOREHOLE_CAPACITY: split beyond100 collars per input')
    for kind in ('surveys','intervals'):
        for number,row in source_tables[kind]:
            identity=row.pop('hole_id')
            if not isinstance(identity,str) or identity not in holes:raise BridgeError(f'BOREHOLE_UNKNOWN: {kind} row{number} references no exact collar ID')
            if kind=='intervals' and row.get('description') is None:row['description']=''
            key='survey' if kind=='surveys' else 'intervals';holes[identity][key].append(row);holes[identity]['source_rows'][kind].append(number)
    for hole in holes.values():
        if sort_by_depth:
            for key,source_key,depth in [('survey','surveys','md'),('intervals','intervals','top')]:
                # Invalid numeric types are diagnosed by the model, never
                # lexically sorted or silently converted from text.
                if any(type(r[depth]) not in (int,float) for r in hole[key]):raise BridgeError('BOREHOLE_SURVEY: numeric measured-depth cells required')
                pairs=sorted(zip(hole[key],hole['source_rows'][source_key]),key=lambda item:item[0][depth])
                hole[key]=[r for r,_ in pairs];hole['source_rows'][source_key]=[number for _,number in pairs]
        borehole_trajectory(hole['survey'],hole['collar'],frame)
        hole['intervals'],hole['unlogged_intervals']=validate_intervals(hole['intervals'],hole['survey'][-1]['md'])
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_DURING_TABLE_READ')
    return {'coordinate_frame':frame,'holes':list(holes.values()),'source':{'file':str(path),'sha256':expected_sha256,'tables':tables,'sort_by_depth':sort_by_depth},
        'source_preserved':True,'formula_policy':'used cells must be literal measurements; no cached formula evaluation',
        'missing_intervals':'unlogged intervals retained as gaps; no stratigraphic interpolation or cross-hole correlation'}
