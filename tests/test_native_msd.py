"""Real CMsiDB native index creation, readback and member deletion; no GUI."""
from pathlib import Path
import json,shutil
import numpy as np
import pytest,rasterio
from rasterio.transform import from_origin
from mapgis_agent.core import Bridge
from mapgis_agent.native_backend import _run_child
from mapgis_agent.native_paths import NativePathAlias
from mapgis_agent.native_raster import prepare_native_raster
from test_native_raster import native_settings


def images(root):
    b=Bridge(root);exe,bas=native_settings(root);paths=[]
    for i,origin in enumerate([100,150]):
        source=root/f'source-{i}.tif'
        with rasterio.open(source,'w',driver='GTiff',width=8,height=8,count=1,dtype='uint8',crs='EPSG:32650',transform=from_origin(origin,200,2,2)) as ds:
            ds.write(np.full((8,8),30+i*100,dtype='uint8'),1)
        from mapgis_agent.core import sha
        prepare_native_raster(b,'to_msi',str(source),sha(source.read_bytes()),{'name':f'image-{i}.msi'},f'image-{i}')
        paths.append(Path(b.apply(f'image-{i}')['output']))
    return b,exe,bas,paths


def command(exe,bas,folder,operation,input_name,output_name,control=None,label='native.json'):
    with NativePathAlias(folder) as alias:
        args=[str(exe),'msd',str(bas),str(bas.parent/'CMsiDB.dll'),operation,
            '-' if input_name=='-' else str(alias.path/input_name),'-' if output_name=='-' else str(alias.path/output_name)]
        if control is not None:args.append(str(alias.path/control))
        return _run_child(args,folder,label)


def test_native_msd_empty_library_can_close_and_reopen_without_source_changes(tmp_path):
    b=Bridge(tmp_path);exe,bas=native_settings(tmp_path)
    (tmp_path/'create.txt').write_text('0 0\n',encoding='ascii')
    first,events=command(exe,bas,tmp_path,'create','-','empty.msd','create.txt','create.json')
    before=(tmp_path/'empty.msd').read_bytes()
    second,events=command(exe,bas,tmp_path,'inspect','empty.msd','-',label='read.json')
    assert next(e for e in events if e['event']=='msd')['members']==0
    assert first['pid']!=second['pid'] and (tmp_path/'empty.msd').read_bytes()==before
    assert b'MsiDataBase' in before or b'MSIDataBase' in before or b'MSIDATABASE' in before.upper()


def test_native_msd_members_relative_paths_extent_and_delete_survive_reopen_and_relocation(tmp_path):
    b,exe,bas,paths=images(tmp_path)
    for i,path in enumerate(paths):shutil.copyfile(path,tmp_path/f'image-{i}.msi')
    (tmp_path/'create.txt').write_text('2 0\nimage-0.msi\nimage-1.msi\n',encoding='ascii')
    command(exe,bas,tmp_path,'create','-','model.msd','create.txt','create.json')
    original=(tmp_path/'model.msd').read_bytes()
    host,events=command(exe,bas,tmp_path,'inspect','model.msd','-',label='read.json')
    names=[e['name'] for e in events if e['event']=='msd_member'];info=next(e for e in events if e['event']=='msd')
    assert names==['image-0.msi','image-1.msi'] and info['members']==2
    assert info['native_bounds']==pytest.approx([100,184,166,200])
    (tmp_path/'edit.txt').write_text('0 1\n0\n',encoding='ascii')
    command(exe,bas,tmp_path,'edit','model.msd','edited.msd','edit.txt','edit.json')
    host,events=command(exe,bas,tmp_path,'inspect','edited.msd','-',label='edited-read.json')
    assert [e['name'] for e in events if e['event']=='msd_member']==['image-1.msi']
    assert next(e for e in events if e['event']=='msd')['native_bounds']==pytest.approx([150,184,166,200])
    assert (tmp_path/'model.msd').read_bytes()==original
    moved=tmp_path/'moved';moved.mkdir()
    for name in ['model.msd','image-0.msi','image-1.msi']:shutil.copyfile(tmp_path/name,moved/name)
    host,events=command(exe,bas,moved,'inspect','model.msd','-',label='relocated.json')
    assert next(e for e in events if e['event']=='msd')['members']==2
