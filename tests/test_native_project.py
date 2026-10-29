import json
from pathlib import Path
import shutil

import pytest
from shapely.geometry import Point,LineString,box,mapping

from mapgis_agent.core import Bridge,sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import _run_child
from test_native_raster import native_settings

LOCAL={'kind':'local','name':'project test','axis_order':'xy','unit':'unknown','reference_sha256':'0'*64}


def test_native_project_layers_survive_portable_relocation_and_fresh_open(tmp_path):
    bridge=Bridge(tmp_path);executable,bas=native_settings(tmp_path)
    original={};directory=tmp_path/'work/project';directory.mkdir()
    for index,(geometry,extension) in enumerate([(Point(1,2),'wt'),(LineString([(0,0),(3,4)]),'wl'),(box(0,0,10,10),'wp')],1):
        identity='layer-'+str(index)
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':mapping(geometry),'properties':{'ID':7}}]},
            {'name':identity+'.'+extension,'crs':LOCAL,'map_scale':1000},identity)
        source=Path(bridge.apply(identity)['output']);original[source]=source.read_bytes()
        shutil.copyfile(source,directory/source.name)
    items=directory/'layers.txt';items.write_text('3\nlayer-1.wt\nlayer-2.wl\nlayer-3.wp\n',encoding='ascii')
    project=directory/'map.mpj'
    first,events=_run_child([str(executable),'project','create',str(bas.parent),str(items),str(project)],tmp_path,'create-project.json')
    assert project.is_file() and events[-2]['event']=='project_closed'
    relocated=tmp_path/'work/relocated';shutil.copytree(directory,relocated)
    # Hide the original staging directory. An absolute-path dependency cannot
    # accidentally pass the new-location reopen test.
    hidden=directory.with_name('hidden')
    assert directory.resolve().is_relative_to((tmp_path/'work').resolve()) and hidden.resolve().is_relative_to((tmp_path/'work').resolve())
    directory.rename(hidden)
    second,events=_run_child([str(executable),'project','inspect',str(bas.parent),str(relocated/'map.mpj'),'-'],tmp_path,'reopen-project.json')
    assert first['pid']!=second['pid']
    layers=[e for e in events if e['event']=='project_layer']
    assert len(layers)==3 and [e['kind'] for e in layers]==[1,0,2]
    assert [e['active'] for e in layers]==[1,1,1]
    assert all(source.read_bytes()==blob for source,blob in original.items())


def test_project_bundle_publish_and_reopen_after_stage_is_hidden(tmp_path):
    from mapgis_agent.native_project import prepare_native_project,native_project_info
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':mapping(Point(1,2)),'properties':{'ID':9}}]},
        {'name':'source.wt','crs':LOCAL,'map_scale':1000},'point')
    source=Path(bridge.apply('point')['output']);before=source.read_bytes()
    preview=prepare_native_project(bridge,[{'file':str(source),'expected_sha256':sha(before)}],{'name':'test.mpj','coordinate_frame':'native'},'project')
    assert preview['portable_relocation']=='PASS_WITH_OLD_DIRECTORY_HIDDEN'
    result=bridge.apply('project');assert bridge.status('project')['output_matches']
    # Reopen the published set without either private staging location available.
    stage=Path(result['stage']);hidden=stage.with_name(stage.name+'-hidden')
    assert stage.resolve().is_relative_to((tmp_path/'work').resolve()) and hidden.resolve().is_relative_to((tmp_path/'work').resolve())
    stage.rename(hidden)
    info=native_project_info(tmp_path,result['output'],result['candidate_sha256'])
    assert len(info['layers'])==1 and info['layers'][0]['active']==1 and info['layers'][0]['kind']==1
    assert source.read_bytes()==before


def test_native_project_accepts_native_msi_background_with_bound_extent(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from mapgis_agent.native_raster import prepare_native_raster
    from mapgis_agent.native_project import prepare_native_project,native_project_info
    bridge=Bridge(tmp_path);native_settings(tmp_path);source=tmp_path/'raster.tif'
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=1,dtype='uint8',crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:
        raster.write(np.arange(32*24,dtype=np.uint8).reshape(1,24,32))
    prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},'msi')
    msi=bridge.apply('msi');before=Path(msi['output']).read_bytes()
    preview=prepare_native_project(bridge,[{'file':msi['output'],'expected_sha256':msi['candidate_sha256']}],
        {'name':'raster.mpj','coordinate_frame':'native'},'project')
    assert preview['portable_relocation']=='PASS_WITH_OLD_DIRECTORY_HIDDEN'
    result=bridge.apply('project')
    info=native_project_info(tmp_path,result['output'],result['candidate_sha256'])
    assert len(info['layers'])==1 and info['layers'][0]['kind']==11
    assert info['layers'][0]['native_bounds']==pytest.approx([400,852,464,900])
    assert Path(msi['output']).read_bytes()==before
