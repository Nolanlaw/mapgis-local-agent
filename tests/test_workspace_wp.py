from pathlib import Path

from mapgis_agent.core import read_native,native_ids,sha
from mapgis_agent.workspace import NativeWorkspaces
from mapgis_agent.topology import validate_wp_topology
from test_native_wp import wp_case,enable_native


def test_native_wp_buffer_dirty_save_loss_recover_and_discard_are_connected(wp_case):
    bridge,source=wp_case;enable_native(bridge);registry=NativeWorkspaces(bridge);original=source.read_bytes()
    try:
        opened=registry.execute('open','wp',{'source':str(source),'expected_sha256':sha(original)})
        edited=registry.execute('edit','wp',{'operation':'feature.delete','parameters':{'record_ids':[1,3]}},0,'delete-shared')
        assert edited['dirty'] and edited['active']==2 and edited['native_pid']==opened['native_pid']
        assert validate_wp_topology(Path(edited['checkpoint']))['status']=='PASS'
        saved=registry.execute('save','wp',{},1,'save')
        assert not saved['dirty'] and native_ids(read_native(Path(saved['output']))[1])==[2,4]
        registry.execute('edit','wp',{'operation':'feature.delete','parameters':{'record_ids':[4]}},1,'delete-island')
        process=registry.buffers['wp'].process;process.kill();process.wait(timeout=5)
        assert registry.execute('status','wp',{})['state']=='LOST'
        recovered=registry.execute('recover','wp',{},2)
        assert recovered['dirty'] and recovered['active']==1 and recovered['native_pid']!=opened['native_pid']
        assert native_ids(read_native(Path(recovered['checkpoint']))[1])==[2]
        closed=registry.execute('close','wp',{'discard':True},2)
        assert not closed['dirty'] and closed['active']==2 and closed['state']=='CLOSED'
        assert source.read_bytes()==original
    finally:registry.close_all()
