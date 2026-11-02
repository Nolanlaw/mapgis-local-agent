from pathlib import Path
import json
import shutil

import cv2
import numpy as np
import pytest
from shapely.geometry import Point,LineString,box,mapping

from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.symbol_library import system_library_info,prepare_system_library
from mapgis_agent.native_plot import prepare_native_plot
from test_native_raster import native_settings


X=[{'type':'polyline','parts':[[[-.4,-.4],[.4,.4]]],'color':1,'width_index':1},
   {'type':'polyline','parts':[[[-.4,.4],[.4,-.4]]],'color':1,'width_index':1}]
PATTERN=[{'type':'polyline','parts':[[[0,0],[1,1]]],'color':1,'width_index':1},
         {'type':'polyline','parts':[[[0,1],[1,0]]],'color':1,'width_index':1}]


def library_case(tmp_path):
    bridge=Bridge(tmp_path);_,bas=native_settings(tmp_path)
    original=bas.parent.parent/'Slib'
    assert original.is_dir()
    info=system_library_info(str(original))
    return bridge,original,info


def test_new_process_colors_preserve_old_records_header_fields_and_reserved_trailer(tmp_path):
    import struct
    bridge,source,info=library_case(tmp_path);old=(source/'Pcolor.lib').read_bytes();count=info['color_count'];end=124+32*count
    changes=[{'kind':'color','action':'append','kcmy':[0,0,255,255]},
             {'kind':'color','action':'append','kcmy':[0,255,0,255]}]
    prepared=prepare_system_library(bridge,str(source),info['manifest_sha256'],changes,'new-colors')
    output=bridge.apply('new-colors');folder=Path(output['output']).parent;updated=(folder/'Pcolor.lib').read_bytes()
    assert prepared['color_count']==count+2 and [v['id'] for v in prepared['changes']]==[count,count+1]
    assert struct.unpack_from('<H',updated,8)[0]==count+2
    assert updated[:8]==old[:8] and updated[10:124]==old[10:124] and updated[124:end]==old[124:end]
    assert updated[end+64:]==old[end:]
    assert updated[end:end+32]==bytes(changes[0]['kcmy'])+bytes(28)
    assert updated[end+32:end+64]==bytes(changes[1]['kcmy'])+bytes(28)
    read=system_library_info(str(folder),kind='color',ids=[count,count+1])
    assert [v['kcmy'] for v in read['records']]==[v['kcmy'] for v in changes]
    assert (source/'Pcolor.lib').read_bytes()==old
    hashes={f.name:(sha(f.read_bytes()),f.stat().st_mtime_ns) for f in folder.iterdir()}
    assert prepare_system_library(bridge,str(source),info['manifest_sha256'],changes,'new-colors')['state']=='APPLIED'
    assert all((sha((folder/name).read_bytes()),(folder/name).stat().st_mtime_ns)==record for name,record in hashes.items())


def test_native_save_reopen_and_plot_resolve_a_new_color_number_like_an_existing_control(tmp_path):
    from mapgis_agent.native_backend import prepare_native_operation
    from mapgis_agent.core import read_native
    bridge,source,info=library_case(tmp_path);number=info['color_count'];color=[0,0,255,255]
    prepared=prepare_system_library(bridge,str(source),info['manifest_sha256'],[
        {'kind':'color','action':'append','kcmy':color},{'kind':'color','action':'replace','id':101,'kcmy':color}],'palette')
    saved=bridge.apply('palette');binding={'folder':str(Path(saved['output']).parent),'expected_manifest_sha256':prepared['manifest_sha256']}
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(box(i*10,0,i*10+10,10)),
        'properties':{'ID':i},'style':{'fill_color':n,'pattern':0}} for i,n in enumerate([101,number])]}
    prepare_creation(bridge,features,{'name':'new-color.wp','map_scale':1000,
        'crs':{'kind':'local','name':'new process colors','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}},'regions')
    regions=bridge.apply('regions');prepare_native_operation(bridge,'resave',regions['output'],regions['candidate_sha256'],{},'native-save')
    output=bridge.apply('native-save');_,reader,_=read_native(Path(output['output']))
    assert list(reader.polygon_params.fill_color)==[101,number]
    plotted=prepare_native_plot(bridge,[{'file':output['output'],'expected_sha256':output['candidate_sha256']}],
        {'native_bounds':[0,0,20,10],'paper_mm':[200,100],'coordinate_frame':'native'},'native-new-color',system_library=binding)
    image=cv2.imdecode(np.frombuffer(Path(plotted['render_check']['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR);h,w=image.shape[:2]
    left=image[h//4:3*h//4,w//8:3*w//8];right=image[h//4:3*h//4,5*w//8:7*w//8]
    assert np.array_equal(left,right) and np.all(left[:,:,2]>200) and np.all(left[:,:,:2]<80)


@pytest.mark.parametrize('change',[{'kind':'color','action':'append','id':2,'kcmy':[0,0,0,0]},
    {'kind':'color','action':'append','kcmy':[0,0,0,True]}, {'kind':'color','action':'append','kcmy':[0,0,0,256]},
    {'kind':'color','action':'append','kcmy':[0,0,0]}])
def test_process_color_append_has_no_manual_id_or_unknown_components(tmp_path,change):
    bridge,source,info=library_case(tmp_path)
    with pytest.raises(BridgeError,match='LIBRARY_COLOR_'):prepare_system_library(bridge,str(source),info['manifest_sha256'],[change],'invalid-color')


def test_process_color_count_cannot_exceed_original_native_reader_limit(tmp_path):
    import struct
    bridge,source,info=library_case(tmp_path);full=tmp_path/'full-library';full.mkdir()
    for member in info['members']:shutil.copyfile(source/member['name'],full/member['name'])
    file=full/'Pcolor.lib';header=bytearray(file.read_bytes()[:124]);struct.pack_into('<H',header,8,0x4000)
    file.write_bytes(header+bytes(32*0x4000)+b'preserved opaque trailer')
    current=system_library_info(str(full));before=file.read_bytes()
    with pytest.raises(BridgeError,match='LIBRARY_COLOR_FULL'):
        prepare_system_library(bridge,str(full),current['manifest_sha256'],[{'kind':'color','action':'append','kcmy':[0,0,0,0]}],'full')
    assert file.read_bytes()==before


def test_library_append_preserves_existing_records_and_native_draws_custom_symbols_lines_patterns(tmp_path):
    bridge,source,info=library_case(tmp_path)
    frozen={p:p.read_bytes() for p in source.iterdir() if p.is_file()}
    changes=[{'kind':'symbol','action':'append','primitives':X},
             {'kind':'line','action':'append','dash_fraction':.5},
             {'kind':'pattern','action':'append','primitives':PATTERN}]
    prepared=prepare_system_library(bridge,str(source),info['manifest_sha256'],changes,'library')
    saved=bridge.apply('library');folder=Path(saved['output']).parent
    updated=system_library_info(str(folder),prepared['manifest_sha256'])
    assert prepared['existing_payloads_preserved']
    assert [c['id'] for c in prepared['changes']]==[info['counts'][c['kind']]+1 for c in changes]
    assert updated['counts']=={kind:n+1 for kind,n in info['counts'].items()}
    assert all(p.read_bytes()==raw for p,raw in frozen.items())
    assert bridge.status('library')['output_matches']
    # Recovery uses the persisted artifact set rather than replaying appends.
    stage=Path(saved['stage']);stage.rename(stage.with_name(stage.name+'-hidden'))
    assert bridge.apply('library')['reconciled_existing_output']
    binding={'folder':str(folder),'expected_manifest_sha256':updated['manifest_sha256']}
    local={'kind':'local','name':'symbol test','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}
    cases=[('point',Point(10,10),'wt',{'point_type':1,'symbol_no':prepared['changes'][0]['id'],'height':40,'width':40,'color':1}),
           ('line',LineString([(0,10),(20,10)]),'wl',{'linetype':prepared['changes'][1]['id'],'color':1,'width':.2}),
           ('pattern',box(0,0,20,20),'wp',{'fill_color':2,'pattern':prepared['changes'][2]['id'],
                                       'pattern_color':6,'pattern_height':5,'pattern_width':5})]
    for tag,geometry,suffix,style in cases:
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1',
            'geometry':mapping(geometry),'properties':{'ID':1},'style':style}]},
            {'name':tag+'.'+suffix,'crs':local,'map_scale':1000},tag)
        layer=bridge.apply(tag)
        result=prepare_native_plot(bridge,[{'file':layer['output'],'expected_sha256':layer['candidate_sha256']}],
            {'native_bounds':[0,0,20,20],'paper_mm':[100,100],'coordinate_frame':'native'},'plot-'+tag,system_library=binding)
        plotted=bridge.apply('plot-'+tag)
        assert result['system_library']['manifest_sha256']==updated['manifest_sha256']
        assert result['render_check']['ink_pixels']>200
        assert bridge.status('plot-'+tag)['output_matches']
        pixels=cv2.imdecode(np.frombuffer(Path(result['render_check']['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        if tag=='pattern':
            colors,counts=np.unique(pixels.reshape(-1,3),axis=0,return_counts=True)
            assert sorted(counts,reverse=True)[:2][1]>200
            assert (Path(plotted['output']).read_bytes().find(b'/PatternType 1'))>=0
            assert result['normalization']['native_page_mm']==[100,100]
        if tag=='line':
            # Four separated dash groups, not one solid segment.
            ink=np.any(pixels<245,axis=2).any(axis=0)
            assert np.count_nonzero(np.diff(np.r_[False,ink,False].astype('int8'))==1)==4
    assert all(p.read_bytes()==raw for p,raw in frozen.items())


def test_library_rejects_stale_and_malformed_edits_and_keeps_native_record_ids(tmp_path):
    bridge,source,info=library_case(tmp_path)
    with pytest.raises(BridgeError,match='STALE_LIBRARY'):
        prepare_system_library(bridge,str(source),'0'*64,[{'kind':'symbol','action':'append','primitives':X}],'stale')
    for index,change in enumerate([
        {'kind':'symbol','action':'replace','id':0,'primitives':X},
        {'kind':'symbol','action':'append','primitives':[{'type':'polyline','parts':[[[0,0],[float('nan'),1]]]}]},
        {'kind':'line','action':'append','dash_fraction':0},
        {'kind':'pattern','action':'delete','id':1},
    ]):
        with pytest.raises(BridgeError,match='LIBRARY_'):
            prepare_system_library(bridge,str(source),info['manifest_sha256'],[change],'invalid-'+str(index))
    change={'kind':'symbol','action':'replace','id':1,'primitives':X}
    prepared=prepare_system_library(bridge,str(source),info['manifest_sha256'],[change],'replace')
    output=bridge.apply('replace');actual=system_library_info(str(Path(output['output']).parent),kind='symbol',ids=[1,2])
    assert actual['counts']==info['counts']
    assert actual['records'][0]['id']==1 and actual['records'][1]['id']==2
    assert np.allclose(actual['records'][0]['primitives'][0]['parts'],X[0]['parts'],rtol=0,atol=1e-7)
    assert prepared['existing_payloads_preserved']


def test_plot_rejects_truncated_native_postscript_and_wrong_native_page(tmp_path):
    from mapgis_agent.native_plot import normalize_native_ps,_ghostscript
    from test_native_raster import ROOT
    source=tmp_path/'map.ps';candidate=tmp_path/'map.eps'
    source.write_bytes(b'\r%%BoundingBox: 0 0 283.46 283.46\r<</PageSize [283.46 283.46]>>setpagedevice\r')
    with pytest.raises(BridgeError,match='POSTSCRIPT_INCOMPLETE'):
        normalize_native_ps(_ghostscript(ROOT),tmp_path,source,candidate,[100,100])
    source.write_bytes(source.read_bytes()+b'%EOF')
    with pytest.raises(BridgeError,match='POSTSCRIPT_PAGE'):
        normalize_native_ps(_ghostscript(ROOT),tmp_path,source,candidate,[200,100])


def test_color_branch_preserves_opaque_bytes_and_native_plot_uses_its_palette(tmp_path):
    bridge,source,info=library_case(tmp_path)
    before=(source/'Pcolor.lib').read_bytes()
    other_bytes={m['name']:(source/m['name']).read_bytes() for m in info['members'] if m['name'].casefold()!='pcolor.lib'}
    records=system_library_info(str(source),kind='color',ids=[0,101])['records']
    assert records[0]['kcmy']==[255,0,0,0]
    change={'kind':'color','action':'replace','id':101,'kcmy':[0,0,255,255]}
    prepared=prepare_system_library(bridge,str(source),info['manifest_sha256'],[change],'red')
    saved=bridge.apply('red');folder=Path(saved['output']).parent
    after=(folder/'Pcolor.lib').read_bytes();offset=124+32*101
    assert after[:offset]==before[:offset] and after[offset+4:]==before[offset+4:]
    assert after[offset:offset+4]==bytes([0,0,255,255])
    assert all((folder/name).read_bytes()==blob for name,blob in other_bytes.items())
    assert (source/'Pcolor.lib').read_bytes()==before
    binding={'folder':str(folder),'expected_manifest_sha256':prepared['manifest_sha256']}
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1',
        'geometry':mapping(box(2,2,18,18)),'properties':{'ID':1},'style':{'fill_color':101}}]},
        {'name':'red.wp','map_scale':1000,'crs':{'kind':'local','name':'palette test','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}},'area')
    layer=bridge.apply('area')
    plotted=prepare_native_plot(bridge,[{'file':layer['output'],'expected_sha256':layer['candidate_sha256']}],
        {'native_bounds':[0,0,20,20],'paper_mm':[100,100],'coordinate_frame':'native'},'red-plot',system_library=binding)
    pixels=cv2.imdecode(np.frombuffer(Path(plotted['render_check']['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    # Ghostscript's CMYK conversion renders process red near RGB(237,28,36),
    # not RGB(255,0,0). The old native palette renders this ID green.
    assert np.count_nonzero((pixels[:,:,2]>220)&(pixels[:,:,1]<50)&(pixels[:,:,0]<50))>1000
    assert (source/'Pcolor.lib').read_bytes()==before
    with pytest.raises(BridgeError,match='LIBRARY_COLOR_ID'):
        prepare_system_library(bridge,str(source),info['manifest_sha256'],[{**change,'id':0}],'black')
    # A recipient must not need the producer's original library path.
    from mapgis_agent.native_project import prepare_native_project,prepare_project_plot,prepare_project_edit,native_project_info
    prepare_native_project(bridge,[{'file':layer['output'],'expected_sha256':layer['candidate_sha256']}],
        {'name':'colors.mpj','coordinate_frame':'native','system_library':binding},'color-project')
    project=bridge.apply('color-project');moved=tmp_path/'work/recipient'
    shutil.copytree(Path(project['output']).parent,moved)
    file=moved/'colors.mpj';fingerprint=sha(file.read_bytes())
    opened=native_project_info(bridge.root,str(file),fingerprint)
    assert opened['system_library']['folder']==str(moved.resolve())
    edit=prepare_project_edit(bridge,str(file),fingerprint,[{'source_index':1}],'project-edit')
    edited=bridge.apply('project-edit')
    plotted=prepare_project_plot(bridge,edited['output'],edited['candidate_sha256'],
        {'native_bounds':[0,0,20,20],'paper_mm':[100,100],'coordinate_frame':'native'},'project-red')
    rendered=bridge.apply('project-red')
    pixels=cv2.imdecode(np.frombuffer(Path(rendered['output']).with_name('preview.png').read_bytes(),np.uint8),1)
    assert np.count_nonzero((pixels[:,:,2]>220)&(pixels[:,:,1]<50)&(pixels[:,:,0]<50))>1000
    assert plotted['system_library']['manifest_sha256']==binding['expected_manifest_sha256']
    (moved/'Pcolor.lib').write_bytes(before)
    with pytest.raises(BridgeError,match='STALE_LIBRARY'):
        native_project_info(bridge.root,str(file),fingerprint)
