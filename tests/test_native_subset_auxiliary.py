"""WP selected copies must retain physical arc/node attributes after native save."""
import io,struct
from pathlib import Path
import pandas as pd
import pytest
from pymapgis import reader as native,writer
from mapgis_agent.core import BridgeError,read_native
from mapgis_agent.creation import _replace_sections
from mapgis_agent.native_com import prepare_native_subset,_check_subset
from mapgis_agent.raster import file_sha
from mapgis_agent.topology import _wp_arc_records,_wp_nodes
from test_native_wp import wp_case,enable_native


def populated_auxiliary(source):
    blob,_,sections,arcs=_wp_arc_records(source);nodes=_wp_nodes(arcs)
    replacement={}
    for index,identities,kind in [(2,arcs,'弧'),(5,nodes,'结点')]:
        frame=pd.DataFrame({'ID':[7]*max(identities),'备注':[kind+str(i) for i in range(1,max(identities)+1)],
                            '数值':[(-1.)**i*(i+.125) for i in range(1,max(identities)+1)]})
        stamp=struct.unpack_from('<i',blob,sections[index][0]+12)[0]
        replacement[index]=writer._build_attribute_section(frame,writer._fields_from_dataframe(frame),stamp)
    source.write_bytes(_replace_sections(blob,sections,replacement))


def table_records(file,index):
    blob,_,sections=read_native(Path(file));start,size=sections[index];stream=io.BytesIO(blob)
    count,width,fields=native._read_attribute_header(stream,start);data=stream.tell()
    return blob[start:data],{i:blob[data+i*width:data+(i+1)*width] for i in range(count)},data,width


@pytest.mark.parametrize('selected',[[2,1,3,4],[2,3,4],[3]])
@pytest.mark.parametrize('transformed',[False,True])
def test_native_wp_copy_preserves_arc_node_raw_records_shared_boundary_hole_and_island(wp_case,selected,transformed):
    bridge,source=wp_case;enable_native(bridge);populated_auxiliary(source);before=source.read_bytes()
    transform={'translation':[100.,-20.],'scale':[-2.,.75],'rotation_degrees':31.,
               'order':['rotate','translate','scale'],'coordinate_frame':'source'} if transformed else None
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),selected,{'name':'selected.wp'},'selected',transform=transform)
    target=Path(bridge.apply('selected')['output']);verification=receipt['verification']
    aux=verification['polygon_subordinate_records'];assert aux['attribute_records_byte_identical']
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    _,_,_,arcs=_wp_arc_records(target);nodes=_wp_nodes(arcs)
    for index,key,actual in [(2,'arc_mapping',arcs),(5,'node_mapping',nodes)]:
        header,old,_,_=table_records(source,index);other,new,_,_=table_records(target,index)
        assert header[:324]==other[:324] and header[328:]==other[328:]
        pairs=aux[key];assert {v['output_record_id'] for v in pairs}==set(actual)
        assert len({v['source_record_id'] for v in pairs})==len(pairs)
        assert all(old[v['source_record_id']]==new[v['output_record_id']] for v in pairs)
    if 1 in selected and 2 in selected:assert any(len(a['owner_region_ids'])==2 for a in arcs.values())
    assert source.read_bytes()==before
    # An intact region table cannot hide corrupted subordinate attributes.
    _,_,offset,width=table_records(target,2);changed=bytearray(target.read_bytes());changed[offset+width+1]^=1
    corrupt=target.with_name('wrong-arc-attribute.wp');corrupt.write_bytes(changed)
    with pytest.raises(BridgeError,match='SUBSET_AUXILIARY_ATTRIBUTE_BYTES_CHANGED'):
        _check_subset(source,corrupt,verification['mapping'],transform)


def test_unknown_nonempty_attribute_section_is_still_rejected_before_native_launch(wp_case):
    bridge,source=wp_case;blob,_,sections=read_native(source);populated_auxiliary(source)
    blob,_,sections=read_native(source);offset,size=sections[2]
    source.write_bytes(_replace_sections(blob,sections,{15:blob[offset:offset+size]}))
    with pytest.raises(BridgeError,match='SUBSET_AUXILIARY_ATTRIBUTES'):
        prepare_native_subset(bridge,str(source),file_sha(source),[1],{'name':'out.wp'},'unknown')
    assert not any((bridge.root/'work/artifacts').glob('*/native/native-com.started.json'))
