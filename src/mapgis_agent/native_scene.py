"""Original MapGIS DEM files and perspective MSI through the isolated x86 host."""
from __future__ import annotations
import json,math,os,re,shutil,struct,uuid
from pathlib import Path
from typing import Literal
import numpy as np
import psutil
import rasterio
from pydantic import BaseModel,ConfigDict,Field,StrictInt,model_validator
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle,_member_parts
from .core import Bridge,BridgeError,json_text,sha
from .grid_analysis import _linear_scale
from .grid_vectors import _grid_sources
from .native_backend import backend_configuration,_run_child,completed_native_child
from .native_paths import NativePathAlias
from .native_raster import _raster_sources,_snapshot_raster,_native_image
from .raster import file_sha
from .raster_tiles import read_native_tiles
from .visibility import _checkpoint,_producer_alive
from .scene_texture import TextureInput,prepare_texture,texture_info
from .scene_lines import LineOverlayInput,prepare_lines,line_overlay_info
from .scene_flight import FlightParameters,flight_settings,validate_flight
from .scene_camera import NativeCameraPose,camera_settings,camera_readback
from .scene_annotations import AnnotationOverlayInput,prepare_annotations,annotation_info,annotation_settings,validate_annotation_readback
from .scene_regions import RegionOverlayInput,prepare_regions,region_info,region_settings,validate_region_readback

FORMAT='mapgis-agent.native-scene.v1'
MODULES={'DemArea.dll':'384332e2db3690dff959e4b38e5f42fc4b8d683531c1f44570a913da6e6803c2',
    'mapdsp60.dll':'e1995ea477c7d87bd6ed75d7327376b9a5f9ed8d2564b0a42dbada9197b671cb',
    'NewTin3DFly.dll':'c4c1c342e0d45a2008cb01fe3b274fdab5cf393d63455ab5226d9c69c73e9340',
    'Dem3DDlg.dll':'9fd2f6cccb3c44623edf4c86d00bdbe53a4f7fca41c7cecd31ee8442761a29f4',
    'DemDlg.dll':'07c7038e6f6f0cfe416a9b44ca99c14bc46ea2d67646d89a15e2e7ab27eaa0c1',
    'TexDbs.dll':'b6b6fa0d701c9ff939343eaf87a3b7e7e188ec4626f89205c25248cc5c363c08'}

class SceneParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    vertical_unit:Literal['metre','foot','us_survey_foot']|None=None
    band:StrictInt=Field(default=1,ge=1)
    max_elevation_rounding_error_m:float=Field(default=0.,ge=0)
    width:StrictInt=Field(default=640,ge=32,le=2048)
    height:StrictInt=Field(default=480,ge=32,le=2048)
    mode:Literal['overview','walk']='overview'
    quality:float=Field(default=1.,ge=.01,le=1.,description='Native terrain sampling ratio; 1 keeps every elevation cell. Lower values must retain at least two rows and columns.')
    eye:list[float]|None=Field(default=None,min_length=3,max_length=3)
    target:list[float]|None=Field(default=None,min_length=3,max_length=3)
    native_camera:NativeCameraPose|None=None

    @model_validator(mode='after')
    def camera_pair(self):
        if self.native_camera is not None and (self.eye is not None or self.target is not None or self.mode!='walk'):
            raise ValueError('saved native camera requires walk mode and replaces eye/target')
        if (self.eye is None)!=(self.target is None):raise ValueError('eye and target must be supplied together')
        if self.eye is not None:
            values=np.asarray([self.eye,self.target],dtype='float64')
            if not np.isfinite(values.astype('float32')).all() or self.eye[:2]==self.target[:2]:
                raise ValueError('finite float32 camera positions with distinct XY are required by the native camera')
        return self

def _surface(file,options,frame):
    if options.vertical_unit is None:raise BridgeError('SCENE_VERTICAL_UNIT_REQUIRED')
    if not isinstance(frame,dict):raise BridgeError('SCENE_EXPLICIT_FRAME_OBJECT_REQUIRED')
    crs,scale=_linear_scale(frame,options.vertical_unit)
    horizontal=1. if crs is None and frame['unit']=='metre' else crs.axis_info[0].unit_conversion_factor if crs else 0.
    if not math.isclose(horizontal,1.,rel_tol=1e-12):raise BridgeError('SCENE_PROJECTED_METRE_FRAME')
    with rasterio.open(file) as ds:
        if ds.crs and (crs is None or not crs.equals(ds.crs)):raise BridgeError('SCENE_CRS_MISMATCH')
        if not 1<=options.band<=ds.count:raise BridgeError('SCENE_BAND')
        if ds.width<2 or ds.height<2 or max(ds.width,ds.height)>16384 or ds.width*ds.height>2_000_000:
            raise BridgeError('SCENE_GRID_BOUND:2..16384 rows/columns and at most2M elevation cells')
        t=ds.transform
        if any(not math.isfinite(v) for v in t) or t.a<=0 or t.e>=0 or t.b!=0 or t.d!=0 or ds.gcps[0]:
            raise BridgeError('SCENE_NORTH_UP_AFFINE_REQUIRED')
        data=ds.read(options.band,masked=True)
        if np.ma.getmaskarray(data).any() or not np.isfinite(data.data).all():
            raise BridgeError('SCENE_MISSING_ELEVATION: rectify/fill missing cells explicitly before native rendering')
        band_scale,offset=ds.scales[options.band-1],ds.offsets[options.band-1]
        if not math.isfinite(band_scale) or not math.isfinite(offset):raise BridgeError('SCENE_CALIBRATION')
        calibrated=(data.data.astype('float64')*band_scale+offset)/scale
        with np.errstate(over='ignore',invalid='ignore'):native=calibrated.astype('<f4')
        if not np.isfinite(native).all() or np.any(np.abs(native)>=1e18):raise BridgeError('SCENE_NATIVE_ELEVATION_RANGE')
        error=float(np.max(np.abs(native.astype('float64')-calibrated)))
        if error>options.max_elevation_rounding_error_m:raise BridgeError('SCENE_FLOAT32_ROUNDING: explicitly allow the measured elevation error in metres')
        bounds=[t.c+.5*t.a,t.f+(ds.height-.5)*t.e,t.c+(ds.width-.5)*t.a,t.f+.5*t.e]
        grid={'rows':ds.height,'columns':ds.width,'bounds':bounds,'transform':list(t)[:6],
            'crs_wkt':crs.to_wkt() if crs else None,'range_m':[float(native.min()),float(native.max())],
            'source_range_m':[float(calibrated.min()),float(calibrated.max())],
            'source_scale':band_scale,'source_offset':offset,'rounding_error_m':error,'row_order':'south_to_north'}
    return np.flipud(native).copy(),grid

def _transport(values,grid):
    return struct.pack('<ii4d',grid['columns'],grid['rows'],*grid['bounds'])+values.astype('<f4',copy=False).tobytes()

def _runtime(root):
    executable,bas=backend_configuration(root);program=bas.parent
    files={str(executable):file_sha(executable),str(bas):file_sha(bas)}
    for name,wanted in MODULES.items():
        path=program/name
        if file_sha(path)!=wanted:raise BridgeError('SCENE_NATIVE_BUILD_MISMATCH')
        files[str(path)]=wanted
    for name in ['basroot.dll','msibase.dll','HeightLut/Terrain.lut','HeightLut/Newgrey.lut']:
        path=program/name;files[str(path)]=file_sha(path)
    return executable,program,files

def _source_member(parent,name):
    path=parent
    for part in _member_parts(name):
        path=path/part
        if path.is_symlink() or path.is_junction():raise BridgeError('SCENE_MEMBER_REPARSE_POINT')
    if not path.resolve(strict=True).is_relative_to(parent.resolve()) or not path.is_file():raise BridgeError('SCENE_MEMBER_PATH')
    return path

def _dem_dependencies(folder,members):
    """Validate native file references before the original parser opens them."""
    blob=(folder/'terrain.dem').read_bytes()
    if len(blob)<861 or blob[:9]!=b'DEM_INFO\0' or struct.unpack_from('<I',blob,9)[0]!=17504:
        raise BridgeError('SCENE_DEM_HEADER')
    if struct.unpack_from('<h',blob,14)[0]!=7 or struct.unpack_from('<h',blob,66)[0]!=1:
        raise BridgeError('SCENE_DEM_PROFILE:one native float32 pyramid required')
    directory=blob[84:84+256].split(b'\0',1)[0].decode('gbk',errors='strict').replace('\\','/').rstrip('/')
    if directory!='terrain':raise BridgeError('SCENE_DEM_DIRECTORY')
    paths=[]
    for offset in (84+256,84+516):
        field=blob[offset:offset+260]
        if b'\0' not in field:raise BridgeError('SCENE_DEM_PATH_TERMINATOR')
        raw=field.split(b'\0',1)[0].decode('gbk',errors='strict').replace('\\','/')
        if raw not in members:raise BridgeError('SCENE_UNBOUND_DEM_DEPENDENCY')
        paths.append(_source_member(folder,raw))
    header=paths[0].read_bytes()
    if len(header)<43:raise BridgeError('SCENE_DEM_PYRAMID_HEADER')
    _,dx,dy,rows,columns,tr,tc,br,bc=struct.unpack_from('<Bddii hhii',header)
    if (not math.isfinite(dx) or not math.isfinite(dy) or min(dx,dy)<=0 or min(rows,columns)<2
            or max(rows,columns)>16384 or rows*columns>2_000_000 or not 2<=tr<=1024 or not 2<=tc<=1024
            or tr&(tr-1) or tc&(tc-1) or br!=(rows-1)//(tr-1)+1 or bc!=(columns-1)//(tc-1)+1):
        raise BridgeError('SCENE_DEM_GRID_BOUND')
    pages=struct.unpack_from('<h',header,37)[0]
    if pages!=(br*bc+4095)//4096 or len(header)!=39+4*pages+17*br*bc:raise BridgeError('SCENE_DEM_PYRAMID_SIZE')

def _scene_source(file,expected_sha256):
    if file_sha(file)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    record=json.loads(file.read_text(encoding='utf8'))
    if record.get('format')!=FORMAT or record.get('native_dem')!='terrain.dem' or record.get('elevation')!='elevation.tif':
        raise BridgeError('SCENE_DOCUMENT_FORMAT')
    members=record.get('members')
    if not isinstance(members,dict) or not {'terrain.dem','elevation.tif'}<=set(members):raise BridgeError('SCENE_MEMBERS')
    native_members=record.get('native_members')
    if (not isinstance(native_members,dict) or 'terrain.dem' not in native_members
            or any((name not in {'terrain.dem','texture.tdb','texture.msi','overlay.wl','notes.wt','regions.wp','regions.mesh'} and not name.startswith(('terrain/','texture_DAT/')))
                   or members.get(name)!=wanted for name,wanted in native_members.items())):
        raise BridgeError('SCENE_NATIVE_MEMBERS')
    sources=[{'file':str(file),'sha256':expected_sha256}]
    for name,wanted in members.items():
        path=_source_member(file.parent,name)
        if file_sha(path)!=wanted:raise BridgeError('SCENE_MEMBER_CHANGED')
        sources.append({'file':str(path),'sha256':wanted})
    _dem_dependencies(file.parent,native_members)
    native_texture=texture_info(file.parent,native_members)
    if native_texture:
        description=record.get('texture')
        if (not isinstance(description,dict) or description.get('native')!='texture.tdb' or description.get('source')!='texture.tif'
                or 'texture.tif' not in members):raise BridgeError('SCENE_TEXTURE_BINDING')
    elif record.get('texture') is not None:raise BridgeError('SCENE_TEXTURE_BINDING')
    description=record.get('line_overlay')
    line_profile=line_overlay_info(file.parent,native_members,record['frame'],record['grid'],description.get('source_crs') if isinstance(description,dict) else None)
    if description!=line_profile:raise BridgeError('SCENE_LINE_BINDING')
    description=record.get('annotations')
    if description!=annotation_info(file.parent,native_members,record['frame'],record['grid'],description):raise BridgeError('SCENE_ANNOTATION_BINDING')
    description=record.get('region_overlay')
    if description!=region_info(file.parent,native_members,record['frame'],record['grid'],description):raise BridgeError('SCENE_REGION_BINDING')
    if record.get('final_camera') is not None:NativeCameraPose.model_validate(record['final_camera'])
    return record,sources

def _settings(options):
    eye=options.eye or [0.,0.,0.];target=options.target or [0.,0.,0.]
    return ' '.join(map(str,[options.width,options.height,int(options.mode=='walk'),int(options.eye is not None),options.quality,*eye,*target]))+'\n'

def _invoke(root,folder,mode,log):
    executable,program,_=_runtime(root)
    with NativePathAlias(folder) as alias:
        try:
            return _run_child([str(executable),'scene',mode,str(program),str(alias.path/'input.bin'),str(alias.path),str(alias.path/'settings.txt')],
                folder,log,mfc_windows=True)
        except BridgeError as error:
            diagnostic=folder/log
            if str(error).startswith('NATIVE_CHILD_FAILED:') and diagnostic.is_file():
                result=json.loads(diagnostic.read_text(encoding='utf8'))
                reasons={'scene annotation font unavailable; choose an installed GB2312 font':'SCENE_ANNOTATION_FONT_UNAVAILABLE',
                    'scene annotation font missing glyph':'SCENE_ANNOTATION_FONT_MISSING_GLYPH',
                    'scene annotation exceeds original glyph atlas; split the label':'SCENE_ANNOTATION_ATLAS_OVERFLOW',
                    'scene region palette colour unavailable':'SCENE_REGION_PALETTE_UNAVAILABLE'}
                code=reasons.get(result.get('stderr','').strip())
                if code and result.get('returncode')==1 and not result.get('timed_out'):
                    raise BridgeError(f'{code}: {result["stderr"].strip()}; diagnostic {diagnostic}') from error
            raise

def _encode_frame(root,stage,frame):
    """Adopt completed native encoding separately from the original 3D draw."""
    marker=stage/'image-encode.json';digest=file_sha(frame)
    if marker.exists():
        record=json.loads(marker.read_text(encoding='utf8'))
        if record['frame_sha256']!=digest or not re.fullmatch(r'image-encode-[0-9a-f]{12}',record['attempt']):
            raise BridgeError('SCENE_ENCODER_CONTEXT_CHANGED')
        attempt=stage/record['attempt']
        try:completed=completed_native_child(attempt/'native','image.json')
        except FileNotFoundError:completed='incomplete'
        except BridgeError as error:
            if not str(error).startswith(('NATIVE_CHILD_FAILED:','NATIVE_CANDIDATE_INCOMPLETE:')):raise
            completed='incomplete'
        if completed is None:return None
        if completed!='incomplete':
            host,events=completed
            if not any(v.get('event')=='image_saved' and v.get('native_result')==0 for v in events):raise BridgeError('SCENE_ENCODER_INCOMPLETE')
            return attempt/'scene.msi',host,True
    # This retry only encodes an immutable completed frame into a fresh file;
    # it never repeats terrain creation, texture mutation or original rendering.
    attempt=stage/('image-encode-'+uuid.uuid4().hex[:12]);attempt.mkdir()
    _checkpoint(marker,{'frame_sha256':digest,'attempt':attempt.name})
    executable,bas=backend_configuration(root)
    host,_=_native_image(executable,bas,attempt/'native','from_raw',frame,attempt/'scene.msi')
    return attempt/'scene.msi',host,False

def prepare_native_scene(bridge:Bridge,operation,file,expected_sha256,parameters,template,request_id,expected_members=None,texture=None,remove_texture=False,line_overlay=None,remove_line_overlay=False,flight=None,annotations=None,remove_annotations=False,region_overlay=None,remove_region_overlay=False):
    if operation not in {'create','render','flight'}:raise BridgeError('SCENE_OPERATION')
    if isinstance(parameters,SceneParameters):parameters=parameters.model_dump(exclude_unset=True)
    image=TextureInput.model_validate(texture) if texture is not None else None
    lines=LineOverlayInput.model_validate(line_overlay) if line_overlay is not None else None
    notes=AnnotationOverlayInput.model_validate(annotations) if annotations is not None else None
    regions=RegionOverlayInput.model_validate(region_overlay) if region_overlay is not None else None
    flying=FlightParameters.model_validate(flight) if flight is not None else None
    if (operation=='flight')!=(flying is not None):raise BridgeError('SCENE_FLIGHT_OPTIONS: operation=flight needs flight options and an existing saved scene')
    if (image and remove_texture) or (remove_texture and operation=='create'):raise BridgeError('SCENE_TEXTURE_OPTIONS')
    if (lines and remove_line_overlay) or (remove_line_overlay and operation=='create'):raise BridgeError('SCENE_LINE_OPTIONS')
    if (notes and remove_annotations) or (remove_annotations and operation=='create'):raise BridgeError('SCENE_ANNOTATION_OPTIONS')
    if (regions and remove_region_overlay) or (remove_region_overlay and operation=='create'):raise BridgeError('SCENE_REGION_OPTIONS')
    if set(template)!={'name','crs'} or not isinstance(template['name'],str) or not template['name'].endswith('.scene.json'):
        raise BridgeError('SCENE_TEMPLATE:name.scene.json and explicit source crs required')
    source=Path(file).resolve(strict=True);document=None
    if operation=='create':
        options=SceneParameters.model_validate(parameters)
        if options.vertical_unit is None:raise BridgeError('SCENE_VERTICAL_UNIT_REQUIRED')
        sources=_raster_sources(source,expected_sha256,expected_members)
        if source.suffix.lower()=='.grd':sources=list({v['file']:v for v in sources+_grid_sources(source,expected_sha256,template['crs'])}.values())
    else:
        if {'vertical_unit','band','max_elevation_rounding_error_m'} & parameters.keys():
            raise BridgeError('SCENE_RENDER_CALIBRATION:existing canonical elevations are already metres')
        document,sources=_scene_source(source,expected_sha256)
        if not isinstance(document.get('parameters'),dict):raise BridgeError('SCENE_DOCUMENT_PARAMETERS')
        if flying and parameters.get('mode','walk')!='walk':raise BridgeError('SCENE_FLIGHT_WALK_MODE')
        saved=dict(document['parameters'])
        explicit_camera=bool({'eye','target','native_camera'} & parameters.keys())
        if explicit_camera or parameters.get('mode')=='overview':saved.pop('native_camera',None)
        if document.get('flight') and not flying and not explicit_camera and parameters.get('mode')!='overview':
            if document.get('final_camera') is None:
                raise BridgeError('SCENE_FINAL_CAMERA_MISSING: rerecord flight.input once to capture its final pose, or explicitly choose another view')
            saved.update(native_camera=document['final_camera'],eye=None,target=None,mode='walk')
        options=SceneParameters.model_validate(saved|({'mode':'walk'} if flying else {})|parameters)
        if json_text(document['frame'])!=json_text(template['crs']):raise BridgeError('SCENE_FRAME_CHANGED')
        if expected_members is not None and expected_members!=document['members']:raise BridgeError('SCENE_MEMBERS')
    surface_sources=list(sources);image_sources=[]
    if image:
        image=image.model_copy(update={'file':str(Path(image.file).resolve(strict=True))})
        image_sources=_raster_sources(Path(image.file),image.expected_sha256,image.expected_members)
        known={v['file']:v['sha256'] for v in sources}
        for item in image_sources:
            if item['file'] in known:
                if item['sha256']!=known[item['file']]:raise BridgeError('STALE_SOURCE')
            else:sources.append(item);known[item['file']]=item['sha256']
    if lines:
        lines=lines.model_copy(update={'file':str(Path(lines.file).resolve(strict=True))})
        if file_sha(Path(lines.file))!=lines.expected_sha256:raise BridgeError('STALE_SOURCE')
        known={v['file']:v['sha256'] for v in sources}
        if lines.file in known and known[lines.file]!=lines.expected_sha256:raise BridgeError('STALE_SOURCE')
        if lines.file not in known:sources.append({'file':lines.file,'sha256':lines.expected_sha256})
    if notes:
        notes=notes.model_copy(update={'file':str(Path(notes.file).resolve(strict=True))})
        if file_sha(Path(notes.file))!=notes.expected_sha256:raise BridgeError('STALE_SOURCE')
        known={v['file']:v['sha256'] for v in sources}
        if notes.file in known and known[notes.file]!=notes.expected_sha256:raise BridgeError('STALE_SOURCE')
        if notes.file not in known:sources.append({'file':notes.file,'sha256':notes.expected_sha256})
    if regions:
        regions=regions.model_copy(update={'file':str(Path(regions.file).resolve(strict=True))})
        if file_sha(Path(regions.file))!=regions.expected_sha256:raise BridgeError('STALE_SOURCE')
        known={v['file']:v['sha256'] for v in sources}
        if regions.file in known and known[regions.file]!=regions.expected_sha256:raise BridgeError('STALE_SOURCE')
        if regions.file not in known:sources.append({'file':regions.file,'sha256':regions.expected_sha256})
    executable,program,runtime=_runtime(bridge.root)
    payload={'operation':'native.scene.'+operation,'file':str(source),'expected_sha256':expected_sha256,
        'parameters':options.model_dump(exclude_unset=True),'template':template,'expected_members':expected_members,'scene_protocol':9,
        'texture':image.model_dump() if image else None,'remove_texture':remove_texture,
        'line_overlay':lines.model_dump() if lines else None,'remove_line_overlay':remove_line_overlay,'flight':flying.model_dump() if flying else None,
        'annotations':notes.model_dump() if notes else None,'remove_annotations':remove_annotations,
        'region_overlay':regions.model_dump() if regions else None,'remove_region_overlay':remove_region_overlay}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        owner=psutil.Process();_checkpoint(stage/'producer.json',{'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()})
        native=stage/'native';native.mkdir()
        inherited=bool(document and document.get('texture') and image is None and not remove_texture)
        inherited_lines=bool(document and document.get('line_overlay') and lines is None and not remove_line_overlay)
        inherited_notes=bool(document and document.get('annotations') and notes is None and not remove_annotations)
        inherited_regions=bool(document and document.get('region_overlay') and regions is None and not remove_region_overlay)
        texture_plan=None
        line_plan=None
        note_plan=None
        region_plan=None
        if operation=='create':
            snapshot=_snapshot_raster(source,surface_sources,stage/'input');values,grid=_surface(snapshot,options,template['crs'])
            profile={'driver':'GTiff','width':grid['columns'],'height':grid['rows'],'count':1,'dtype':'float32',
                'transform':rasterio.Affine(*grid['transform']),'compress':'deflate'}
            if grid['crs_wkt']:profile['crs']=grid['crs_wkt']
            with rasterio.open(stage/'elevation.tif','w',**profile) as ds:ds.write(np.flipud(values),1)
        else:
            shutil.copy2(source.parent/'elevation.tif',stage/'elevation.tif')
            values,grid=_surface(stage/'elevation.tif',SceneParameters(vertical_unit='metre'),template['crs'])
            for name in document['native_members']:
                if not inherited and name.startswith('texture'):continue
                if name=='overlay.wl' and not inherited_lines:continue
                if name=='notes.wt' and not inherited_notes:continue
                if name in {'regions.wp','regions.mesh'} and not inherited_regions:continue
                origin=_source_member(source.parent,name);target=native.joinpath(*_member_parts(name));target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(origin,target)
        if min(grid['rows'],grid['columns'])*float(np.float32(options.quality))<2:
            raise BridgeError('SCENE_DRAW_RESOLUTION: sampling collapses the surface; use quality=1 or a ratio retaining at least two rows and columns')
        if lines:line_plan=prepare_lines(stage,lines,template['crs'],grid)
        elif inherited_lines:
            line_plan=line_overlay_info(native,document['native_members'],template['crs'],grid,document['line_overlay'].get('source_crs'))
            if line_plan!=document['line_overlay']:raise BridgeError('SCENE_LINE_BINDING')
        if notes:note_plan=prepare_annotations(stage,notes,template['crs'],grid)
        elif inherited_notes:
            note_plan=annotation_info(native,document['native_members'],template['crs'],grid,document['annotations'])
            if note_plan!=document['annotations']:raise BridgeError('SCENE_ANNOTATION_BINDING')
        if regions:region_plan=prepare_regions(stage,regions,template['crs'],grid)
        elif inherited_regions:
            region_plan=region_info(native,document['native_members'],template['crs'],grid,document['region_overlay'])
            if region_plan!=document['region_overlay']:raise BridgeError('SCENE_REGION_BINDING')
        if image:texture_plan=prepare_texture(bridge.root,stage,image,image_sources,template['crs'],grid)
        elif inherited:
            shutil.copy2(source.parent/'texture.tif',stage/'texture.tif');texture_plan=document['texture']|{'import_reused':True}
            texture_info(native,document['native_members'],grid)
        (native/'input.bin').write_bytes(_transport(values,grid));(native/'settings.txt').write_text(_settings(options),encoding='ascii')
        input_names=['elevation.tif','native/input.bin','native/settings.txt']
        if options.native_camera:
            (native/'camera.txt').write_text(camera_settings(options.native_camera),encoding='ascii')
            input_names.append('native/camera.txt')
        if flying:
            (native/'flight.txt').write_text(flight_settings(flying,grid,options.width,options.height),encoding='ascii')
            input_names.append('native/flight.txt')
        if line_plan:input_names.append('native/overlay.wl')
        if note_plan:
            (native/'annotations.txt').write_text(annotation_settings(note_plan),encoding='ascii')
            input_names.extend(['native/notes.wt','native/annotations.txt'])
        if region_plan:
            (native/'regions.txt').write_text(region_settings(region_plan),encoding='ascii')
            input_names.extend(['native/regions.wp','native/regions.mesh','native/regions.txt'])
        if texture_plan:
            input_names.append('texture.tif')
            if (native/'texture.msi').is_file():input_names.append('native/texture.msi')
        _checkpoint(stage/'scene-context.json',{'payload_sha256':sha(json_text(payload).encode('utf8')),'grid':grid,'runtime':runtime,
            'texture':texture_plan,'line_overlay':line_plan,'annotations':note_plan,'region_overlay':region_plan,'inputs':{name:file_sha(stage/name) for name in input_names}})
        _invoke(bridge.root,native,operation,'native-scene.json')
        return _finish_scene(bridge,request_id)
    except Exception as error:fail_bundle(bridge,request_id,error);raise

def _finish_scene(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage']);native=stage/'native'
    context=json.loads((stage/'scene-context.json').read_text(encoding='utf8'))
    if context['payload_sha256']!=sha(json_text(receipt['payload']).encode('utf8')):raise BridgeError('SCENE_CONTEXT_CHANGED')
    _,_,runtime=_runtime(bridge.root)
    if runtime!=context['runtime']:raise BridgeError('SCENE_RUNTIME_CHANGED')
    for name,wanted in context['inputs'].items():
        if file_sha(stage/name)!=wanted:raise BridgeError('SCENE_INPUT_CHANGED')
    for source in receipt['sources']:
        if file_sha(Path(source['file']))!=source['sha256']:raise BridgeError('STALE_SOURCE')
    complete=completed_native_child(native,'native-scene.json')
    if complete is None:return receipt|{'native_child':'RUNNING','replayed':False}
    host,events=complete;members={}
    for value in events:
        if value.get('event')!='scene_file':continue
        name=value['name'];path=_source_member(native,name)
        if name in members or file_sha(path)!=value['sha256']:raise BridgeError('SCENE_CANDIDATE_CHANGED')
        members[name]=value['sha256']
    if not {'scene.tiles','terrain.dem'}<=set(members):raise BridgeError('SCENE_NATIVE_INCOMPLETE')
    flying=FlightParameters.model_validate(receipt['payload']['flight']) if receipt['payload'].get('flight') else None
    if bool('flight.avi' in members)!=bool(flying):raise BridgeError('SCENE_FLIGHT_EXECUTION')
    dem_members={name:wanted for name,wanted in members.items() if name not in {'scene.tiles','flight.avi'}};_dem_dependencies(native,dem_members)
    texture_profile=texture_info(native,dem_members,context['grid'])
    line_plan=context.get('line_overlay')
    line_profile=line_overlay_info(native,dem_members,receipt['payload']['template']['crs'],context['grid'],line_plan.get('source_crs') if line_plan else None)
    if line_profile!=line_plan:raise BridgeError('SCENE_LINE_EXECUTION')
    note_plan=context.get('annotations')
    note_profile=annotation_info(native,dem_members,receipt['payload']['template']['crs'],context['grid'],note_plan)
    if note_profile!=note_plan:raise BridgeError('SCENE_ANNOTATION_EXECUTION')
    native_notes=validate_annotation_readback(events,note_profile,bound=True)
    region_plan=context.get('region_overlay')
    region_profile=region_info(native,dem_members,receipt['payload']['template']['crs'],context['grid'],region_plan)
    if region_profile!=region_plan:raise BridgeError('SCENE_REGION_EXECUTION')
    native_regions=validate_region_readback(events,region_profile,bound=True)
    if line_profile:
        bound=[v for v in events if v.get('event')=='scene_lines_bound']
        inspected=[v for v in events if v.get('event')=='scene_lines_readback']
        if len(bound)!=1 or len(inspected)!=1 or bound[0].get('flags')!=32768 or bound[0].get('reduce_tolerance')!=0:
            raise BridgeError('SCENE_LINE_READBACK')
        for item in bound+inspected:
            if (item.get('active'),item.get('record_upper_bound'))!=(line_profile['feature_count'],line_profile['record_upper_bound']):raise BridgeError('SCENE_LINE_READBACK')
    if bool(texture_profile)!=bool(context.get('texture')):raise BridgeError('SCENE_TEXTURE_EXECUTION')
    if texture_profile:
        texture_events=[v for v in events if v.get('event')=='scene_texture_bound']
        if len(texture_events)!=1:raise BridgeError('SCENE_TEXTURE_READBACK')
        bound=texture_events[0]
        if ((bound['width'],bound['height'],bound['tiles'])!=(texture_profile['tile_width'],texture_profile['tile_height'],texture_profile['tiles'])
                or not 1<=bound['cache_capacity']<=texture_profile['tiles'] or bound['cache_bytes']!=bound['cache_capacity']*(texture_profile['tile_bytes']+4)
                or bound['cache_bytes']>8*1024*1024):raise BridgeError('SCENE_TEXTURE_READBACK')
    render=next(v for v in events if v.get('event')=='scene_rendered');grid=context['grid'];options=SceneParameters.model_validate(receipt['payload']['parameters'])
    readbacks=[v for v in events if v.get('event')=='scene_dem_readback']
    if (len(readbacks)!=1 or (readbacks[0]['rows'],readbacks[0]['columns'],readbacks[0]['cells'])!=(grid['rows'],grid['columns'],grid['rows']*grid['columns'])
            or readbacks[0]['all_cells_equal'] is not True or readbacks[0]['range']!=grid['range_m']):raise BridgeError('SCENE_NATIVE_GRID_READBACK')
    if any(not math.isfinite(a) or abs(a-b)>max(1e-9,16*math.ulp(max(abs(a),abs(b),1.)))
            for a,b in zip(readbacks[0]['bounds'],grid['bounds'])):raise BridgeError('SCENE_NATIVE_BOUNDS_READBACK')
    if (render['width'],render['height'],render['mode'])!=(options.width,options.height,int(options.mode=='walk')) or render['window_visible'] is not False:
        raise BridgeError('SCENE_RENDER_READBACK')
    if not math.isclose(render['sampling_ratio'],options.quality,rel_tol=1e-6):raise BridgeError('SCENE_SAMPLING_READBACK')
    encoded=_encode_frame(bridge.root,stage,native/'scene.tiles')
    if encoded is None:return receipt|{'native_child':'ENCODING','replayed':False}
    encoded_file,encoder_host,encoding_reused=encoded
    files=stage/'files'
    if files.exists():
        prior=bridge.destination_path(stage/('incomplete-files-'+uuid.uuid4().hex[:10]))
        if not files.resolve().is_relative_to(stage.resolve()):raise BridgeError('SCENE_STAGE_IDENTITY')
        files.rename(prior)
    files.mkdir()
    for name in dem_members:
        target=files.joinpath(*_member_parts(name));target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(native.joinpath(*_member_parts(name)),target)
    shutil.copy2(encoded_file,files/'scene.msi')
    if flying:shutil.copy2(native/'flight.avi',files/'flight.avi')
    shutil.copy2(stage/'elevation.tif',files/'elevation.tif')
    if texture_profile:shutil.copy2(stage/'texture.tif',files/'texture.tif')
    reopen=stage/('reopen-'+uuid.uuid4().hex[:10]);reopen.mkdir()
    for name in dem_members:
        target=reopen.joinpath(*_member_parts(name));target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(files.joinpath(*_member_parts(name)),target)
    shutil.copy2(native/'input.bin',reopen/'input.bin');shutil.copy2(native/'settings.txt',reopen/'settings.txt')
    if flying:shutil.copy2(files/'flight.avi',reopen/'flight.avi')
    fresh,readback=_invoke(bridge.root,reopen,'inspect','native-inspect.json')
    validate_annotation_readback(readback,note_profile)
    validate_region_readback(readback,region_profile)
    readback_members={v['name']:v['sha256'] for v in readback if v.get('event')=='scene_file'}
    if fresh['pid']==host['pid'] or readback_members!=(dem_members|({'flight.avi':members['flight.avi']} if flying else {})):
        raise BridgeError('SCENE_FRESH_DEM_READBACK')
    flight_profile=validate_flight(events,readback,flying,options.width,options.height) if flying else None
    final_camera=None
    if flying:
        final_camera=camera_readback(events,'flight_final_camera')
        if render['eye']!=flight_profile['frame_details'][-1]['eye']:raise BridgeError('SCENE_FINAL_CAMERA_EYE')
    elif options.native_camera:
        final_camera=camera_readback(events,'scene_camera')
        expected=options.native_camera.model_dump()
        for key in expected:
            if key=='profile':continue
            if not np.array_equal(np.asarray(final_camera[key],dtype='float32'),np.asarray(expected[key],dtype='float32')):
                raise BridgeError('SCENE_CAMERA_RESTORE_READBACK')
    fresh_lines=[v for v in readback if v.get('event')=='scene_lines_readback']
    if line_profile and (len(fresh_lines)!=1 or (fresh_lines[0].get('active'),fresh_lines[0].get('record_upper_bound'))!=(line_profile['feature_count'],line_profile['record_upper_bound'])):
        raise BridgeError('SCENE_FRESH_LINE_READBACK')
    executable,bas=backend_configuration(bridge.root);image_read=stage/('image-read-'+uuid.uuid4().hex[:10]);image_read.mkdir()
    raw=image_read/'scene.tiles';image_host,image_events=_native_image(executable,bas,image_read/'native','to_raw',files/'scene.msi',raw)
    read_native_tiles(raw,files/'scene.tif',{'transform':[1.,0.,0.,0.,1.,0.],'color_interpretation':['red','green','blue']})
    read_native_tiles(native/'scene.tiles',image_read/'original-frame.tif',{'transform':[1.,0.,0.,0.,1.,0.],'color_interpretation':['red','green','blue']})
    with rasterio.open(files/'scene.tif') as ds:
        if (ds.width,ds.height,ds.count,ds.dtypes)!=(options.width,options.height,3,('uint8',)*3) or ds.crs or not np.all(ds.read_masks()==255):
            raise BridgeError('SCENE_PIXEL_PROFILE')
        pixels=ds.read();colors=int(np.unique(pixels.reshape(3,-1).T,axis=0).shape[0])
    with rasterio.open(image_read/'original-frame.tif') as source:
        if not np.array_equal(source.read(),pixels):raise BridgeError('SCENE_NATIVE_ENCODER_PIXEL_MISMATCH')
    with rasterio.open(files/'scene.png','w',driver='PNG',width=options.width,height=options.height,count=3,dtype='uint8') as png:png.write(pixels)
    if any(file_sha(_source_member(native,name))!=wanted for name,wanted in members.items()):raise BridgeError('SCENE_NATIVE_CHANGED_DURING_READBACK')
    bound={path.relative_to(files).as_posix():file_sha(path) for path in files.rglob('*') if path.is_file()}
    metadata={'backend':'MapGIS6.7 DemArea + NewTin3DFly original terrain renderer','native_execution':host,
        'native_reopen':fresh,'native_image_readback':image_host,'native_dem_readback':readbacks[0],'grid':grid,'render':render,'unique_rgb_colors':colors,
        'native_image_encode':encoder_host,'encoding_reused':encoding_reused,'frame_sha256':members['scene.tiles'],'pixel_check':'EXACT original renderer frame vs fresh native MSI pixels',
        'native_members':dem_members,'source_preserved':True,'mouse_or_keyboard_input':False,'replayed':False,
        'texture':context.get('texture'),'native_texture':texture_profile,'line_overlay':line_profile,'annotations':note_profile,'native_annotations':native_notes,
        'region_overlay':region_profile,'native_regions':native_regions,'flight':flight_profile,'final_camera':final_camera,
        'reconciled_after_interruption':_producer_alive(stage) is False,
        'scope':'Editable original DEM header/pyramid/data files; perspective MSI/TIFF/PNG; no terrain CRS on perspective pixels. Scene JSON binds original native data and camera settings; it is an agent manifest, not a proprietary native3D scene format. Original renderer may clamp camera positions and uses its native display scaling.'}
    document={'format':FORMAT,'frame':receipt['payload']['template']['crs'],'native_dem':'terrain.dem','elevation':'elevation.tif',
        'native_members':dem_members,'members':bound,'parameters':options.model_dump(exclude={'vertical_unit','band','max_elevation_rounding_error_m'}),'grid':grid,'render':render,
        'image_coordinates':'perspective pixel columns/rows; not ground coordinates','native_data':'MapGIS DEM_INFO version17504; float32, one pyramid'}
    document['texture']=({k:v for k,v in context['texture'].items() if k not in {'native_import','native_pixel_readback','import_reused'}} if texture_profile else None)
    document['line_overlay']=line_profile
    document['annotations']=note_profile
    document['region_overlay']=region_profile
    document['flight']=flight_profile
    document['final_camera']=final_camera
    (files/Path(receipt['output']).name).write_text(json_text(document),encoding='utf8')
    return finish_bundle(bridge,request_id,files,metadata)

def reconcile_native_scene(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if _producer_alive(stage):raise BridgeError('SCENE_PRODUCER_RUNNING')
    return _finish_scene(bridge,request_id)
