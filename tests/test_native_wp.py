"""WP deletion must remove ownership while preserving neighbours and holes."""
import json
import struct
import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box, mapping

from mapgis_agent.core import Bridge, BridgeError, native_ids, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import native_file_info, prepare_native_operation

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {'kind':'local','name':'analytic WP','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64}


@pytest.fixture
def wp_case(tmp_path):
    bridge = Bridge(tmp_path)
    geometries = [box(0,0,10,10), box(10,0,20,10),
        Polygon([(0,20),(20,20),(20,40),(0,40)], holes=[[(5,25),(15,25),(15,35),(5,35)]]),
        box(7,27,13,33)]
    features = {'type':'FeatureCollection','features':[
        {'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':7,'name':str(i)},
         'style':{'fill_color':10+i}} for i,g in enumerate(geometries,1)]}
    prepare_creation(bridge, features, {'name':'regions.wp','map_scale':1000,'crs':LOCAL}, 'create')
    source = Path(bridge.apply('create')['output'])
    return bridge, source


def enable_native(bridge):
    config = ROOT / 'config.local.json'
    if sys.platform != 'win32' or not config.exists():
        pytest.skip('Requires the explicitly enabled local MapGIS native backend')
    settings = json.loads(config.read_text(encoding='utf8'))
    if not settings.get('native_backend_legacy_dep_compatibility'):
        pytest.skip('No explicit process-only legacy host opt-in')
    (bridge.root/'config.local.json').write_text(json.dumps({key:settings[key] for key in
        ('mapgis_installation','native_backend_legacy_dep_compatibility')}), encoding='utf8')


@pytest.mark.parametrize('removed,remaining,areas', [([1,3],[2,4],[100,36]), ([2],[1,3,4],[100,300,36]), ([1,2,3,4],[],[])])
def test_native_wp_delete_reopens_preserves_neighbours_and_recovers_exactly(wp_case,removed,remaining,areas):
    bridge, source = wp_case
    enable_native(bridge)
    before = source.read_bytes()
    receipt = prepare_native_operation(bridge,'feature.delete',str(source),sha(before),{'record_ids':removed},'delete')
    assert receipt['state']=='PREPARED'
    assert receipt['comparison']['removed_ids']==removed
    assert receipt['comparison']['changed_ids']==[]
    assert receipt['comparison']['native_topology']['candidate']['status']=='PASS'
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    result = bridge.apply('delete')
    _,reader,_ = read_native(Path(result['output']))
    assert native_ids(reader)==remaining
    assert [g.area for g in reader.geom]==pytest.approx(areas)
    if remaining==[1,3,4]:
        donut = reader.geom[1]
        parts = list(donut.geoms) if donut.geom_type=='MultiPolygon' else [donut]
        assert sum(len(part.interiors) for part in parts)==1
    assert native_file_info(bridge.root,result['output'],result['candidate_sha256'])['active']==len(remaining)
    assert source.read_bytes()==before
    repeated = prepare_native_operation(bridge,'feature.delete',str(source),sha(before),{'record_ids':removed},'delete')
    assert repeated['state']=='APPLIED' and repeated['native_execution']['pid']==receipt['native_execution']['pid']
    restored = bridge.recover('delete',result['output'],result['candidate_sha256'],'undo')
    assert Path(bridge.apply(restored['id'])['output']).read_bytes()==before


def test_wp_topology_rejects_dangling_owner_and_disconnected_walk(wp_case):
    from mapgis_agent.topology import validate_wp_topology
    _,source = wp_case
    blob,_,sections = read_native(source)
    report = validate_wp_topology(source)
    assert report['status']=='PASS' and report['shared_arcs']==1 and report['regions']==4
    # An arc may not refer to a nonexistent region, even if a permissive reader can reconstruct others.
    changed = bytearray(blob)
    struct.pack_into('<i',changed,sections[3][0]+24+8,999)
    invalid = source.with_name('dangling.wp'); invalid.write_bytes(changed)
    with pytest.raises(BridgeError,match='TOPOLOGY'):
        validate_wp_topology(invalid)
    # Leave ownership intact but sever a native ring's first edge.
    changed = bytearray(blob)
    point_offset = struct.unpack_from('<i',changed,sections[0][0]+57+14)[0]
    struct.pack_into('<d',changed,sections[1][0]+point_offset,12345)
    invalid.write_bytes(changed)
    with pytest.raises(BridgeError,match='TOPOLOGY'):
        validate_wp_topology(invalid)


def test_topology_error_retains_specific_wrong_type_diagnostic(tmp_path):
    import geopandas as gpd
    from pymapgis import Writer
    from shapely.geometry import Point
    from mapgis_agent.topology import validate_wp_topology
    source=tmp_path/'point.wt';Writer(gpd.GeoDataFrame({'ID':[1]},geometry=[Point(1,2)])).to_file(source)
    with pytest.raises(BridgeError,match='TOPOLOGY: WP required'):
        validate_wp_topology(source)


@pytest.mark.parametrize('workspace',[False,True])
@pytest.mark.parametrize('kind',['wp','wt','wl'])
def test_delete_does_not_recalculate_defaults_over_other_regions_string_fields(tmp_path,workspace,kind):
    from mapgis_agent.workspace import NativeWorkspaces
    from shapely.geometry import Point,LineString
    bridge=Bridge(tmp_path);enable_native(bridge)
    fields=[]
    for i in range(1,9):
        geometry=box(10*i,0,10*i+10,10) if kind=='wp' else Point(10*i,0) if kind=='wt' else LineString([(10*i,0),(10*i+10,10)])
        fields.append({'type':'Feature','id':str(i),'geometry':mapping(geometry),
            'properties':{'ID':i,'CLASS':'hdn5_2','QA':'linework_rebuilt','REGION':i,'SOURCE':'6-1.JPG',
                          '周长':40.+i*.125,'地层编号':i+10,'面积':100.+i*.25}})
    prepare_creation(bridge,{'type':'FeatureCollection','features':fields},{'name':'defaults.'+kind,'map_scale':1000,'crs':LOCAL},'create')
    source=Path(bridge.apply('create')['output']);before=source.read_bytes()
    if workspace:
        work=NativeWorkspaces(bridge)
        try:
            work.execute('open','buffer',{'source':str(source),'expected_sha256':sha(before)})
            result=work.execute('edit','buffer',{'operation':'feature.delete','parameters':{'record_ids':[1]}},0,'delete')
            output=Path(result['checkpoint'])
        finally:work.close_all()
    else:
        prepare_native_operation(bridge,'feature.delete',str(source),sha(before),{'record_ids':[1]},'delete')
        output=Path(bridge.apply('delete')['output'])
    old,new=read_native(source)[1],read_native(output)[1]
    assert native_ids(new)==list(range(2,9)) and new.data.to_dict('records')==old.data.iloc[1:].to_dict('records')
    from mapgis_agent.native_com import _table
    a,ar,sa=read_native(source);b,br,sb=read_native(output)
    _,_,start,count,width=_table(a,ar,sa);_,_,values,number,length=_table(b,br,sb)
    assert (count,width)==(number,length) and b[values+width]==0
    for identity in range(count):
        original=a[start+identity*width:start+(identity+1)*width];actual=b[values+identity*width:values+(identity+1)*width]
        assert actual==(b'\0'+original[1:] if identity==1 else original)
    assert source.read_bytes()==before
