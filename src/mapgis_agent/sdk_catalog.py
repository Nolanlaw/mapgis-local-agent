"""Read the exact installed MapGIS6.7 Automation contract without running it."""
from __future__ import annotations

import json
from pathlib import Path
import struct

from .core import BridgeError
from .raster import file_sha

PROFILE_PATH = Path(__file__).with_name('data') / 'com67-profile.json'

VT_NAMES = {0:'empty',1:'null',2:'int16',3:'int32',4:'float32',5:'float64',6:'currency',7:'date',
    8:'bstr',9:'dispatch',10:'error',11:'boolean',12:'variant',13:'unknown',14:'decimal',
    16:'int8',17:'uint8',18:'uint16',19:'uint32',20:'int64',21:'uint64',22:'int',23:'uint',
    24:'void',25:'hresult',26:'pointer',27:'safearray',28:'carray',29:'userdefined',30:'lpstr',31:'lpwstr'}


def sdk_profile(root):
    config_path=Path(root)/'config.local.json'
    config=json.loads(config_path.read_text(encoding='utf8')) if config_path.is_file() else {}
    if not config.get('mapgis_installation'):
        raise BridgeError('SDK_INSTALLATION_NOT_CONFIGURED')
    program=Path(config['mapgis_installation']).resolve()/'program'
    profile=json.loads(PROFILE_PATH.read_text(encoding='utf8'))
    return program,profile


def _type(ti,value):
    if isinstance(value,int):return {'vt':value,'name':VT_NAMES.get(value,'other')}
    if isinstance(value,(list,tuple)) and value:
        vt=value[0];result={'vt':vt,'name':VT_NAMES.get(vt,'other')}
        if vt in (26,27) and len(value)>1:result['element']=_type(ti,value[1])
        elif vt==29 and len(value)>1:
            try:
                ref=ti.GetRefTypeInfo(value[1]);attr=ref.GetTypeAttr()
                result.update(declared_name=ref.GetDocumentation(-1)[0],guid=str(attr.iid),typekind=attr.typekind)
            except Exception:result['reference_status']='UNRESOLVED_METADATA_REFERENCE'
        else:result['raw']=repr(value)
        return result
    return {'raw':repr(value)}


def _element(ti,description):
    result={'type':_type(ti,description[0]),'flags':description[1]}
    if len(description)>2 and description[2] is not None:
        value=description[2]
        result['default']=value if type(value) in (str,int,float,bool) else {'repr':repr(value)}
    return result


def _function(ti,index):
    fn=ti.GetFuncDesc(index);doc=ti.GetDocumentation(fn.memid);names=list(ti.GetNames(fn.memid))
    return {'member_id':fn.memid,'name':doc[0],'documentation':doc[1],'invoke_kind':fn.invkind,
        'function_kind':fn.funckind,'optional_parameters':fn.cParamsOpt,
        'return':_element(ti,fn.rettype),'parameters':[
            {'name':names[i+1] if i+1<len(names) else f'arg{i}',**_element(ti,arg)} for i,arg in enumerate(fn.args)],
        'flags':fn.wFuncFlags}


def sdk_describe(root,library=None,class_name=None,interface=None,member=None,offset=0,limit=100):
    """Original typelib names, directions, enums and signatures; not execution.

    Full-path LoadTypeLib does not register a typelib. The 64-bit metadata
    reader is not used as a 32-bit vtable address map; calls use IDispatch.
    """
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=250:
        raise BridgeError('SDK_PAGE: offset>=0, limit1..250')
    program,profile=sdk_profile(root)
    available=[{'library':p['name'],'sha256':p['sha256'],'classes':len(p['classes']),
                'typelib_guid':p['typelib'],'typelib_version':p['version']} for p in profile['files']]
    base={'scope':'Exact installed MapGIS6.7 Automation metadata; source contract, not an executed method.',
        'execution_verified':False,'metadata_host_bits':struct.calcsize('P')*8,'libraries':available,
        'library_hash_status':'Expected exact profile; requesting a library verifies its current binary before and after metadata reading.'}
    if library is None:
        if any(v is not None for v in (class_name,interface,member)):raise BridgeError('SDK_LIBRARY_REQUIRED')
        return base
    entry=next((p for p in profile['files'] if p['name'].casefold()==library.casefold()),None)
    if entry is None:raise BridgeError('SDK_LIBRARY_NOT_IN_EXACT_PROFILE')
    source=program/entry['name']
    if not source.is_file() or file_sha(source)!=entry['sha256']:raise BridgeError('SDK_BUILD_MISMATCH: '+entry['name'])
    import pythoncom
    lib=pythoncom.LoadTypeLib(str(source.resolve()))
    lib_attr=lib.GetLibAttr()
    if str(lib_attr[0]).casefold()!=entry['typelib'].casefold() or f'{lib_attr[3]}.{lib_attr[4]}'!=entry['version']:
        raise BridgeError('SDK_TYPELIB_BUILD_MISMATCH: '+entry['name'])
    infos={};types=[]
    for i in range(lib.GetTypeInfoCount()):
        ti=lib.GetTypeInfo(i);attr=ti.GetTypeAttr();name=ti.GetDocumentation(-1)[0]
        infos[name]=ti;types.append({'name':name,'kind':attr.typekind,'guid':str(attr.iid),'methods':attr.cFuncs,'variables':attr.cVars})
    if {t['name']:t['guid'].casefold() for t in types if t['kind']==pythoncom.TKIND_COCLASS} != {
            cls['name']:cls['clsid'].casefold() for cls in entry['classes']}:
        raise BridgeError('SDK_TYPELIB_CLASS_MISMATCH: '+entry['name'])
    result=base|{'library':entry['name'],'source':str(source),'source_sha256':entry['sha256'],
                 'typelib_guid':entry['typelib'],'typelib_version':entry['version']}
    if class_name is None and interface is None:
        selected=types[offset:offset+limit]
        result.update(types=selected,total=len(types),next_offset=offset+len(selected) if offset+len(selected)<len(types) else None)
    else:
        if class_name is not None:
            if interface is not None:raise BridgeError('SDK_CLASS_OR_INTERFACE')
            selected=infos.get(class_name)
            if selected is None or selected.GetTypeAttr().typekind!=5:raise BridgeError('SDK_CLASS_NOT_FOUND')
            implementations=[]
            for i in range(selected.GetTypeAttr().cImplTypes):
                ref=selected.GetRefTypeInfo(selected.GetRefTypeOfImplType(i));flags=selected.GetImplTypeFlags(i)
                implementations.append({'name':ref.GetDocumentation(-1)[0],'guid':str(ref.GetTypeAttr().iid),'flags':flags})
            defaults=[p for p in implementations if p['flags']&1 and not p['flags']&2]
            result.update(class_name=class_name,clsid=str(selected.GetTypeAttr().iid),interfaces=implementations)
            if len(defaults)!=1:raise BridgeError('SDK_DEFAULT_INTERFACE_AMBIGUOUS')
            interface=defaults[0]['name']
        ti=infos.get(interface)
        if ti is None:raise BridgeError('SDK_INTERFACE_NOT_FOUND')
        attr=ti.GetTypeAttr();result.update(interface=interface,interface_guid=str(attr.iid))
        functions=[_function(ti,i) for i in range(attr.cFuncs)]
        functions=[f for f in functions if f['name'] not in {'QueryInterface','AddRef','Release','GetTypeInfoCount','GetTypeInfo','GetIDsOfNames','Invoke'}]
        if member is not None:functions=[f for f in functions if f['name']==member]
        constants=[]
        for i in range(attr.cVars):
            var=ti.GetVarDesc(i);doc=ti.GetDocumentation(var.memid)
            constants.append({'name':doc[0],'documentation':doc[1],'value':var.value,'type':_type(ti,var.elemdescVar[0])})
        result.update(methods=functions[offset:offset+limit],total=len(functions),constants=constants,
            next_offset=offset+limit if offset+limit<len(functions) else None)
    if file_sha(source)!=entry['sha256']:raise BridgeError('SDK_SOURCE_CHANGED')
    return result
