"""Registered RGB input and the bounded original TEX_INFO profile used by scenes."""
from __future__ import annotations
import math,struct
from pathlib import Path
import numpy as np
import rasterio
from pydantic import BaseModel,ConfigDict,Field
from .core import BridgeError
from .grid_analysis import _linear_scale
from .native_backend import backend_configuration
from .native_raster import _snapshot_raster,_native_image
from .raster_tiles import write_native_tiles,read_native_tiles

class TextureInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    file:str
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    expected_members:dict[str,str]|None=None

def _texture_pixels(source,frame,grid):
    crs,_=_linear_scale(frame,'metre')
    with rasterio.open(source) as ds:
        if ds.count!=3 or ds.dtypes!=('uint8',)*3 or ds.colorinterp!=(rasterio.enums.ColorInterp.red,rasterio.enums.ColorInterp.green,rasterio.enums.ColorInterp.blue):
            raise BridgeError('SCENE_TEXTURE_RGB: explicitly materialize three uint8 RGB bands')
        if min(ds.width,ds.height)<2 or max(ds.width,ds.height)>1024:
            raise BridgeError('SCENE_TEXTURE_SIZE: this single native base-tile profile accepts2..1024 pixels per axis; explicitly resample larger inputs')
        if ds.crs and (crs is None or not crs.equals(ds.crs)):raise BridgeError('SCENE_TEXTURE_CRS_MISMATCH')
        t=ds.transform
        if any(not math.isfinite(v) for v in t) or t.a<=0 or t.e>=0 or t.b or t.d or ds.gcps[0]:raise BridgeError('SCENE_TEXTURE_RECTIFIED_AFFINE_REQUIRED')
        if ds.scales!=(1.,)*3 or ds.offsets!=(0.,)*3:raise BridgeError('SCENE_TEXTURE_CALIBRATION: materialize display RGB values first')
        if not np.all(ds.read_masks()==255):raise BridgeError('SCENE_TEXTURE_MISSING_PIXELS: explicitly fill missing pixels before using an opaque texture')
        left,bottom,right,top=ds.bounds;x0,y0,x1,y1=grid['bounds']
        if left>x0+1e-8 or bottom>y0+1e-8 or right<x1-1e-8 or top<y1-1e-8:raise BridgeError('SCENE_TEXTURE_COVERAGE: registered RGB must cover the whole terrain extent')
        values=ds.read()
        profile={'driver':'GTiff','width':ds.width,'height':ds.height,'count':3,'dtype':'uint8','transform':t,'compress':'deflate'}
        if crs:profile['crs']=crs.to_wkt()
        info={'width':ds.width,'height':ds.height,'bounds':list(ds.bounds),'transform':list(t)[:6],
            'source_pixels':'unaltered uint8 RGB','renderer_display':'original MapGIS texture display conversion and interpolation'}
    return values,profile,info

def prepare_texture(root,stage,spec,sources,frame,grid):
    source=Path(spec.file).resolve(strict=True)
    snapshot=_snapshot_raster(source,sources,stage/'texture-input')
    values,profile,info=_texture_pixels(snapshot,frame,grid)
    canonical=stage/'texture.tif'
    with rasterio.open(canonical,'w',**profile) as ds:ds.write(values)
    work=stage/'texture-preprocess';work.mkdir()
    raw=work/'texture.tiles';write_native_tiles(canonical,raw)
    t=profile['transform'];w=info['width'];h=info['height']
    controls=[{'pixel':[x,y],'world':list(t*(x,y))} for x,y in [(0,0),(w,0),(w,h),(0,h)]]
    command=work/'gcps.txt';command.write_text(''.join(' '.join(map(str,[*p['pixel'],*p['world']]))+'\n' for p in controls),encoding='ascii')
    executable,bas=backend_configuration(root);native=stage/'native/texture.msi'
    created,_=_native_image(executable,bas,work/'create','from_raw',raw,native,command)
    reopened=work/'reopened.tiles';fresh,events=_native_image(executable,bas,work/'reopen','to_raw',native,reopened)
    read_native_tiles(reopened,work/'reopened.tif',{'transform':info['transform'],'color_interpretation':['red','green','blue']})
    with rasterio.open(work/'reopened.tif') as ds:
        if ds.dtypes!=('uint8',)*3 or not np.array_equal(values,ds.read()):raise BridgeError('SCENE_TEXTURE_NATIVE_PIXELS')
    extent=next(v['native_bounds'] for v in events if v.get('event')=='image_extent')
    if any(not math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-7) for a,b in zip(extent,info['bounds'])):raise BridgeError('SCENE_TEXTURE_NATIVE_EXTENT')
    return info|{'native_import':created,'native_pixel_readback':fresh,'pixel_check':'EXACT','native':'texture.tdb','source':'texture.tif'}

def texture_info(folder,members,grid=None):
    """Reject malformed sizes and implicit file references before the old reader."""
    names={name for name in members if name.startswith('texture')}
    if not names:return None
    required={'texture.tdb','texture_DAT/Dat0.py'}
    if not required<=names or names-required-{'texture.msi'}:raise BridgeError('SCENE_TEXTURE_MEMBERS')
    blob=(folder/'texture.tdb').read_bytes()
    if len(blob)<373 or blob[:8]!=b'TEX_INFO' or struct.unpack_from('<I',blob,8)[0]!=17504:
        raise BridgeError('SCENE_TEXTURE_HEADER')
    if b'\0' not in blob[12:268] or blob[12:268].split(b'\0',1)[0]!=b'texture.msi':raise BridgeError('SCENE_TEXTURE_IMAGE_NAME')
    kind,channels,width,height,byte_count,gl_format=struct.unpack_from('<BBHHII',blob,332)
    levels,tile_count,data_files=struct.unpack_from('<hII',blob,346)
    if (kind!=1 or channels!=3 or not 2<=width<=1024 or not 2<=height<=1024 or width&(width-1) or height&(height-1)
            or byte_count!=width*height*3 or gl_format or not 1<=levels<=2 or tile_count!=levels or data_files!=1
            or len(blob)!=356+levels*8+tile_count*9):raise BridgeError('SCENE_TEXTURE_PROFILE')
    for index in range(levels):
        if struct.unpack_from('<II',blob,356+index*8)!=(1,1):raise BridgeError('SCENE_TEXTURE_TILES')
        if struct.unpack_from('<BhhI',blob,356+levels*8+index*9)!=(0,index,0,index*byte_count):raise BridgeError('SCENE_TEXTURE_INDEX')
    if (folder/'texture_DAT/Dat0.py').stat().st_size!=tile_count*byte_count:raise BridgeError('SCENE_TEXTURE_DATA_LENGTH')
    x0,x1,y0,y1=struct.unpack_from('<4d',blob,268);bounds=[x0,y0,x1,y1]
    if not all(math.isfinite(v) for v in bounds) or x0>=x1 or y0>=y1:raise BridgeError('SCENE_TEXTURE_BOUNDS')
    if grid and any(abs(a-b)>max(1e-8,16*math.ulp(max(abs(a),abs(b),1.))) for a,b in zip(bounds,grid['bounds'])):
        raise BridgeError('SCENE_TEXTURE_TERRAIN_ALIGNMENT')
    return {'format':'TEX_INFO17504','kind':'uncompressed RGB','tile_width':width,'tile_height':height,
        'tile_bytes':byte_count,'levels':levels,'tiles':tile_count,'data_files':data_files,'bounds':bounds}
