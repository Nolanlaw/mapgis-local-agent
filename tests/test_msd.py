"""Public MSD operations bind dependencies and publish portable native libraries."""
import json,shutil
from pathlib import Path
import pytest
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from mapgis_agent.native_msd import msd_source_info,native_msd_info,prepare_native_msd,prepare_msd_edit
from test_native_msd import images


def source(path):
    return {'file':str(path),'expected_sha256':file_sha(path),
        'expected_witness_sha256':file_sha(path.with_name(path.name+'.json'))}


def test_public_msd_create_query_edit_relocation_and_source_guard(tmp_path):
    b,exe,bas,paths=images(tmp_path);inputs=[source(p) for p in paths]
    receipt=prepare_native_msd(b,inputs,{'name':'images.msd','coordinate_frame':'native'},'collection')
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    original=Path(b.apply('collection')['output']);info=msd_source_info(str(original))
    before={p:Path(p).read_bytes() for p in [str(original),*info['members']]}
    inspected=native_msd_info(b.root,str(original),file_sha(original),info['members'],[166,184,170,200])
    assert inspected['native_bounds']==pytest.approx([100,184,166,200])
    assert inspected['query']['member_indexes']==[2]  # boundary-inclusive envelope intersection
    assert [v['index'] for v in inspected['images']]==[1,2]
    edit=prepare_msd_edit(b,str(original),file_sha(original),info['members'],[1],[],
        {'name':'edited.msd','coordinate_frame':'native'},'delete-first')
    assert edit['source_index_to_output_index']=={'1':None,'2':1}
    edited=Path(b.apply('delete-first')['output']);check=msd_source_info(str(edited))
    assert native_msd_info(b.root,str(edited),file_sha(edited),check['members'])['native_bounds']==pytest.approx([150,184,166,200])
    assert all(Path(p).read_bytes()==data for p,data in before.items())
    moved=tmp_path/'relocated';shutil.copytree(original.parent,moved)
    moved_file=moved/original.name;relocated=msd_source_info(str(moved_file))
    assert all(Path(p).parent==moved for p in relocated['members'])
    assert native_msd_info(b.root,str(moved_file),file_sha(moved_file),relocated['members'])['member_count']==2
    witness=paths[0].with_name(paths[0].name+'.json');witness.write_text(witness.read_text()+' ')
    with pytest.raises(BridgeError,match='STALE_MSI_WITNESS'):
        prepare_native_msd(b,inputs,{'name':'stale.msd','coordinate_frame':'native'},'stale')


def test_empty_msd_add_delete_all_and_member_change_blocks_publication(tmp_path):
    b,exe,bas,paths=images(tmp_path)
    prepare_native_msd(b,[],{'name':'empty.msd','coordinate_frame':'native'},'empty')
    empty=Path(b.apply('empty')['output']);info=msd_source_info(str(empty))
    assert native_msd_info(b.root,str(empty),file_sha(empty),info['members'])['native_bounds'] is None
    prepare_msd_edit(b,str(empty),file_sha(empty),info['members'],[],[source(paths[0])],
        {'name':'added.msd','coordinate_frame':'native'},'add')
    added=Path(b.apply('add')['output']);info=msd_source_info(str(added))
    prepare_msd_edit(b,str(added),file_sha(added),info['members'],[1],[],
        {'name':'removed.msd','coordinate_frame':'native'},'remove')
    removed=Path(b.apply('remove')['output']);check=msd_source_info(str(removed))
    assert len(check['images'])==0 and set(check['members'])=={str(removed.with_name(removed.name+'.json'))}
    prepare_native_msd(b,[source(paths[1])],{'name':'guard.msd','coordinate_frame':'native'},'guard')
    paths[1].write_bytes(paths[1].read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('guard')


def test_msd_rejects_unknown_layout_and_known_frame_mismatch(tmp_path):
    file=tmp_path/'wrong.msd';file.write_text('MsiDataBase6.0;\n2;2;\n文件名;\na.msi;\nb.msi;\n',encoding='gbk')
    with pytest.raises(BridgeError,match='MSD_FORMAT'):msd_source_info(str(file))
    b,exe,bas,paths=images(tmp_path);witness=paths[1].with_name(paths[1].name+'.json')
    data=json.loads(witness.read_text());data['raster']['crs_wkt']='EPSG:32651';witness.write_text(json.dumps(data))
    with pytest.raises(BridgeError,match='MSD_COORDINATE_FRAME'):
        prepare_native_msd(b,[source(p) for p in paths],{'name':'wrong.msd','coordinate_frame':'native'},'wrong')
