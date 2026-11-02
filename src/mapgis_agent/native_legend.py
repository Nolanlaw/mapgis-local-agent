"""Native CLN catalog and editable legend layout, in isolated SDK processes."""
from __future__ import annotations
import json,math,os,re,shutil,uuid
from pathlib import Path
import psutil
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text,sha,read_native,native_ids,annotation_text
from .native_backend import backend_configuration,completed_native_child,native_file_info
from .native_com import private_com_runtime,invoke_com
from .raster import file_sha

KINDS={0:'line',1:'point',2:'region'}
DEFAULTS={
    'line':{'linetype':1,'aux_linetype':0,'color':1,'width':.2,'x_coef':1.,'y_coef':1.,'aux_color':0,'layer':1},
    'point':{'symbol_no':1,'height':5.,'width':5.,'angle':0.,'pen_width':.1,'color':1,'layer':1},
    'region':{'fill_color':3,'pattern':0,'pattern_height':5.,'pattern_width':5.,'pattern_color':1,'layer':1},
}
INTEGERS={'linetype','aux_linetype','color','aux_color','layer','symbol_no','fill_color','pattern','pattern_color'}


def _text(value,limit,empty=False):
    if not isinstance(value,str) or (not value and not empty) or any(ord(c)<32 for c in value):raise BridgeError('LEGEND_TEXT')
    try:encoded=value.encode('gbk',errors='strict')
    except UnicodeEncodeError as error:raise BridgeError('LEGEND_GBK_TEXT') from error
    if len(encoded)>limit:raise BridgeError('LEGEND_TEXT_LENGTH')
    return value


def legend_spec(items,parameters):
    if not isinstance(parameters,dict) or set(parameters)!={'name','title','bounds','columns'}:raise BridgeError('LEGEND_PARAMETERS: name/title/bounds/columns required')
    if not isinstance(parameters['name'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,55}\.cln',parameters['name']):raise BridgeError('LEGEND_OUTPUT_NAME')
    bounds=parameters['bounds'];columns=parameters['columns']
    if not isinstance(bounds,list) or len(bounds)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds) or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]:raise BridgeError('LEGEND_RECTANGLE')
    if type(columns) is not int or not 1<=columns<=20:raise BridgeError('LEGEND_COLUMNS')
    if not isinstance(items,list) or not 1<=len(items)<=1000:raise BridgeError('LEGEND_ITEMS: 1..1000 explicit items required')
    result=[]
    for item in items:
        if not isinstance(item,dict) or not {'kind','name','code'}<=set(item) or set(item)-{'kind','name','description','code','style'}:raise BridgeError('LEGEND_ITEM_FIELDS')
        kind=item['kind']
        if not isinstance(kind,str) or kind not in DEFAULTS:raise BridgeError('LEGEND_ITEM_KIND')
        if type(item['code']) is not int or not -(2**31)<=item['code']<2**31:raise BridgeError('LEGEND_CLASS_CODE')
        style=item.get('style',{})
        if not isinstance(style,dict) or set(style)-set(DEFAULTS[kind]):raise BridgeError('LEGEND_STYLE_FIELDS')
        style=DEFAULTS[kind]|style
        for key,value in style.items():
            if type(value) not in (int,float) or not math.isfinite(value):raise BridgeError('LEGEND_STYLE_NUMBER')
            if key in INTEGERS:
                limit=255 if key in {'layer','pattern_color','color'} and kind=='point' else 255 if key in {'layer','pattern_color'} else 32767 if key in {'linetype','aux_linetype','symbol_no','pattern'} else 65535
                if type(value) is not int or not 0<=value<=limit:raise BridgeError('LEGEND_STYLE_INTEGER: '+key)
            if key in {'height','width','x_coef','y_coef'} and not (key=='width' and kind=='line') and value<=0:raise BridgeError('LEGEND_STYLE_DIMENSION: '+key)
            if key in {'pattern_height','pattern_width'} and (value<0 or style['pattern']>0 and value==0):raise BridgeError('LEGEND_STYLE_DIMENSION: '+key)
            if (key=='pen_width' or key=='width' and kind=='line') and value<0:raise BridgeError('LEGEND_STYLE_DIMENSION: '+key)
        result.append({'kind':kind,'name':_text(item['name'],90),'description':_text(item.get('description',''),127,True),'code':item['code'],'style':style})
    return result,{'name':parameters['name'],'title':_text(parameters['title'],127),'bounds':list(bounds),'columns':columns}


def _items(events):
    rows=[v for v in events if v['event']=='legend_item'];counts=[v['count'] for v in events if v['event']=='legend_count']
    if counts!=[len(rows)] or [v['index'] for v in rows]!=list(range(len(rows))):raise BridgeError('LEGEND_NATIVE_RECEIPT')
    result=[]
    for row in rows:
        if row['type'] not in KINDS:raise BridgeError('LEGEND_NATIVE_ITEM_KIND')
        kind=KINDS[row['type']]
        if set(row['style'])!=set(DEFAULTS[kind]):raise BridgeError('LEGEND_NATIVE_STYLE_FIELDS')
        result.append({'kind':kind,'name':bytes.fromhex(row['name_gbk_hex']).decode('gbk'),'description':bytes.fromhex(row['description_gbk_hex']).decode('gbk'),'code':row['code'],'style':row['style']})
    return result


def _check_items(actual,expected):
    if len(actual)!=len(expected):raise BridgeError('LEGEND_ITEM_COUNT_DIFF')
    for a,b in zip(actual,expected):
        if any(a[k]!=b[k] for k in ('kind','name','description','code')):raise BridgeError('LEGEND_SAVED_ITEM_DIFF')
        for name,value in b['style'].items():
            if not math.isclose(a['style'][name],value,rel_tol=1e-6,abs_tol=1e-6):raise BridgeError('LEGEND_SAVED_STYLE_DIFF: '+name)


def _control(items,parameters):
    hextext=lambda v:v.encode('gbk').hex() or '-'
    lines=[f"{len(items)} "+' '.join(repr(v) for v in parameters['bounds'])+f" {parameters['columns']} {hextext(parameters['title'])}"]
    for item in items:
        kind=next(k for k,v in KINDS.items() if v==item['kind']);style=item['style']
        lines.append(f"{kind} {item['code']} {hextext(item['name'])} {hextext(item['description'])} {len(style)}")
        lines.extend(f'{key} {value!r}' for key,value in style.items())
    return '\n'.join(lines)+'\n'


def native_legend_info(root,file,expected_sha256):
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.suffix.lower()!='.cln' or not 12<=source.stat().st_size<=50*1024*1024:raise BridgeError('LEGEND_NATIVE_FILE')
    exe,bas=backend_configuration(Path(root));folder=Bridge(root).destination_path(Path(root)/'work'/('legend-info-'+uuid.uuid4().hex));folder.mkdir()
    native=folder/'native';native.mkdir();shutil.copyfile(source,native/'input.cln');private_com_runtime(bas.parent,folder/'sdk-runtime')
    if file_sha(native/'input.cln')!=expected_sha256:raise BridgeError('STALE_SOURCE')
    host,events=invoke_com(root,native,folder/'sdk-runtime','legend.inspect','input.cln','-','-','native-com.json')
    if file_sha(source)!=expected_sha256 or file_sha(native/'input.cln')!=expected_sha256:raise BridgeError('LEGEND_INSPECTION_CHANGED_INPUT')
    return {'file':str(source),'sha256':expected_sha256,'items':_items(events),'native_execution':host,'source_preserved':True,'mouse_or_keyboard_input':False,
        'scope':'Saved CLN item catalog; page placement is bound in the generated legend JSON and editable WT/WL/WP, not inferred from CLN alone'}


def prepare_native_legend(bridge,items,parameters,request_id):
    items,parameters=legend_spec(items,parameters);request_id=bridge.request_id(request_id)
    exe,bas=backend_configuration(bridge.root)
    payload={'operation':'native.legend','items':items,'parameters':parameters,'legend_protocol':1}
    encoded=json_text(payload).encode('utf8');intent=bridge.destination_path(bridge.root/'work/legend-intents'/(sha(request_id.encode())+'.json'));intent.parent.mkdir(parents=True,exist_ok=True)
    if intent.exists() and intent.read_bytes()!=encoded:raise BridgeError('REQUEST_ID_CONFLICT')
    if not intent.exists():
        with intent.open('xb') as stream:stream.write(encoded)
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(intent),'sha256':sha(encoded)}],parameters['name'])
    if not new:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        native=stage/'native';native.mkdir();private_com_runtime(bas.parent,stage/'sdk-runtime')
        (native/'legend.txt').write_text(_control(items,parameters),encoding='ascii')
        host,events=invoke_com(bridge.root,native,stage/'sdk-runtime','legend.create','-',parameters['name'],'legend.txt','native-com.json')
        return finish_native_legend(bridge,request_id,stage,payload,host,events)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def finish_native_legend(bridge,request_id,stage,payload,host,events,recovered=False):
    parameters=payload['parameters'];native=stage/'native';candidate=native/parameters['name'];items=_items(events);_check_items(items,payload['items'])
    saved=[v['sha256'] for v in events if v['event']=='legend_saved']
    if saved!=[file_sha(candidate)]:raise BridgeError('LEGEND_SAVE_RECEIPT_DIFF')
    files=bridge.destination_path(stage/'files')
    if files.exists():
        if not recovered:raise BridgeError('LEGEND_STAGE_EXISTS')
        files.rename(bridge.destination_path(stage/('incomplete-files-'+uuid.uuid4().hex[:8])))
    files.mkdir();target=files/candidate.name;shutil.copyfile(candidate,target)
    vectors={};texts=[]
    for suffix,kind in [('.wt','POINT'),('.wl','LINE'),('.wp','POLYGON')]:
        source=candidate.with_suffix(suffix)
        if not source.is_file():continue
        output=target.with_suffix(suffix);shutil.copyfile(source,output);blob,reader,sections=read_native(output)
        if reader.shapeType!=kind:raise BridgeError('LEGEND_VECTOR_TYPE_DIFF')
        vectors[kind]=native_file_info(bridge.root,str(output),file_sha(output))
        if vectors[kind]['active']!=len(native_ids(reader)):raise BridgeError('LEGEND_VECTOR_COUNT_DIFF')
        if kind=='POINT':texts=[annotation_text(blob,sections,i) for i in native_ids(reader)]
        if any(not math.isfinite(v) for geom in reader.geom for v in geom.bounds):raise BridgeError('LEGEND_VECTOR_FINITE')
    expected_texts=[parameters['title']]+[v['name'] for k in ('point','line','region') for v in items if v['kind']==k]
    if [v for v in texts if v]!=expected_texts:raise BridgeError('LEGEND_VECTOR_LABEL_DIFF')
    if vectors.get('POINT',{}).get('active')!=len(items)+1+sum(v['kind']=='point' for v in items):raise BridgeError('LEGEND_POINT_COUNT_DIFF')
    if vectors.get('POLYGON',{}).get('active',0)!=sum(v['kind']=='region' for v in items):raise BridgeError('LEGEND_REGION_COUNT_DIFF')
    if vectors.get('LINE',{}).get('active',0)!=1+sum(v['kind']=='line' for v in items):raise BridgeError('LEGEND_LINE_COUNT_DIFF')
    moved=bridge.destination_path(stage/('relocated-'+uuid.uuid4().hex[:8]));shutil.copytree(files,moved)
    hidden=bridge.destination_path(stage/('hidden-'+uuid.uuid4().hex[:8]));files.rename(hidden)
    try:
        fresh,readback=invoke_com(bridge.root,moved,stage/'sdk-runtime','legend.inspect',candidate.name,'-','-','native-com-reopen-'+uuid.uuid4().hex[:8]+'.json')
        _check_items(_items(readback),items)
    finally:hidden.rename(files)
    if fresh['pid']==host['pid'] or file_sha(moved/candidate.name)!=saved[0]:raise BridgeError('LEGEND_FRESH_NATIVE_READBACK')
    metadata={'backend':'MapGIS 6.7 mapleg60 CLN with installed SDK style converters and native layout exporter','items':items,'layout':parameters,
        'native_execution':host,'native_reopen':fresh,'vectors':vectors,'portable_relocation':'PASS_WITH_OLD_DIRECTORY_HIDDEN','mouse_or_keyboard_input':False,
        'registry_changed':False,'replayed':False,'reconciled_after_interruption':recovered,
        'scope':'Native CLN item catalog plus separately bound editable WT/WL/WP layout; descriptions remain CLN metadata, names are drawn; symbols/colors resolve in the selected project system library'}
    target.with_suffix('.legend.json').write_text(json_text({'format':'mapgis-agent.legend.v1','cln_sha256':saved[0],
        'members':{v.name:file_sha(v) for v in files.iterdir()},'items':items,'layout':parameters,'coordinate_frame':'native drawing millimetres','map_scale':1}),encoding='utf8')
    return finish_bundle(bridge,request_id,files,metadata)


def reconcile_native_legend(bridge,request_id):
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=bridge.destination_path(receipt['stage'])
    if row['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process=psutil.Process(owner['pid'])
        if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
    except psutil.NoSuchProcess:pass
    complete=completed_native_child(stage/'native','native-com.json')
    if complete is None:raise BridgeError('ARTIFACT_INCOMPLETE: native legend generation was not replayed')
    return finish_native_legend(bridge,request_id,stage,receipt['payload'],*complete,recovered=True)
