"""Editable native drillhole columns/sections, with literal source/result tables."""
from __future__ import annotations
import json,math,os,re,shutil
from pathlib import Path
import numpy as np
import psutil
from shapely.geometry import Point,LineString,box,mapping
from .tables import write_literal_workbook as _workbook
from .core import Bridge,BridgeError,json_text,read_native,sha
from .raster import file_sha
from .borehole import borehole_trajectory,validate_intervals,_frame,_number
from .borehole_tables import read_borehole_workbook
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .creation import prepare_creation
from .native_backend import backend_configuration,prepare_native_operation,reconcile_native_request,native_file_info


def _data(data):
    if not isinstance(data,dict):raise BridgeError('BOREHOLE_DATA')
    if 'workbook' in data:
        if not {'workbook','coordinate_frame'}<=set(data)<={'workbook','coordinate_frame','sort_by_depth'}:raise BridgeError('BOREHOLE_DATA')
        source=data['workbook']
        if not isinstance(source,dict) or set(source)!={'file','expected_sha256','tables'}:raise BridgeError('BOREHOLE_WORKBOOK')
        result=read_borehole_workbook(source['file'],source['expected_sha256'],source['tables'],data['coordinate_frame'],sort_by_depth=data.get('sort_by_depth',False))
        return result,[{'file':result['source']['file'],'sha256':result['source']['sha256']}]
    if set(data)!={'holes','coordinate_frame'}:raise BridgeError('BOREHOLE_DATA: inline holes/frame or an explicit workbook binding required')
    return data,[]


def _text(value,label):
    if not isinstance(value,str) or not value.strip():raise BridgeError('BOREHOLE_TEXT: '+label)
    try:encoded=value.encode('gbk')
    except UnicodeEncodeError as error:raise BridgeError('BOREHOLE_TEXT: native labels require GBK text; '+label) from error
    if b'\0' in encoded or len(encoded)>1024:raise BridgeError('BOREHOLE_TEXT: nonempty text at most1024GBK bytes without NUL; '+label)


def borehole_chart_spec(data,parameters):
    data,sources=_data(data);frame=_frame(data.get('coordinate_frame'))
    common={'mode','title','vertical_scale','column_width_mm','font_mm','tick_interval_metres','lithology_styles','font_zh','font_en'}
    mode=parameters.get('mode') if isinstance(parameters,dict) else None
    extra={'column_spacing_mm'} if mode=='columns' else {'horizontal_scale','reference_elevation','sample_step_md','section'}
    if mode not in {'columns','section'} or set(parameters)-common-extra or not (common-{'font_zh','font_en'})<=set(parameters):raise BridgeError('BOREHOLE_CHART_PARAMETERS')
    for key in ('vertical_scale','column_width_mm','font_mm','tick_interval_metres'):
        if not _number(parameters[key]) or parameters[key]<=0:raise BridgeError('BOREHOLE_CHART_PARAMETERS: positive '+key+' required')
    font=float(parameters['font_mm']);width=float(parameters['column_width_mm']);vscale=float(parameters['vertical_scale'])
    if not 1<=font<=12 or not 1<=width<=100:raise BridgeError('BOREHOLE_CHART_PARAMETERS: font1..12mm and graphic track width1..100mm')
    for key in ('font_zh','font_en'):
        if type(parameters.get(key,1 if key=='font_zh' else 0)) is not int or not 0<=parameters.get(key,1 if key=='font_zh' else 0)<=255:raise BridgeError('BOREHOLE_CHART_PARAMETERS: native font ID0..255')
    _text(parameters['title'],'title')
    styles=parameters['lithology_styles']
    if not isinstance(styles,dict) or not styles:raise BridgeError('BOREHOLE_LITHOLOGY: explicit unit-to-native-region-style mapping required')
    if mode=='columns':
        spacing=parameters.get('column_spacing_mm')
        if not _number(spacing) or spacing<width+12*font:raise BridgeError('BOREHOLE_CHART_PARAMETERS: column spacing must leave an explicit label track')
        hscale=None;origin=None;direction=None
    else:
        hscale=parameters.get('horizontal_scale');reference=parameters.get('reference_elevation');step=parameters.get('sample_step_md');section=parameters.get('section')
        if not all(_number(v) for v in (hscale,reference,step)) or hscale<=0 or step<=0:raise BridgeError('BOREHOLE_SECTION: explicit positive horizontal scale/sample step and finite reference elevation required')
        if not isinstance(section,dict) or set(section)!={'start','end','max_crossline_distance'}:raise BridgeError('BOREHOLE_SECTION')
        if any(not isinstance(section[k],list) or len(section[k])!=2 or not all(_number(v) for v in section[k]) for k in ('start','end')):
            raise BridgeError('BOREHOLE_SECTION: finite start/end in original horizontal metre frame')
        origin=np.array(section['start'],dtype=float);vector=np.array(section['end'],dtype=float)-origin;length=float(np.linalg.norm(vector))
        if not length>0 or not _number(section['max_crossline_distance']) or section['max_crossline_distance']<0:raise BridgeError('BOREHOLE_SECTION: distinct start/end and nonnegative sampled crossline distance tolerance')
        direction=vector/length
    holes=data.get('holes')
    if not isinstance(holes,list) or not 1<=len(holes)<=100:raise BridgeError('BOREHOLE_CAPACITY:1..100 holes per drawing')
    features={k:{'type':'FeatureCollection','features':[]} for k in ('POINT','LINE','POLYGON')};normalized=[];unlogged=[];samples=[];ids=set();total_intervals=0
    def add(kind,geometry,properties,style,text=None):
        feature={'type':'Feature','id':str(len(features[kind]['features'])+1),'geometry':mapping(geometry),'properties':properties,'style':style}
        if text is not None:_text(text,'annotation');feature['text']=text
        features[kind]['features'].append(feature)
    line_style={'linetype':1,'aux_linetype':0,'color':1,'width':.18}
    text_style={'color':1,'height':font,'width':font*.8,'font_zh':parameters.get('font_zh',1),'font_en':parameters.get('font_en',0)}
    def label(x,y,text,hole='',role='label',unit=''):
        add('POINT',Point(x,y),{'HOLE':hole,'ROLE':role,'UNIT':unit},text_style,text)
    def line(points,hole='',role='axis'):
        if len(points)>1 and any(a!=b for a,b in zip(points,points[1:])):add('LINE',LineString(points),{'HOLE':hole,'ROLE':role},line_style)
    maximum_depth=0.;maximum_offset=0.
    for index,hole in enumerate(holes):
        if not isinstance(hole,dict) or not {'id','collar','survey','intervals'}<=set(hole)<={'id','collar','survey','intervals','source_rows','unlogged_intervals'}:raise BridgeError('BOREHOLE_DATA: hole id/collar/survey/intervals required')
        identity=hole['id'];_text(identity,'hole ID')
        if identity in ids:raise BridgeError('BOREHOLE_DUPLICATE: '+identity)
        ids.add(identity);base=borehole_trajectory(hole['survey'],hole['collar'],frame);total=base['survey_samples'][-1]['md'];maximum_depth=max(maximum_depth,total)
        intervals,gaps=validate_intervals(hole['intervals'],total);total_intervals+=len(intervals)
        if total_intervals>10000:raise BridgeError('BOREHOLE_CAPACITY: at most10000 logged intervals')
        for item in intervals:
            _text(item['unit'],'unit')
            if item['unit'] not in styles or not isinstance(styles[item['unit']],dict):raise BridgeError('BOREHOLE_LITHOLOGY: missing explicit style for '+item['unit'])
        query={v for item in [*intervals,*gaps] for v in (item['top'],item['base'],(item['top']+item['base'])/2)}|{s['md'] for s in hole['survey']}
        if mode=='section':
            count=math.ceil(total/step)+1
            if count>50000:raise BridgeError('BOREHOLE_CAPACITY: reduce sampled MD count or split the drawing')
            query.update(np.linspace(0,total,count).tolist())
        query=sorted(query)
        if len(samples)+len(query)>100000:raise BridgeError('BOREHOLE_CAPACITY: at most100000 sampled positions per drawing')
        result=borehole_trajectory(hole['survey'],hole['collar'],frame,query_mds=query);positions={row['md']:row for row in result['query_samples']}
        for row in result['query_samples']:
            sample={'hole_id':identity,**row}
            if mode=='section':
                delta=np.array([row['easting'],row['northing']])-origin;station=float(delta@direction);offset=float(direction[0]*delta[1]-direction[1]*delta[0])
                maximum_offset=max(maximum_offset,abs(offset));sample.update(section_distance=station,crossline_offset=offset)
                if abs(offset)>section['max_crossline_distance']+1e-9:raise BridgeError('BOREHOLE_SECTION_OFFSET: sampled well position outside the explicit section-plane tolerance; '+identity)
                sample.update(drawing_x=station*1000/hscale,drawing_y=(row['elevation']-reference)*1000/vscale)
            else:sample.update(drawing_x=index*spacing,drawing_y=-row['md']*1000/vscale)
            samples.append(sample);positions[row['md']]=sample
        if mode=='columns':
            x=index*spacing;line([(x,0),(x,-total*1000/vscale)],identity,'column_edge');line([(x+width,0),(x+width,-total*1000/vscale)],identity,'column_edge')
            label(x,2*font,identity,identity,'hole_name');label(x,3.8*font,f"孔口高程 {hole['collar']['elevation']:g} m",identity,'collar')
            label_x=x+width+font
        else:
            points=[(positions[m]['drawing_x'],positions[m]['drawing_y']) for m in query];line(points,identity,'trajectory')
            collar=positions[0.];label(collar['drawing_x'],collar['drawing_y']+2*font,identity,identity,'hole_name')
            label_x=max(p[0] for p in points)+width+font
            for station in result['survey_samples']:
                p=positions[station['md']];add('POINT',Point(p['drawing_x'],p['drawing_y']),{'HOLE':identity,'ROLE':'survey_station','UNIT':''},
                    {'point_type':2,'radius_mm':.7,'border_color':1,'filled':1,'color':1})
        pending=[]
        for item in intervals:
            top,bottom,unit=item['top'],item['base'],item['unit'];middle=positions[(top+bottom)/2]
            if mode=='columns':geometry=box(index*spacing,-bottom*1000/vscale,index*spacing+width,-top*1000/vscale)
            else:
                path=[(positions[m]['drawing_x'],positions[m]['drawing_y']) for m in query if top<=m<=bottom]
                curve=LineString(path)
                if curve.length<=1e-10:raise BridgeError('BOREHOLE_SECTION_COLLAPSED: projected interval has no drawable length; '+identity)
                geometry=curve.buffer(width/2,cap_style='flat',join_style='mitre')
            if geometry.is_empty or not geometry.is_valid:raise BridgeError('BOREHOLE_CHART_GEOMETRY')
            add('POLYGON',geometry,{'HOLE':identity,'MD_TOP':top,'MD_BASE':bottom,'UNIT':unit},styles[unit])
            for poly in (list(geometry.geoms) if hasattr(geometry,'geoms') else [geometry]):
                line(list(poly.exterior.coords),identity,'interval_border')
                for ring in poly.interiors:line(list(ring.coords),identity,'interval_border')
            pending.append((middle['drawing_y'],middle['drawing_x']+(width if mode=='columns' else width/2),f'{unit} {top:g}-{bottom:g} m',unit))
        for gap in gaps:
            unlogged.append({'hole_id':identity,**gap});middle=positions[(gap['top']+gap['base'])/2]
            pending.append((middle['drawing_y'],middle['drawing_x']+(width if mode=='columns' else width/2),f"未编录 {gap['top']:g}-{gap['base']:g} m",''))
        last_y=math.inf
        for center_y,start_x,text,unit in sorted(pending,reverse=True):
            y=min(center_y,last_y-1.6*font);last_y=y;label(label_x,y-font*.4,text,identity,'interval_label' if unit else 'unlogged_label',unit)
            line([(start_x,center_y),(label_x-font*.3,y)],identity,'label_leader')
        normalized.append({'id':identity,'collar':dict(hole['collar']),'survey':hole['survey'],'intervals':intervals,'source_rows':hole.get('source_rows',{}),
            'survey_samples':result['survey_samples']})
    if not features['POLYGON']['features']:raise BridgeError('BOREHOLE_LITHOLOGY: no logged intervals to draw')
    all_points=[(v['drawing_x'],v['drawing_y']) for v in samples];xmin=min(p[0] for p in all_points);xmax=max(p[0] for p in all_points);ymin=min(p[1] for p in all_points);ymax=max(p[1] for p in all_points)
    axis_x=xmin-width-font*4
    if mode=='columns':
        line([(axis_x,0),(axis_x,-maximum_depth*1000/vscale)])
        ticks=math.floor(maximum_depth/parameters['tick_interval_metres'])+1
        if ticks>2000:raise BridgeError('BOREHOLE_CHART_TICKS')
        for i in range(ticks):
            value=i*parameters['tick_interval_metres'];y=-value*1000/vscale;line([(axis_x-font,y),(axis_x,y)]);label(axis_x-font*5,y-font*.4,f'{value:g}',role='depth_tick')
        label(axis_x-font*5,font*4,'测深 / m',role='axis_name');scale_note=f'测深比例 1:{vscale:g}；水平为排版间距'
    else:
        line([(axis_x,ymin),(axis_x,ymax)]);line([(axis_x,ymin-font*5),(xmax+width,ymin-font*5)])
        low=min(v['elevation'] for v in samples);high=max(v['elevation'] for v in samples);tick=parameters['tick_interval_metres']
        count=math.floor(high/tick)-math.ceil(low/tick)+1
        if count>2000:raise BridgeError('BOREHOLE_CHART_TICKS')
        for i in range(math.ceil(low/tick),math.floor(high/tick)+1):
            value=i*tick;y=(value-reference)*1000/vscale;line([(axis_x-font,y),(axis_x,y)]);label(axis_x-font*5,y-font*.4,f'{value:g}',role='elevation_tick')
        label(axis_x-font*5,ymax+font*2,'高程 / m',role='axis_name')
        lo=min(v['section_distance'] for v in samples);hi=max(v['section_distance'] for v in samples)
        if math.floor(hi/tick)-math.ceil(lo/tick)>2000:raise BridgeError('BOREHOLE_CHART_TICKS')
        for i in range(math.ceil(lo/tick),math.floor(hi/tick)+1):
            value=i*tick;x=value*1000/hscale;line([(x,ymin-font*5),(x,ymin-font*6)]);label(x-font,ymin-font*8,f'{value:g}',role='section_tick')
        label(axis_x,ymin-font*12,'剖面距离 / m',role='axis_name');scale_note=f'H 1:{hscale:g}  V 1:{vscale:g}  VE {hscale/vscale:g}x；点为测斜站，曲线为最小曲率模型'
    label(axis_x,ymax+font*9,parameters['title'],role='title')
    label(axis_x,ymin-font*16,scale_note,role='scale_note')
    label(axis_x,ymin-font*18,'未编录留白；孔间未进行地层对比',role='missingness_note')
    return {'features':features,'normalized_holes':normalized,'trajectory_samples':samples,'unlogged_intervals':unlogged,'sources':sources,'coordinate_frame':frame,
        'drawing_contract':{'vertical_basis':'measured_depth' if mode=='columns' else 'elevation','horizontal_basis':'layout_spacing' if mode=='columns' else 'signed_projection_on_declared_straight_section_plane',
            'drawing_unit':'millimetre','horizontal_scale':hscale,'vertical_scale':vscale,'vertical_exaggeration':None if hscale is None else hscale/vscale,
            'reference_elevation':parameters.get('reference_elevation'),'graphic_track_width_mm':width,'track_width_is_borehole_diameter':False,
            'sample_step_md':parameters.get('sample_step_md'),'maximum_sampled_crossline_distance_metres':maximum_offset,'native_3d_file':False,
            'cross_hole_geology_inferred':False,'unlogged_gaps_filled':False,'minimum_curvature_is_interpolation':True}}


def _sheets(spec):
    result={'孔口':[['孔号','东坐标_m','北坐标_m','高程_m','源行号']],'测斜':[['孔号','测深_m','倾斜角_deg','方位角_deg','源行号']],
        '分层':[['孔号','顶深_m','底深_m','地层','说明','源行号']],'轨迹采样':[['孔号','测深_m','TVD_m','东坐标_m','北坐标_m','高程_m','剖面距离_m','侧向偏距_m','图面X_mm','图面Y_mm']],
        '未编录':[['孔号','顶深_m','底深_m']],'说明':[['项目','值'],['坐标约定',json_text(spec['coordinate_frame'])],['图面约定',json_text(spec['drawing_contract'])]]}
    for hole in spec['normalized_holes']:
        source=hole['source_rows'];c=hole['collar'];result['孔口'].append([hole['id'],c['easting'],c['northing'],c['elevation'],source.get('collar','')])
        for i,row in enumerate(hole['survey']):result['测斜'].append([hole['id'],row['md'],row['inclination'],row['azimuth'],source.get('surveys',['']*len(hole['survey']))[i]])
        for i,row in enumerate(hole['intervals']):result['分层'].append([hole['id'],row['top'],row['base'],row['unit'],row['description'],source.get('intervals',['']*len(hole['intervals']))[i]])
    for row in spec['trajectory_samples']:result['轨迹采样'].append([row['hole_id'],row['md'],row['true_vertical_depth'],row['easting'],row['northing'],row['elevation'],
        row.get('section_distance',''),row.get('crossline_offset',''),row['drawing_x'],row['drawing_y']])
    for row in spec['unlogged_intervals']:result['未编录'].append([row['hole_id'],row['top'],row['base']])
    return result


def _advance(bridge,request_id,stage,spec,parameters,template,recovered=False):
    _,bas=backend_configuration(bridge.root);encoder=Bridge(stage/'encoder')
    config=encoder.root/'config.local.json';expected=json_text({'mapgis_installation':str(bas.parent.parent),'native_backend_legacy_dep_compatibility':True})
    if config.exists() and config.read_text(encoding='utf8')!=expected:raise BridgeError('BOREHOLE_ENCODER_CONFIG_CHANGED')
    if not config.exists():config.write_text(expected,encoding='utf8')
    files=stage/'files';files.mkdir(exist_ok=True);name=Path(template['name']);native={};saves={};frame={'kind':'local','name':'Borehole chart drawing plane','unit':'millimetre','axis_order':'xy','reference_sha256':file_sha(stage/'chart-spec.json')}
    for kind,suffix in [('POLYGON','.wp'),('LINE','.wl'),('POINT','.wt')]:
        identity=kind.lower();created=prepare_creation(encoder,spec['features'][kind],{'name':'chart'+suffix,'crs':frame,'map_scale':1},identity+'-encode')
        source=encoder.apply(identity+'-encode');child_id=identity+'-save'
        with encoder.connect() as db:old=db.execute('SELECT state FROM commands WHERE id=?',(child_id,)).fetchone()
        if old and old['state'] in {'RUNNING','FAILED'}:prepare=reconcile_native_request(encoder,child_id)
        else:prepare=prepare_native_operation(encoder,'resave',source['output'],source['candidate_sha256'],{},child_id)
        saved=encoder.apply(child_id);target=files/name.with_suffix(suffix).name
        if target.exists():
            if file_sha(target)!=saved['candidate_sha256']:raise BridgeError('BOREHOLE_RECOVERY_FILE_CHANGED')
        else:shutil.copyfile(saved['output'],target)
        info=native_file_info(bridge.root,str(target),file_sha(target));blob,reader,_=read_native(target)
        if info['active']!=len(spec['features'][kind]['features']) or len(reader.geom)!=info['active']:raise BridgeError('BOREHOLE_NATIVE_COUNT')
        native[kind]=info
        saves[kind]=saved['native_execution']
    workbook=files/name.with_suffix('.xlsx').name;_workbook(workbook,_sheets(spec))
    metadata={'backend':'wellpathpy/SciPy minimum curvature, existing native WT/WL/WP encoder and original BAS60 save/reopen','native_files':native,'native_saves':saves,
        'drawing_contract':spec['drawing_contract'],'feature_counts':{k:len(v['features']) for k,v in spec['features'].items()},
        'unlogged_intervals':spec['unlogged_intervals'],'source_preserved':True,'mouse_or_keyboard_input':False,'replayed':False,'reconciled_after_interruption':recovered,
        'scope':'Editable2D drawing from explicit drillhole surveys and logs; no subsurface correlation or native3D-file inference'}
    sidecar=files/name.with_suffix('.json').name;body=json_text({'schema':'mapgis-agent/borehole-chart/1','input_frame':spec['coordinate_frame'],
        'parameters':parameters,'drawing_contract':spec['drawing_contract'],'holes':spec['normalized_holes'],'unlogged_intervals':spec['unlogged_intervals'],
        'members':{f.name:file_sha(f) for f in files.iterdir() if f.suffix.lower() in ('.wt','.wl','.wp','.xlsx')},'source_files':spec['sources']})
    if sidecar.exists() and sidecar.read_text(encoding='utf8')!=body:raise BridgeError('BOREHOLE_RECOVERY_METADATA_CHANGED')
    if not sidecar.exists():sidecar.write_text(body,encoding='utf8')
    return finish_bundle(bridge,request_id,files,metadata)


def prepare_borehole_chart(bridge,data,parameters,template,request_id):
    if not isinstance(template,dict) or set(template)!={'name'} or not isinstance(template['name'],str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}\.wp',template['name']):raise BridgeError('BOREHOLE_TEMPLATE: lowercase ASCII WP basename required')
    request_id=bridge.request_id(request_id);backend_configuration(bridge.root);spec=borehole_chart_spec(data,parameters)
    payload={'operation':'geology.borehole_chart','data':data,'parameters':parameters,'template':template,'chart_protocol':1,
        'chart_spec_sha256':sha(json_text(spec).encode('utf8'))};encoded=json_text(payload).encode('utf8')
    intent=bridge.destination_path(bridge.root/'work/borehole-intents'/(sha(request_id.encode())+'.json'));intent.parent.mkdir(parents=True,exist_ok=True)
    if intent.exists() and intent.read_bytes()!=encoded:raise BridgeError('REQUEST_ID_CONFLICT')
    if not intent.exists():
        with intent.open('xb') as stream:stream.write(encoded)
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(intent),'sha256':sha(encoded)},*spec['sources']],template['name'])
    if not new:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        (stage/'chart-spec.json').write_text(json_text(spec),encoding='utf8')
        return _advance(bridge,request_id,stage,spec,parameters,template)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def reconcile_borehole_chart(bridge,request_id):
    receipt=bridge.status(request_id);stage=Path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if receipt['state']=='RUNNING':
        owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(owner['pid'])
            if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
        except psutil.NoSuchProcess:pass
    for source in receipt['sources']:
        if file_sha(Path(source['file']))!=source['sha256']:raise BridgeError('STALE_SOURCE')
    payload=receipt['payload'];spec_file=stage/'chart-spec.json'
    if file_sha(spec_file)!=payload['chart_spec_sha256']:raise BridgeError('BOREHOLE_RECOVERY_SPEC_CHANGED')
    spec=json.loads(spec_file.read_text(encoding='utf8'))
    return _advance(bridge,request_id,stage,spec,payload['parameters'],payload['template'],True)
