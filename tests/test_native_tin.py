"""The file must be native TIN, independently reopened and geometrically valid."""
import json
from pathlib import Path
import struct

import numpy as np
import pytest

from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.native_backend import _run_child
from mapgis_agent.native_paths import NativePathAlias
from test_native_raster import native_settings


@pytest.mark.parametrize('offset',[0,1,2,3])
def test_cdt_saves_real_mapgis_tin_and_fresh_native_reader_returns_plane(tmp_path,offset):
    Bridge(tmp_path);executable,bas=native_settings(tmp_path)
    xyz=[(x+offset*1000,y,100+.1*(x+offset*1000)+.2*y) for y in range(0,101,25) for x in range(0,101,25)]
    command=tmp_path/'points.txt';command.write_text(str(len(xyz))+'\n'+''.join(' '.join(map(str,p))+'\n' for p in xyz),encoding='ascii')
    output=tmp_path/'plane.tin'
    with NativePathAlias(tmp_path) as alias:
        first,events=_run_child([str(executable),'terrain','tin',str(bas.parent/'W60_Tin.dll'),str(alias.path/command.name),str(alias.path/output.name)],tmp_path,'build.json')
    before=output.read_bytes();assert before[:4]==b'WTIN'
    with NativePathAlias(tmp_path) as alias:
        second,readback=_run_child([str(executable),'terrain','tin.inspect',str(bas.parent/'W60_Tin.dll'),str(alias.path/output.name),str(alias.path/'model.bin')],tmp_path,'read.json')
    assert first['pid']!=second['pid'] and output.read_bytes()==before
    raw=(tmp_path/'model.bin').read_bytes();magic,n,m=struct.unpack_from('<8s2I',raw)
    assert magic==b'MGNT0001' and n==25 and m==32
    points=np.dtype([('x','<f8'),('y','<f8'),('z','<f8'),('reserved','<u2'),('state','i1'),('pad','u1')])
    nets=np.dtype([('vertices','<i4',(3,)),('neighbors','<i4',(3,)),('state','i1'),('pad','u1')])
    p=np.frombuffer(raw,dtype=points,count=n+1,offset=16)
    t=np.frombuffer(raw,dtype=nets,count=m+1,offset=16+(n+1)*28)
    assert len(raw)==16+(n+1)*28+(m+1)*26
    np.testing.assert_allclose(p['z'][1:],100+.1*p['x'][1:]+.2*p['y'][1:],atol=1e-10)
    assert set(zip(p['x'][1:],p['y'][1:]))=={(x,y) for x,y,z in xyz}
    assert np.all(p['state'][1:]==1) and np.all(t['state'][1:]==1)
    assert t['vertices'][1:].min()>=1 and t['vertices'][1:].max()<=n
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    triangles=[Polygon([(p['x'][v],p['y'][v]) for v in row]) for row in t['vertices'][1:]]
    assert sum(g.area for g in triangles)==pytest.approx(10000)
    assert unary_union(triangles).area==pytest.approx(10000)
    assert next(e for e in readback if e['event']=='tin')['points']==25


def test_native_constraint_forces_an_existing_point_edge_without_changing_heights(tmp_path):
    Bridge(tmp_path);executable,bas=native_settings(tmp_path)
    xyz=[(0,0,100),(100,0,110),(100,100,130),(0,100,120),(40,60,150)]
    command=tmp_path/'points.txt'
    command.write_text('5 1\n'+''.join(' '.join(map(str,p))+'\n' for p in xyz)+'0 2\n',encoding='ascii')
    with NativePathAlias(tmp_path) as alias:
        _run_child([str(executable),'terrain','tin',str(bas.parent/'W60_Tin.dll'),str(alias.path/command.name),str(alias.path/'ridge.tin')],tmp_path,'build.json')
        _run_child([str(executable),'terrain','tin.inspect',str(bas.parent/'W60_Tin.dll'),str(alias.path/'ridge.tin'),str(alias.path/'model.bin')],tmp_path,'read.json')
    raw=(tmp_path/'model.bin').read_bytes();magic,n,m=struct.unpack_from('<8s2I',raw)
    assert (n,m)==(5,4)
    actual=[struct.unpack_from('<3d',raw,16+28*i) for i in range(1,n+1)]
    assert actual==xyz
    triangles=[struct.unpack_from('<3i',raw,16+28*(n+1)+26*i) for i in range(1,m+1)]
    assert any(1 in tri and 3 in tri for tri in triangles)


def test_native_tin_bundle_through_public_mcp_and_readback_preserves_bound_frame(tmp_path):
    import asyncio
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.server import build_server
    from mapgis_agent.native_tin import native_tin_info
    from test_grid_analysis import FRAME
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    features=[{'type':'Feature','id':str(i+1),'geometry':{'type':'Point','coordinates':[x,y]},
        'properties':{'ID':i+1,'Z':100+.1*x+.2*y}} for i,(x,y) in enumerate((x,y) for y in range(0,101,25) for x in range(0,101,25))]
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'points.wt','map_scale':1000,'crs':FRAME},'points')
    source=bridge.apply('points');before=Path(source['output']).read_bytes()
    async def scenario():
        server=build_server(tmp_path)
        reply=await server.call_tool('mapgis_prepare_terrain',{'file':source['output'],'expected_sha256':source['candidate_sha256'],'height_field':'Z',
            'parameters':{'operation':'native.tin','vertical_unit':'metre'},'template':{'name':'plane.tin','crs':FRAME},'request_id':'native-model'})
        result=json.loads(''.join(c.text for c in reply if c.type=='text'))
        assert result['state']=='PREPARED' and result['metrics']['triangle_count']==32
        assert result['backend'].startswith('CDT 2.0.1')
    asyncio.run(scenario());saved=bridge.apply('native-model')
    model=native_tin_info(tmp_path,saved['output'],saved['candidate_sha256'],base_height=100.)
    assert model['metrics']['point_count']==25 and model['metrics']['planar_area']==pytest.approx(10000)
    assert model['metrics']['surface_area']==pytest.approx(10000*np.sqrt(1.05))
    assert model['metrics']['cut_volume']==pytest.approx(150000)
    assert model['frame']['crs']==FRAME and model['frame']['vertical_unit']=='metre'
    assert Path(source['output']).read_bytes()==before


def test_tin_vectors_are_derived_from_saved_native_faces_and_source_guarded(tmp_path):
    from mapgis_agent.native_tin import prepare_tin_vectors,prepare_native_tin
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.core import read_native
    from test_grid_analysis import FRAME
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    xy=[(0,0),(100,0),(100,100),(0,100),(40,60)]
    features=[{'type':'Feature','id':str(i+1),'geometry':{'type':'Point','coordinates':list(p)},'properties':{'ID':i+1,'Z':100+.1*p[0]+.2*p[1]}} for i,p in enumerate(xy)]
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'source.wt','map_scale':1000,'crs':FRAME},'source')
    source=bridge.apply('source')
    prepare_native_tin(bridge,source['output'],source['candidate_sha256'],{'operation':'native.tin','height_field':'Z','vertical_unit':'metre','constraints':[[1,3]]},
        {'name':'model.tin','crs':FRAME},'model');model=bridge.apply('model')
    before=Path(model['output']).read_bytes()
    for part,suffix,count in [('nodes','wt',5),('faces','wp',4),('edges','wl',8)]:
        prepared=prepare_tin_vectors(bridge,model['output'],model['candidate_sha256'],part,
            {'name':part+'.'+suffix,'map_scale':1000,'crs':FRAME},part)
        result=bridge.apply(part);reader=read_native(Path(result['output']))[1]
        assert len(reader.geom)==count and prepared['metrics']['features']==count
        if part=='faces':assert sum(g.area for g in reader.geom)==pytest.approx(10000)
        if part=='edges':assert 1 in reader.data['constraint'].values
    assert Path(model['output']).read_bytes()==before
    prepare_tin_vectors(bridge,model['output'],model['candidate_sha256'],'nodes',{'name':'stale.wt','map_scale':1000,'crs':FRAME},'stale')
    Path(model['output']+'.json').write_text('{}',encoding='utf8')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('stale')
