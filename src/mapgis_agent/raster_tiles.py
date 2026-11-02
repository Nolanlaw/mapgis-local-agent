"""Stream typed pixel tiles through the native MSI API, without TIFF type coercion.

MGRT0002 is a private pipe/file protocol for this package, not an MSI codec.
The installed library alone creates, saves and reads the proprietary MSI file.
"""
from pathlib import Path
import struct

import numpy as np
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.transform import Affine
from rasterio.windows import Window

from .core import BridgeError

DTYPES=('uint8','uint16','uint32','int8','int16','int32','float32','float64')
HEADER=struct.Struct('<8s7Id')


def _shape(kind,width,height,bands,tw,th):
    if not 0<=kind<len(DTYPES) or not 0<bands<=32 or min(width,height,tw,th)<=0:
        raise BridgeError('RASTER_TILE_HEADER')
    if tw>128 or th>128 or tw*th*np.dtype(DTYPES[kind]).itemsize!=16384:
        raise BridgeError('RASTER_TILE_LAYOUT')
    columns=(width+tw-1)//tw;rows=(height+th-1)//th
    if rows*columns*bands*16384>2_000_000_000:
        raise BridgeError('RASTER_NATIVE_CAPACITY: explicit subrasters required for the verified 2GB native profile')
    return rows,columns


def write_native_tiles(source:Path,target:Path):
    with rasterio.open(source) as raster:
        dtype=raster.dtypes[0]
        if dtype not in DTYPES or any(v!=dtype for v in raster.dtypes):raise BridgeError('RASTER_NATIVE_DTYPE')
        kind=DTYPES.index(dtype);size=np.dtype(dtype).itemsize
        tw,th={1:(128,128),2:(128,64),4:(64,64),8:(64,32)}[size]
        rows,columns=_shape(kind,raster.width,raster.height,raster.count,tw,th)
        nodata=raster.nodata
        if nodata is not None and not np.isfinite(nodata):raise BridgeError('RASTER_NATIVE_FINITE_NODATA_REQUIRED')
        with target.open('xb') as stream:
            stream.write(HEADER.pack(b'MGRT0002',kind,raster.width,raster.height,raster.count,tw,th,int(nodata is not None),nodata or 0))
            for band in range(1,raster.count+1):
                for row in range(rows):
                    for col in range(columns):
                        x,y=col*tw,row*th;w,h=min(tw,raster.width-x),min(th,raster.height-y)
                        tile=np.zeros((th,tw),dtype=np.dtype(dtype).newbyteorder('<'))
                        tile[:h,:w]=raster.read(band,window=Window(x,y,w,h))
                        if not np.all(np.isfinite(tile)):raise BridgeError('RASTER_NONFINITE_NATIVE_VALUES: explicit nodata/mask conversion required')
                        stream.write(tile.tobytes())


def read_native_tiles(source:Path,target:Path,spatial:dict):
    with source.open('rb') as stream:
        header=stream.read(HEADER.size)
        if len(header)!=HEADER.size:raise BridgeError('RASTER_TILE_HEADER')
        magic,kind,width,height,bands,tw,th,has_nodata,nodata=HEADER.unpack(header)
        if magic!=b'MGRT0002' or has_nodata not in (0,1) or not np.isfinite(nodata):raise BridgeError('RASTER_TILE_MAGIC_OR_NODATA')
        rows,columns=_shape(kind,width,height,bands,tw,th)
        if source.stat().st_size!=HEADER.size+rows*columns*bands*16384:raise BridgeError('RASTER_TILE_SIZE')
        dtype=DTYPES[kind];profile={'driver':'GTiff','width':width,'height':height,'count':bands,
            'dtype':dtype,'compress':'lzw','BIGTIFF':'IF_SAFER'}
        if has_nodata:profile['nodata']=nodata
        if spatial.get('gcps') and spatial.get('gcp_crs_wkt'):
            profile.update(gcps=[GroundControlPoint(col=p['pixel'][0],row=p['pixel'][1],x=p['world'][0],y=p['world'][1]) for p in spatial['gcps']],
                           crs=rasterio.crs.CRS.from_wkt(spatial['gcp_crs_wkt']))
        else:
            profile['transform']=Affine(*spatial['transform'])
            if spatial.get('crs_wkt'):profile['crs']=rasterio.crs.CRS.from_wkt(spatial['crs_wkt'])
        with rasterio.open(target,'w',**profile) as raster:
            for band in range(1,bands+1):
                for row in range(rows):
                    for col in range(columns):
                        tile=np.frombuffer(stream.read(16384),dtype=np.dtype(dtype).newbyteorder('<')).reshape(th,tw)
                        x,y=col*tw,row*th;w,h=min(tw,width-x),min(th,height-y)
                        raster.write(tile[:h,:w],band,window=Window(x,y,w,h))
            colors=spatial.get('color_interpretation',[])
            if len(colors)==bands:raster.colorinterp=tuple(rasterio.enums.ColorInterp[v] for v in colors)
