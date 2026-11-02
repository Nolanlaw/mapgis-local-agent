"""Original Tin3DView rendering of byte-preserved WTIN and exact local cameras."""
from __future__ import annotations
import json,math,os,shutil,struct,uuid
from pathlib import Path
from typing import Literal
import numpy as np
import psutil
import rasterio
from pydantic import BaseModel,ConfigDict,Field,StrictInt,model_validator,ValidationError
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text,sha
from .grid_analysis import _linear_scale
from .native_backend import backend_configuration,_run_child,completed_native_child
from .native_paths import NativePathAlias
from .native_raster import _native_image,_raster_sources
from . import native_scene as scene_images
from .native_scene import _source_member
from .native_tin import _read_native,_tin_frame
from .raster import file_sha
from .raster_tiles import read_native_tiles
from .scene_texture import TextureInput,_texture_pixels,prepare_texture
from .scene_lines import prepare_lines,line_profile
from .tin_lines import TinLineOverlayInput,_transport as _line_transport
from .tin_flight import TinFlightParameters,flight_settings,validate_flight
from .visibility import _checkpoint,_producer_alive

FORMAT='mapgis-agent.native-tin-scene.v1'
MODULES={'Tin3DView.dll':'05de544e830293a282b25adbcfeb8b0b4f67c2b5d49e6dedca6c2251a9351783',
    'Tin3DBase.dll':'4d40a8467a889f7cc279b00fe27310fbb40b11f43954188cb22965b839dd8a35',
    'C3DView.dll':'2e5cb1425d23d9517317458cc2fd66e1572f07fe4ab8b54f102696f156f4eca4',
    'COglDraw.dll':'74ae1996c7852c079ecb18c7884de1c0835e5c45551de0cfe8eabd6d61b88403',
    'W60_Tin.dll':'94e25abdb102fb50d791aa689b114fddd48dfecf9eb24a2a82329ed4ddce0404',
    'TinBas60.dll':'ac02d4eb00fcf2dfa9b8f64703f08b28af4bfa171bf60d9cb15d93d341df8270',
    'basroot.dll':'d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba',
    'mapdsp60.dll':'e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb',
    'msibase.dll':'1a2098e4f0086d4f7fec7de2c6b1f7c3705b31ab2bf216ced9bb9a21647e7245',
    'TexDbs.dll':'b6b6fa0d701c9ff939343eaf87a3b7e7e188ec4626f89205c25248cc5c363c08'}

def _native_vector(values):
    with np.errstate(over='ignore',under='ignore'):return np.asarray(values,dtype='float64').astype('float32')

class NativeTinCamera(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    profile:Literal['tin3d67.camera.v1']='tin3d67.camera.v1'
    origin:list[float]=Field(min_length=2,max_length=2)
    scales:list[float]=Field(min_length=3,max_length=3)
    eye_model:list[float]=Field(min_length=3,max_length=3)
    target_model:list[float]=Field(min_length=3,max_length=3)
    up:list[float]=Field(min_length=3,max_length=3)
    fov_degrees:float=Field(ge=30,le=120)

    @model_validator(mode='after')
    def native_pose(self):
        data=_native_vector(self.scales+self.eye_model+self.target_model+self.up+[self.fov_degrees])
        if not np.isfinite(data).all() or np.any(data[:3]<=0):raise ValueError('finite native floats and positive scales required')
        direction=data[3:6].astype('float64')-data[6:9];up=data[9:12].astype('float64')
        if np.linalg.norm(np.cross(direction,up))<=1e-8*np.linalg.norm(direction)*np.linalg.norm(up) or not np.linalg.norm(direction) or not np.linalg.norm(up):
            raise ValueError('camera direction and up must span a plane')
        return self

class TinSceneParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    vertical_unit:Literal['metre']|None=None
    width:StrictInt=Field(default=640,ge=32,le=2048)
    height:StrictInt=Field(default=480,ge=32,le=2048)
    fov_degrees:float|None=Field(default=None,ge=30,le=120)
    scales:list[float]|None=Field(default=None,min_length=3,max_length=3)
    eye:list[float]|None=Field(default=None,min_length=3,max_length=3)
    target:list[float]|None=Field(default=None,min_length=3,max_length=3)
    native_camera:NativeTinCamera|None=None

    @model_validator(mode='after')
    def view(self):
        if (self.eye is None)!=(self.target is None):raise ValueError('eye and target are a pair')
        if self.native_camera and (self.eye is not None or self.scales is not None):raise ValueError('saved camera replaces eye, target and scales')
        if self.scales is not None:
            values=_native_vector(self.scales)
            if not np.isfinite(values).all() or np.any(values<=0):raise ValueError('positive native float scales required')
        if self.eye is not None and self.eye[:2]==self.target[:2]:raise ValueError('camera must have a horizontal direction')
        return self

def _camera_bytes(pose):
    return struct.pack('<8s2d13f',b'MGTK0002',*pose.origin,*pose.scales,*pose.eye_model,*pose.target_model,*pose.up,pose.fov_degrees)

def _camera_read(file):
    raw=file.read_bytes()
    if len(raw)!=76 or raw[:8]!=b'MGTK0002':raise BridgeError('TIN_SCENE_CAMERA_PROFILE')
    data=struct.unpack('<8s2d13f',raw)
    try:return NativeTinCamera(origin=list(data[1:3]),scales=list(data[3:6]),eye_model=list(data[6:9]),
        target_model=list(data[9:12]),up=list(data[12:15]),fov_degrees=data[15])
    except ValidationError as error:raise BridgeError('TIN_SCENE_CAMERA_PROFILE') from error

def _world_camera(pose):
    def world(values):return [values[i]/pose.scales[i]+(pose.origin[i] if i<2 else 0.) for i in range(3)]
    return world(pose.eye_model),world(pose.target_model)

def _frame(frame,vertical,sidecar):
    if vertical!='metre' or sidecar and sidecar.get('vertical_unit')!='metre':raise BridgeError('TIN_SCENE_VERTICAL_METRE_REQUIRED')
    if not isinstance(frame,dict):raise BridgeError('TIN_SCENE_PROJECTED_METRE_FRAME')
    try:crs,_=_linear_scale(frame,'metre')
    except BridgeError as error:raise BridgeError('TIN_SCENE_PROJECTED_METRE_FRAME') from error
    horizontal=1. if crs is None and frame.get('unit')=='metre' else crs.axis_info[0].unit_conversion_factor if crs else 0.
    if not math.isclose(horizontal,1.,rel_tol=1e-12):raise BridgeError('TIN_SCENE_PROJECTED_METRE_FRAME')
    if sidecar and json_text(sidecar['crs'])!=json_text(frame):raise BridgeError('TIN_SCENE_FRAME_CHANGED')
    return frame

def _scene_source(file,digest):
    if file_sha(file)!=digest:raise BridgeError('STALE_SOURCE')
    record=json.loads(file.read_text(encoding='utf8'))
    if record.get('format')!=FORMAT or record.get('native_tin')!='model.tin':raise BridgeError('TIN_SCENE_DOCUMENT_FORMAT')
    members=record.get('members')
    if not isinstance(members,dict) or not {'model.tin','camera.bin'}<=members.keys() or not members.keys()<= {'model.tin','model.tin.json','camera.bin','scene.msi','scene.tif','scene.png','texture.tif','texture.msi','overlay.wl','lines.xyz','flight.avi','flight.pat','flight.txt','initial-camera.bin'}:
        raise BridgeError('TIN_SCENE_MEMBERS')
    sources=[{'file':str(file),'sha256':digest}]
    for name,wanted in members.items():
        path=_source_member(file.parent,name)
        if file_sha(path)!=wanted:raise BridgeError('TIN_SCENE_MEMBER_CHANGED')
        sources.append({'file':str(path),'sha256':wanted})
    pose=_camera_read(file.parent/'camera.bin')
    if pose.model_dump()!=record.get('camera'):raise BridgeError('TIN_SCENE_CAMERA_BINDING')
    sidecar,_=_tin_frame(file.parent/'model.tin',members['model.tin'])
    _frame(record.get('frame'),'metre',sidecar)
    texture_names=members.keys() & {'texture.tif','texture.msi'}
    if texture_names:
        if texture_names!={'texture.tif','texture.msi'} or not isinstance(record.get('texture'),dict):raise BridgeError('TIN_SCENE_TEXTURE_BINDING')
        description=_texture_description(file.parent/'texture.tif',record['frame'],record['metrics']['bounds'])
        if description!=record['texture']:raise BridgeError('TIN_SCENE_TEXTURE_BINDING')
    elif record.get('texture') is not None:raise BridgeError('TIN_SCENE_TEXTURE_BINDING')
    line_names=members.keys() & {'overlay.wl','lines.xyz'};description=record.get('line_overlay')
    if line_names:
        if line_names!={'overlay.wl','lines.xyz'} or not isinstance(description,dict) or description.get('native_xyz')!='lines.xyz':raise BridgeError('TIN_SCENE_LINE_BINDING')
        profile=line_profile(file.parent/'overlay.wl',record['frame'],{'bounds':record['metrics']['bounds']},description.get('source_crs'))
        if any(description.get(k)!=v for k,v in profile.items() if k!='drape'):raise BridgeError('TIN_SCENE_LINE_BINDING')
    elif description is not None:raise BridgeError('TIN_SCENE_LINE_BINDING')
    flight_names=members.keys() & {'flight.avi','flight.pat','flight.txt','initial-camera.bin'}
    if flight_names:
        description=record.get('flight')
        if flight_names!={'flight.avi','flight.pat','flight.txt','initial-camera.bin'} or not isinstance(description,dict):raise BridgeError('TIN_SCENE_FLIGHT_BINDING')
        initial=_camera_read(file.parent/'initial-camera.bin')
        if initial.model_dump()!=description.get('initial_camera'):raise BridgeError('TIN_SCENE_FLIGHT_BINDING')
        spec=TinFlightParameters.model_validate(description['input'])
        if flight_settings(spec,initial,record['width'],record['height'])!=(file.parent/'flight.txt').read_text(encoding='ascii'):raise BridgeError('TIN_SCENE_FLIGHT_BINDING')
    elif record.get('flight') is not None:raise BridgeError('TIN_SCENE_FLIGHT_BINDING')
    return record,sources

def _texture_description(file,frame,bounds):
    _,_,info=_texture_pixels(file,frame,{'bounds':bounds})
    origin=bounds[:2];world=info['bounds']
    return info|{'native':'texture.msi','source':'texture.tif','display_shift':origin,
        'native_bounds':[world[i]-origin[i%2] for i in range(4)]}

def _texture_native_readback(root,stage,msi,canonical,info):
    executable,bas=backend_configuration(root);stage.mkdir()
    raw=stage/'texture.tiles';host,events=_native_image(executable,bas,stage/'native','to_raw',msi,raw)
    read_native_tiles(raw,stage/'texture.tif',{'transform':info['transform'],'color_interpretation':['red','green','blue']})
    with rasterio.open(canonical) as source,rasterio.open(stage/'texture.tif') as actual:
        if not np.array_equal(source.read(),actual.read()) or actual.dtypes!=('uint8',)*3:raise BridgeError('TIN_SCENE_TEXTURE_NATIVE_PIXELS')
    extents=[v['native_bounds'] for v in events if v.get('event')=='image_extent']
    if len(extents)!=1 or any(not math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-7) for a,b in zip(extents[0],info['native_bounds'])):
        raise BridgeError('TIN_SCENE_TEXTURE_NATIVE_EXTENT')
    return host

def _prepare_tin_texture(root,stage,image,sources,frame,bounds):
    prepare_texture(root,stage,image,sources,frame,{'bounds':bounds})
    canonical=stage/'texture.tif';info=_texture_description(canonical,frame,bounds)
    work=stage/'texture-preprocess';world=stage/'native/texture.msi'
    world.rename(work/'texture-world.msi')
    with rasterio.open(canonical) as ds:
        t=ds.transform;w=ds.width;h=ds.height
    command=work/'local-gcps.txt';origin=bounds[:2]
    command.write_text(''.join(' '.join(map(str,[x,y,*(np.asarray(t*(x,y))-origin)]))+'\n' for x,y in [(0,0),(w,0),(w,h),(0,h)]),encoding='ascii')
    executable,bas=backend_configuration(root)
    _native_image(executable,bas,work/'local-create','from_raw',work/'texture.tiles',world,command)
    _texture_native_readback(root,work/'local-readback',world,canonical,info)
    return info

def _runtime(root):
    executable,bas=backend_configuration(root);files={str(executable):file_sha(executable),str(bas):file_sha(bas)}
    for name,wanted in MODULES.items():
        path=bas.parent/name
        if file_sha(path)!=wanted:raise BridgeError('TIN_SCENE_NATIVE_BUILD_MISMATCH')
        files[str(path)]=wanted
    return executable,bas.parent,files

def _settings(options):
    return ' '.join(format(v,'.17g') for v in [options.width,options.height,options.fov_degrees,
        int(options.scales is not None),*(options.scales or [1.,1.,1.]),int(options.eye is not None),
        *(options.eye or [0.,0.,0.]),*(options.target or [0.,0.,0.])])+'\n'

def _invoke(root,folder,log,mode='render'):
    executable,program,_=_runtime(root)
    with NativePathAlias(folder) as alias:
        return _run_child([str(executable),'tin.scene',mode,str(program),str(alias.path/'model.tin'),
            str(alias.path),str(alias.path/'settings.txt')],folder,log,mfc_windows=True)

def prepare_native_tin_scene(bridge:Bridge,operation,file,expected_sha256,parameters,template,request_id,expected_members=None,texture=None,remove_texture=False,line_overlay=None,remove_line_overlay=False,flight=None):
    if operation not in {'create','render','flight'}:raise BridgeError('TIN_SCENE_OPERATION')
    flying=TinFlightParameters.model_validate(flight) if flight is not None else None
    if (operation=='flight')!=bool(flying):raise BridgeError('TIN_SCENE_FLIGHT_OPTIONS')
    if isinstance(parameters,TinSceneParameters):parameters=parameters.model_dump(exclude_unset=True)
    image=TextureInput.model_validate(texture) if texture is not None else None
    lines=TinLineOverlayInput.model_validate(line_overlay) if line_overlay is not None else None
    if (image and remove_texture) or (remove_texture and operation=='create'):raise BridgeError('TIN_SCENE_TEXTURE_OPTIONS')
    if (lines and remove_line_overlay) or (remove_line_overlay and operation=='create'):raise BridgeError('TIN_SCENE_LINE_OPTIONS')
    if set(template)!={'name','crs'} or not isinstance(template['name'],str) or not template['name'].endswith('.scene.json'):
        raise BridgeError('TIN_SCENE_TEMPLATE')
    source=Path(file).resolve(strict=True);document=None
    if operation=='create':
        if expected_members is not None:raise BridgeError('TIN_SCENE_MEMBERS: only saved scene render accepts expected_members')
        options=TinSceneParameters.model_validate(parameters);sidecar,sources=_tin_frame(source,expected_sha256)
        _frame(template['crs'],options.vertical_unit,sidecar)
        model=source
    else:
        if 'vertical_unit' in parameters:raise BridgeError('TIN_SCENE_RENDER_CALIBRATION')
        document,sources=_scene_source(source,expected_sha256)
        if expected_members is not None and expected_members!=document['members']:raise BridgeError('TIN_SCENE_MEMBERS')
        _frame(template['crs'],'metre',{'crs':document['frame'],'vertical_unit':'metre'})
        saved={'width':document['width'],'height':document['height']}
        if not {'native_camera','eye','target','scales'} & parameters.keys():saved['native_camera']=document['flight']['initial_camera'] if flying and document.get('flight') else document['camera']
        options=TinSceneParameters.model_validate(saved|parameters);model=source.parent/'model.tin'
    if options.fov_degrees is None:options=options.model_copy(update={'fov_degrees':options.native_camera.fov_degrees if options.native_camera else 90.})
    if flying and options.native_camera is None:raise BridgeError('TIN_FLIGHT_SAVED_CAMERA_REQUIRED: render explicit view or scales first')
    if flying:
        pose=options.native_camera
        native_pose=pose.model_copy(update={key:_native_vector(getattr(pose,key)).tolist() for key in ['scales','eye_model','target_model','up']}|{'fov_degrees':float(np.float32(options.fov_degrees))})
        options=options.model_copy(update={'native_camera':native_pose})
    image_sources=[]
    if image:
        image=image.model_copy(update={'file':str(Path(image.file).resolve(strict=True))})
        image_sources=_raster_sources(Path(image.file),image.expected_sha256,image.expected_members)
        known={v['file']:v['sha256'] for v in sources}
        for item in image_sources:
            if item['file'] in known:
                if known[item['file']]!=item['sha256']:raise BridgeError('STALE_SOURCE')
            else:sources.append(item);known[item['file']]=item['sha256']
    if lines:
        lines=lines.model_copy(update={'file':str(Path(lines.file).resolve(strict=True))})
        known={v['file']:v['sha256'] for v in sources}
        if lines.file in known and known[lines.file]!=lines.expected_sha256:raise BridgeError('STALE_SOURCE')
        if lines.file not in known:sources.append({'file':lines.file,'sha256':lines.expected_sha256})
    _,_,runtime=_runtime(bridge.root)
    payload={'operation':'native.scene.tin.'+operation,'file':str(source),'expected_sha256':expected_sha256,
        'parameters':options.model_dump(exclude_unset=True),'template':template,'expected_members':expected_members,'tin_scene_protocol':3,
        'texture':image.model_dump() if image else None,'remove_texture':remove_texture,
        'line_overlay':lines.model_dump() if lines else None,'remove_line_overlay':remove_line_overlay}
    if flying:payload.update(flight=flying.model_dump(),tin_scene_protocol=4)
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        owner=psutil.Process();_checkpoint(stage/'producer.json',{'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()})
        native=stage/'native';native.mkdir();shutil.copy2(model,native/'model.tin')
        if model.with_name(model.name+'.json').is_file():shutil.copy2(model.with_name(model.name+'.json'),native/'model.tin.json')
        xyz,faces,edges,metrics,host=_read_native(bridge.root,native/'model.tin',stage/'input-read')
        if options.native_camera and options.native_camera.origin!=metrics['bounds'][:2]:raise BridgeError('TIN_SCENE_CAMERA_BASIS')
        texture_plan=None
        if image:texture_plan=_prepare_tin_texture(bridge.root,stage,image,image_sources,template['crs'],metrics['bounds'])
        elif document and document.get('texture') and not remove_texture:
            for name in ['texture.msi','texture.tif']:shutil.copy2(source.parent/name,(native if name.endswith('.msi') else stage)/name)
            texture_plan=_texture_description(stage/'texture.tif',template['crs'],metrics['bounds'])
            if texture_plan!=document['texture']:raise BridgeError('TIN_SCENE_TEXTURE_BINDING')
            _texture_native_readback(bridge.root,stage/'texture-inherited-readback',native/'texture.msi',stage/'texture.tif',texture_plan)
        line_plan=None
        if lines:
            prepare_lines(stage,lines,template['crs'],{'bounds':metrics['bounds']})
            blob,line_plan=_line_transport(native/'overlay.wl',template['crs'],metrics['bounds'],xyz,faces,edges,lines.height_offset_m,lines.source_crs)
            (native/'lines.xyz').write_bytes(blob)
        elif document and document.get('line_overlay') and not remove_line_overlay:
            description=document['line_overlay'];shutil.copy2(source.parent/'overlay.wl',native/'overlay.wl')
            blob,line_plan=_line_transport(native/'overlay.wl',template['crs'],metrics['bounds'],xyz,faces,edges,description['height_offset_m'],description.get('source_crs'))
            if line_plan!=description or blob!=(source.parent/'lines.xyz').read_bytes():raise BridgeError('TIN_SCENE_LINE_BINDING')
            (native/'lines.xyz').write_bytes(blob)
        (native/'settings.txt').write_text(_settings(options),encoding='ascii')
        if options.native_camera:(native/'camera-input.bin').write_bytes(_camera_bytes(options.native_camera))
        if flying:
            (native/'flight.txt').write_text(flight_settings(flying,options.native_camera,options.width,options.height),encoding='ascii')
            shutil.copy2(native/'camera-input.bin',native/'initial-camera.bin')
        inputs={v.name:file_sha(v) for v in native.iterdir() if v.is_file()}
        _checkpoint(stage/'tin-scene-context.json',{'payload_sha256':sha(json_text(payload).encode()),'runtime':runtime,
            'inputs':inputs,'metrics':metrics,'input_readback':host,'texture':texture_plan,'line_overlay':line_plan,
            'texture_sha256':file_sha(stage/'texture.tif') if texture_plan else None})
        _invoke(bridge.root,native,'native-tin-scene.json')
        return _finish_scene(bridge,request_id)
    except Exception as error:fail_bundle(bridge,request_id,error);raise

def _finish_scene(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage']);native=stage/'native'
    context=json.loads((stage/'tin-scene-context.json').read_text(encoding='utf8'))
    if context['payload_sha256']!=sha(json_text(receipt['payload']).encode()):raise BridgeError('TIN_SCENE_CONTEXT_CHANGED')
    if _runtime(bridge.root)[2]!=context['runtime']:raise BridgeError('TIN_SCENE_RUNTIME_CHANGED')
    for name,wanted in context['inputs'].items():
        if file_sha(_source_member(native,name))!=wanted:raise BridgeError('TIN_SCENE_INPUT_CHANGED')
    if context.get('texture') and file_sha(stage/'texture.tif')!=context['texture_sha256']:raise BridgeError('TIN_SCENE_TEXTURE_CHANGED')
    for value in receipt['sources']:
        if file_sha(Path(value['file']))!=value['sha256']:raise BridgeError('STALE_SOURCE')
    complete=completed_native_child(native,'native-tin-scene.json')
    if complete is None:return receipt|{'native_child':'RUNNING','replayed':False}
    host,events=complete;members={}
    for value in events:
        if value.get('event')!='scene_file':continue
        name=value['name'];path=_source_member(native,name)
        if name in members or file_sha(path)!=value['sha256']:raise BridgeError('TIN_SCENE_CANDIDATE_CHANGED')
        members[name]=value['sha256']
    flying=TinFlightParameters.model_validate(receipt['payload']['flight']) if receipt['payload'].get('flight') else None
    if members.keys()!=({'model.tin','camera.bin','scene.tiles'}|({'texture.msi'} if context.get('texture') else set())|({'overlay.wl','lines.xyz'} if context.get('line_overlay') else set())|({'flight.avi','flight.pat'} if flying else set())):raise BridgeError('TIN_SCENE_NATIVE_INCOMPLETE')
    pose=_camera_read(native/'camera.bin');metrics=context['metrics']
    if pose.origin!=metrics['bounds'][:2]:raise BridgeError('TIN_SCENE_CAMERA_BASIS')
    records=[v for v in events if v.get('event')=='tin_scene_rendered'];options=TinSceneParameters.model_validate(receipt['payload']['parameters'])
    if (len(records)!=1 or records[0].get('window_visible') is not False or records[0].get('records_preserved') is not True
        or (records[0].get('width'),records[0].get('height'),records[0].get('points'),records[0].get('triangles'))
        !=(options.width,options.height,metrics['point_count'],metrics['triangle_count'])):raise BridgeError('TIN_SCENE_RENDER_READBACK')
    if pose.fov_degrees!=float(np.float32(options.fov_degrees)):raise BridgeError('TIN_SCENE_FOV_READBACK')
    texture_events=[v for v in events if v.get('event')=='tin_texture_bound']
    if context.get('texture'):
        if len(texture_events)!=1:raise BridgeError('TIN_SCENE_TEXTURE_READBACK')
        record=texture_events[0];w=record.get('width',0);h=record.get('height',0)
        if not 128<=w<=1024 or not 128<=h<=1024 or w&(w-1) or h&(h-1) or record.get('bytes')!=w*h*3 or record.get('kind')!=1:raise BridgeError('TIN_SCENE_TEXTURE_READBACK')
    elif texture_events:raise BridgeError('TIN_SCENE_TEXTURE_READBACK')
    line_events=[v for v in events if v.get('event')=='tin_lines_bound'];line_plan=context.get('line_overlay')
    if line_plan:
        if len(line_events)!=1:raise BridgeError('TIN_SCENE_LINE_READBACK')
        record=line_events[0]
        if (record.get('active'),record.get('record_upper_bound'),record.get('vertices'))!=(line_plan['feature_count'],line_plan['record_upper_bound'],line_plan['sampled_vertices']) or record.get('all_xyz_equal') is not True or record.get('graphic_parameters_equal') is not True or record.get('licensed_solver_called') is not False:raise BridgeError('TIN_SCENE_LINE_READBACK')
    elif line_events:raise BridgeError('TIN_SCENE_LINE_READBACK')
    flight_plan=None;flight_host=None
    if flying:
        flight_host,readback=_invoke(bridge.root,native,'tin-flight-readback-'+uuid.uuid4().hex[:10]+'.json',mode='inspect')
        if flight_host['pid']==host['pid']:raise BridgeError('TIN_FLIGHT_FRESH_READBACK')
        flight_plan=validate_flight(events,readback,flying,options.width,options.height)
        flight_plan['initial_camera']=options.native_camera.model_dump()
        last=flight_plan['frame_details'][-1]
        if not np.array_equal(_native_vector(last['eye_model']+last['target_model']),_native_vector(pose.eye_model+pose.target_model)):raise BridgeError('TIN_FLIGHT_FINAL_CAMERA')
    encoded=scene_images._encode_frame(bridge.root,stage,native/'scene.tiles')
    if encoded is None:return receipt|{'native_child':'ENCODING','replayed':False}
    encoded_file,encoder_host,reused=encoded
    files=stage/'files'
    if files.exists():files.rename(stage/('incomplete-files-'+uuid.uuid4().hex[:10]))
    files.mkdir()
    for name in ['model.tin','camera.bin']+(['model.tin.json'] if 'model.tin.json' in context['inputs'] else [])+(['texture.msi'] if context.get('texture') else [])+(['overlay.wl','lines.xyz'] if line_plan else []):shutil.copy2(native/name,files/name)
    if flying:
        for name in ['flight.avi','flight.pat','flight.txt','initial-camera.bin']:shutil.copy2(native/name,files/name)
    if context.get('texture'):
        shutil.copy2(stage/'texture.tif',files/'texture.tif')
        _texture_native_readback(bridge.root,stage/('texture-readback-'+uuid.uuid4().hex[:10]),files/'texture.msi',files/'texture.tif',context['texture'])
    shutil.copy2(encoded_file,files/'scene.msi')
    xyz,faces,edges,fresh_metrics,fresh=_read_native(bridge.root,files/'model.tin',stage/('reopen-'+uuid.uuid4().hex[:10]))
    if fresh['pid']==host['pid'] or fresh_metrics!=metrics:raise BridgeError('TIN_SCENE_FRESH_READBACK')
    if line_plan:
        blob,description=_line_transport(files/'overlay.wl',receipt['payload']['template']['crs'],metrics['bounds'],xyz,faces,edges,line_plan['height_offset_m'],line_plan.get('source_crs'))
        if blob!=(files/'lines.xyz').read_bytes() or description!=line_plan:raise BridgeError('TIN_SCENE_LINE_BINDING')
    executable,bas=backend_configuration(bridge.root);read=stage/('image-read-'+uuid.uuid4().hex[:10]);read.mkdir()
    raw=read/'scene.tiles';image_host,_=_native_image(executable,bas,read/'native','to_raw',files/'scene.msi',raw)
    profile={'transform':[1.,0.,0.,0.,1.,0.],'color_interpretation':['red','green','blue']}
    read_native_tiles(raw,files/'scene.tif',profile);read_native_tiles(native/'scene.tiles',read/'original-frame.tif',profile)
    with rasterio.open(files/'scene.tif') as ds:
        if (ds.width,ds.height,ds.count,ds.dtypes)!=(options.width,options.height,3,('uint8',)*3) or ds.crs or not np.all(ds.read_masks()==255):raise BridgeError('TIN_SCENE_PIXEL_PROFILE')
        pixels=ds.read()
    with rasterio.open(read/'original-frame.tif') as ds:
        if not np.array_equal(ds.read(),pixels):raise BridgeError('TIN_SCENE_ENCODER_PIXEL_MISMATCH')
    if flight_plan and sha(np.moveaxis(pixels,0,2).tobytes())!=flight_plan['frame_details'][-1]['rgb_sha256']:raise BridgeError('TIN_FLIGHT_POSTER_PIXELS')
    with rasterio.open(files/'scene.png','w',driver='PNG',width=options.width,height=options.height,count=3,dtype='uint8') as out:out.write(pixels)
    if any(file_sha(_source_member(native,name))!=wanted for name,wanted in members.items()):raise BridgeError('TIN_SCENE_NATIVE_CHANGED_DURING_READBACK')
    bound={v.name:file_sha(v) for v in files.iterdir() if v.is_file()};eye,target=_world_camera(pose)
    document={'format':FORMAT,'native_tin':'model.tin','frame':receipt['payload']['template']['crs'],'members':bound,
        'width':options.width,'height':options.height,'camera':pose.model_dump(),'metrics':metrics,
        'texture':context.get('texture'),
        'line_overlay':line_plan,
        'image_coordinates':'perspective pixels, not ground coordinates'}
    if flight_plan:document['flight']=flight_plan
    (files/Path(receipt['output']).name).write_text(json_text(document),encoding='utf8')
    metadata={'backend':'MapGIS6.7 original Tin3DView/C3DView/W60_Tin','native_execution':host,'native_reopen':fresh,
        'native_image_encode':encoder_host,'native_image_readback':image_host,'encoding_reused':reused,
        'native_members':{k:v for k,v in members.items() if k!='scene.tiles'},'frame_sha256':members['scene.tiles'],
        'camera':pose.model_dump(),'world_eye':eye,'world_target':target,'metrics':metrics,'render':records[0],
        'texture':context.get('texture'),'native_texture':texture_events[0] if texture_events else None,
        'line_overlay':line_plan,'native_lines':line_events[0] if line_events else None,
        'pixel_check':'EXACT original renderer frame vs fresh native MSI pixels','source_preserved':True,
        'mouse_or_keyboard_input':False,'replayed':False,'reconciled_after_interruption':not _producer_alive(stage),
        'scope':'Dense active original WTIN, metre XY/Z; exact local camera and complete topology records. SDK local display area preserves canonical TIN bytes. Optional full-coverage RGB2..1024px and unchanged native-metre WL. Original PAT interpolation/timer/rendering and Windows VFW uncompressed AVI for bounded TIN flights; camera paths are explicitly supplied world metres, speed is the native PAT parameter. No DEM conversion, proprietary scene format or full2D line-pattern claim.'}
    if flight_plan:metadata.update(flight=flight_plan,native_flight_readback=flight_host)
    return finish_bundle(bridge,request_id,files,metadata)

def reconcile_native_tin_scene(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if _producer_alive(stage):raise BridgeError('TIN_SCENE_PRODUCER_RUNNING')
    return _finish_scene(bridge,request_id)
