"""Real SDK MPJ metadata must persist without altering layer data or layout."""
from pathlib import Path
import pytest
from mapgis_agent.core import BridgeError
from mapgis_agent.native_project_parameters import native_project_parameters_info,prepare_project_parameters,parameter_changes
from mapgis_agent.native_project import native_project_info
from mapgis_agent.raster import file_sha
from test_native_project_edit import project_case


def test_native_project_all_29_parameter_fields_save_relocate_and_reopen_without_layer_edits(tmp_path):
    b,source,_=project_case(tmp_path);digest=file_sha(source);original=native_project_info(tmp_path,str(source),digest)
    source_hashes={p:file_sha(p) for p in source.parent.iterdir() if p.is_file()}
    before=native_project_parameters_info(tmp_path,str(source),digest)
    changes={'type':3,'unit':2,'mapw':700.,'maph':500.,'projType':5,'earthParam':7,
        'jd0':0.,'wd0':0.,'djd':0.,'dwd':0.,'rate':50000.,'lon':1203000.,'lon1':1190000.,'lon2':1210000.,
        'lat':300000.,'lat1':250000.,'lat2':350000.,'dx':500000.,'dy':1000000.,'levelType':0,'h':12.5,'H2':-4.5,
        'vRate':10000.,'vUnit':2,'TICtype':1,'TICunit':5,'infoUnit':0,'infoRatex':1.,'infoRatey':1.}
    assert set(changes)==set(before['parameters']) and len(changes)==29
    prepared=prepare_project_parameters(b,str(source),digest,changes,{'name':'parameters.mpj'},'parameters')
    result=b.apply('parameters');after=native_project_parameters_info(tmp_path,result['output'],file_sha(Path(result['output'])))
    assert after['parameters']==changes==prepared['parameters']
    reopened=native_project_info(tmp_path,result['output'],file_sha(Path(result['output'])))
    assert reopened['environment']==original['environment']
    assert [(v['sha256'],v['state'],v['display_limits']) for v in reopened['layers']]==[(v['sha256'],v['state'],v['display_limits']) for v in original['layers']]
    assert all(file_sha(p)==value for p,value in source_hashes.items())
    assert prepared['native_execution']['pid']!=prepared['native_parameter_reopen']['pid']
    assert prepare_project_parameters(b,str(source),digest,changes,{'name':'parameters.mpj'},'parameters')['state']=='APPLIED'
    assert b.status('parameters')['output_matches']
    from mapgis_agent.native_project import prepare_project_edit
    prepare_project_edit(b,result['output'],file_sha(Path(result['output'])),[{'source_index':i} for i in range(1,4)],'later-native-edit')
    later=b.apply('later-native-edit')
    assert native_project_parameters_info(tmp_path,later['output'],file_sha(Path(later['output'])))['parameters']==changes


@pytest.mark.parametrize('changes',[{'type':True},{'unit':35},{'earthParam':90},{'lon':float('nan')},
    {'rate':-1},{'mapw':-1},{'infoRatex':.5},{'unknown':1},{'projType':1.0}])
def test_native_project_parameter_boundary_rejects_invalid_numbers_enums_and_fields(changes):
    with pytest.raises(BridgeError,match='PROJECT_MAP_PARAMETER'):parameter_changes(changes)


def test_project_parameter_apply_rejects_changed_source_layer(tmp_path):
    b,source,_=project_case(tmp_path)
    prepare_project_parameters(b,str(source),file_sha(source),{'mapw':123.}, {'name':'new.mpj'},'stale-layer')
    info=native_project_info(tmp_path,str(source),file_sha(source));layer=Path(info['layers'][0]['file']);layer.write_bytes(layer.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('stale-layer')
