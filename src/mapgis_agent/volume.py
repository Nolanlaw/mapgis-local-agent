"""Bounded MapGIS 6.7 XYZV scatter, 3DEM volumes and numeric sections.

The installed conversion/analysis programs establish the binary layouts. Our
writer and explicitly named interpolation methods avoid the observed invalid
directory offsets in this build's original grid saver. Original application
readback is a separate, persisted validation step, never a solver claim.
"""
from __future__ import annotations

import math
import struct
import csv
import json
import os
import uuid
from pathlib import Path
from typing import Literal

import numpy as np
import psutil
import rasterio
from rasterio.transform import from_origin
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.spatial import cKDTree

from .core import Bridge, BridgeError, json_text, sha
from .artifacts import begin_bundle, finish_bundle, fail_bundle, reconcile_bundle
from .raster import explicit_crs,file_sha

MAX_POINTS = 1_000_000
MAX_CELLS = 2_000_000
SCATTER_HEADER = struct.Struct('<i10fii')
MODEL_HEADER = struct.Struct('<4si10fBBii')
LAYER_HEADER = struct.Struct('<Bhii10fi')


class VolumeFrame(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    crs:dict
    z_axis:Literal['elevation_up','depth_down']
    value_name:str=Field(min_length=1,max_length=80)
    value_unit:str=Field(min_length=1,max_length=80)

    @model_validator(mode='after')
    def frame(self):
        crs=explicit_crs(self.crs)
        if crs is None:
            if self.crs.get('unit')!='metre':raise ValueError('Volume local XY must use metres')
        elif not crs.is_projected or len(crs.axis_info)!=2 or any(
            not math.isclose(a.unit_conversion_factor,1.,rel_tol=0,abs_tol=1e-12) for a in crs.axis_info):
            raise ValueError('Volume projected XY must use metres')
        if any(not s.strip() or '\0' in s for s in (self.value_name,self.value_unit)):
            raise ValueError('Explicit value name and unit required')
        return self


class VolumeImportParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    frame:VolumeFrame
    max_coordinate_error_m:float=Field(ge=0)
    max_value_error:float=Field(ge=0)
    native_reopen:bool=False


class VolumeGridParameters(VolumeImportParameters):
    algorithm:Literal['idw_3d','borehole_linear_idw']
    bounds:list[float]=Field(min_length=6,max_length=6)
    shape:list[int]=Field(min_length=3,max_length=3)
    power:float=Field(ge=1,le=16)
    neighbors:int=Field(ge=1,le=256)
    vertical_extrapolation:Literal['reject','clamp']


class VolumeSliceParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    frame:VolumeFrame
    axis:Literal['x','y','z']
    position_m:float
    method:Literal['linear','nearest']
    max_value_error:float=Field(ge=0)


def _finite(values,code):
    values=np.asarray(values,dtype=np.float64)
    if not np.isfinite(values).all():raise BridgeError(code+': finite values required; no implicit NoData or zero substitution')
    return values


def parse_dat(blob:bytes):
    if len(blob)>128_000_000:raise BridgeError('VOLUME_DAT_SIZE')
    try:
        tokens=blob.decode('utf-8-sig').split()
        if not tokens or not tokens[0].isdigit():raise ValueError('count')
        n=int(tokens[0])
        if not 1<=n<=MAX_POINTS or len(tokens)!=1+4*n:raise ValueError('count/records')
        return _finite(np.array(tokens[1:],dtype=np.float64).reshape(n,4),'VOLUME_DAT_VALUES')
    except (UnicodeError,ValueError,OverflowError) as exc:
        raise BridgeError('VOLUME_DAT: ASCII/UTF8 n followed by exactly n XYZV records required') from exc


def _round32(values,coordinate_error,value_error,coordinate_columns=3):
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (coordinate_error,value_error)):
        raise BridgeError('VOLUME_ROUNDING_BUDGET')
    values=_finite(values,'VOLUME_VALUES')
    with np.errstate(over='ignore',invalid='ignore'):
        rounded=values.astype('<f4')
    if not np.isfinite(rounded).all():raise BridgeError('VOLUME_FLOAT32_RANGE')
    delta=np.abs(values-rounded.astype(np.float64))
    c=float(np.max(delta[...,:coordinate_columns],initial=0)) if coordinate_columns else 0.
    v=float(np.max(delta[...,coordinate_columns:],initial=0))
    if c>coordinate_error or v>value_error:
        raise BridgeError(f'VOLUME_ROUNDING: coordinate error {c:.17g}m, value error {v:.17g}; explicit budgets required')
    return rounded,{'max_coordinate_error_m':c,'max_value_error':v}


def _stats(values):
    values=np.asarray(values,dtype=np.float64)
    return [float(values.min()),float(values.max()),float(values.mean()),float(values.std())]


def _groups(points):
    starts=np.r_[0,np.flatnonzero(np.any(points[1:,:2]!=points[:-1,:2],axis=1))+1]
    ends=np.r_[starts[1:],len(points)]
    return starts,ends


def encode_3bn(points,coordinate_error,value_error):
    points=_finite(points,'VOLUME_SCATTER')
    if points.ndim!=2 or points.shape[1]!=4 or not 1<=len(points)<=MAX_POINTS:raise BridgeError('VOLUME_SCATTER_SHAPE')
    data,rounding=_round32(points,coordinate_error,value_error)
    starts,ends=_groups(data)
    extents=[float(v) for j in range(4) for v in (data[:,j].min(),data[:,j].max())]
    stats=_stats(data[:,3])
    header=SCATTER_HEADER.pack(len(data),*extents,stats[2],stats[3],len(starts),int(max(ends-starts)))
    blob=header+bytes(128-len(header))+data.tobytes()
    return blob,{'count':len(data),'groups':len(starts),'max_group_samples':int(max(ends-starts)),
        'bounds':[extents[i] for i in (0,2,4,1,3,5)],'range':extents[6:8],**rounding}


def read_3bn(file):
    file=Path(file)
    if not 144<=file.stat().st_size<=128+16*MAX_POINTS:raise BridgeError('VOLUME_3BN_SIZE')
    blob=file.read_bytes()
    n,*h=SCATTER_HEADER.unpack_from(blob)
    if not 1<=n<=MAX_POINTS or len(blob)!=128+16*n:raise BridgeError('VOLUME_3BN_LAYOUT: count-first legacy header required')
    data=_finite(np.frombuffer(blob,dtype='<f4',offset=128).reshape(n,4),'VOLUME_3BN_VALUES').copy()
    h=np.array(h,dtype=np.float64)
    if not np.isfinite(h).all() or h[10]<1 or h[11]<1:raise BridgeError('VOLUME_3BN_HEADER')
    actual=np.array([v for j in range(4) for v in (data[:,j].min(),data[:,j].max())])
    if not np.array_equal(h[:8],actual):raise BridgeError('VOLUME_3BN_BOUNDS')
    starts,ends=_groups(data)
    return data,{'format':'MapGIS 6.7 count-first 3BN','count':n,'groups':len(starts),
        'header_groups':int(h[10]),'header_max_group_samples':int(h[11]),'max_group_samples':int(max(ends-starts)),
        'bounds':[float(h[i]) for i in (0,2,4,1,3,5)],'range':h[6:8].tolist(),
        'mean':float(h[8]),'standard_deviation':float(h[9]),'record_order':'physical XYZV order',
        'header_group_counts_match':bool(h[10]==len(starts) and h[11]==max(ends-starts))}


def _grid(bounds,shape):
    bounds=_finite(bounds,'VOLUME_BOUNDS')
    if bounds.shape!=(6,) or np.any(bounds[:3]>=bounds[3:]):raise BridgeError('VOLUME_BOUNDS')
    if (not isinstance(shape,(list,tuple)) or len(shape)!=3 or any(type(v) is not int for v in shape)
            or not all(2<=v<=32767 for v in shape[:2]) or not 2<=shape[2]<=256 or math.prod(shape)>MAX_CELLS):
        raise BridgeError('VOLUME_SHAPE: [nx,ny,nz], each >=2; nz<=256; at most 2000000 voxels')
    return bounds,[np.linspace(bounds[i],bounds[i+3],shape[i]) for i in range(3)]


def encode_3dm(values,bounds,coordinate_error,value_error):
    values=_finite(values,'VOLUME_GRID_VALUES')
    if values.ndim!=3:raise BridgeError('VOLUME_GRID_DIMENSIONS')
    nz,ny,nx=values.shape
    bounds,axes=_grid(bounds,[nx,ny,nz])
    rounded,loss=_round32(values[...,None],coordinate_error,value_error,0)
    data=rounded[...,0]
    stored,_=_round32(bounds.reshape(2,3),coordinate_error,0,3)
    b=stored.astype(float).reshape(6)
    if np.any(b[:3]>=b[3:]):raise BridgeError('VOLUME_COLLAPSED_FLOAT32_AXES')
    loss['max_coordinate_error_m']=float(np.max(np.abs(bounds-b)))
    statistics=_stats(data)
    blob=bytearray(MODEL_HEADER.pack(b'3DEM',nz,b[0],b[3],b[1],b[4],b[2],b[5],*statistics,0,0,58,0))
    blob.extend(bytes(4*nz))
    zs=np.linspace(b[2],b[5],nz)
    tops=zs+(b[5]-b[2])/(nz-1)
    stored_z,z_loss=_round32(np.r_[zs,tops].reshape(-1,1),coordinate_error,0,1)
    if np.any(np.diff(stored_z[:nz,0])<=0):raise BridgeError('VOLUME_COLLAPSED_FLOAT32_Z')
    loss['max_coordinate_error_m']=max(loss['max_coordinate_error_m'],z_loss['max_coordinate_error_m'])
    for k,layer in enumerate(data):
        start=len(blob)
        struct.pack_into('<i',blob,58+4*k,start)
        z=float(stored_z[k,0]);top=float(stored_z[k+nz,0])
        if not math.isfinite(top):raise BridgeError('VOLUME_FLOAT32_LAYER_DEPTH')
        header=LAYER_HEADER.pack(2,1,nx,ny,b[0],b[3],b[1],b[4],z,top,*_stats(layer),start+256)
        blob.extend(header+bytes(256-len(header))+layer.tobytes())
    return bytes(blob),{'shape':[nx,ny,nz],'bounds':b.tolist(),'range':statistics[:2],
        'mean':statistics[2],'standard_deviation':statistics[3],'array_order':'zyx','missing_values':'not supported in this finite 3DEM profile',**loss}


def read_3dm(file,statistics_policy='strict',encoding_policy='linear_only'):
    if statistics_policy not in {'strict','report_discrepancies'}:raise BridgeError('VOLUME_3DM_STATISTICS_POLICY')
    if encoding_policy not in {'linear_only','stored','physical'}:raise BridgeError('VOLUME_3DM_ENCODING_POLICY')
    file=Path(file)
    if not 58+8<=file.stat().st_size<=58+256*260+MAX_CELLS*4:raise BridgeError('VOLUME_3DM_SIZE')
    blob=file.read_bytes()
    signature,nz,*h=MODEL_HEADER.unpack_from(blob)
    flags=h[10:12]
    if (signature!=b'3DEM' or not 2<=nz<=256 or h[12:14]!=[58,0]
        or flags not in ([0,0],[1,1]) or (encoding_policy=='linear_only' and flags!=[0,0])):
        raise BridgeError('VOLUME_3DM_PROFILE: default finite linear-axis3DEM; explicit stored/physical permits the verified jointlog10Z/value1,1 profile only; directory@58 required')
    fields=_finite(h[:10],'VOLUME_3DM_HEADER')
    bounds=fields[[0,2,4,1,3,5]]
    if 58+4*nz>len(blob):raise BridgeError('VOLUME_3DM_DIRECTORY')
    offsets=struct.unpack_from(f'<{nz}i',blob,58)
    previous=58+4*nz;layers=[];layer_info=[];axes=None;shape=None
    for k,offset in enumerate(offsets):
        if offset<previous or offset+55>len(blob):raise BridgeError('VOLUME_3DM_OFFSET')
        kind,version,nx,ny,*header=LAYER_HEADER.unpack_from(blob,offset)
        if kind!=2 or version!=1:raise BridgeError('VOLUME_3DM_LAYER_PROFILE')
        _,current_axes=_grid(bounds,[nx,ny,nz])
        if shape is not None and shape!=[nx,ny,nz]:raise BridgeError('VOLUME_3DM_LAYER_DIMENSIONS')
        shape=[nx,ny,nz];axes=current_axes
        header_values=_finite(header[:10],'VOLUME_3DM_LAYER_HEADER')
        if not np.array_equal(header_values[:4],fields[:4]):raise BridgeError('VOLUME_3DM_LAYER_BOUNDS')
        if abs(header_values[4]-axes[2][k])>max(abs(axes[2][k])*2e-7,1e-7):raise BridgeError('VOLUME_3DM_LAYER_Z')
        data_offset=header[10];end=data_offset+nx*ny*4
        if data_offset<offset+55 or end>len(blob):raise BridgeError('VOLUME_3DM_DATA_OFFSET')
        layer=_finite(np.frombuffer(blob,dtype='<f4',count=nx*ny,offset=data_offset).reshape(ny,nx),'VOLUME_3DM_VALUES').copy()
        stats=_stats(layer)
        matching=bool(np.allclose(header_values[6:10],stats,rtol=2e-6,atol=1e-7))
        if not matching and statistics_policy=='strict':raise BridgeError('VOLUME_3DM_LAYER_STATISTICS')
        layers.append(layer);previous=end
        layer_info.append({'nx':nx,'ny':ny,'bounds_xy':fields[:4].tolist(),'z':float(header_values[4]),
            'range':stats[:2],'mean':float(header_values[8]),'standard_deviation':float(header_values[9])})
        if statistics_policy=='report_discrepancies':
            layer_info[-1]|={'mean':stats[2],'standard_deviation':stats[3],
                'statistics':_statistics_record(header_values[6:10],stats,matching)}
    if previous!=len(blob):raise BridgeError('VOLUME_3DM_TRAILING_DATA')
    physical_z=np.array([layer['z'] for layer in layer_info])
    if np.any(np.diff(physical_z)<=0):raise BridgeError('VOLUME_3DM_LAYER_Z_ORDER')
    axes[2]=physical_z
    data=np.stack(layers)
    stats=_stats(data)
    matching=bool(np.allclose(fields[6:10],stats,rtol=2e-6,atol=1e-7))
    if not matching and statistics_policy=='strict':raise BridgeError('VOLUME_3DM_STATISTICS')
    info={'format':'MapGIS 6.7 3DEM','shape':shape,'bounds':bounds.tolist(),'range':stats[:2],
        'mean':float(fields[8]),'standard_deviation':float(fields[9]),'array_order':'zyx','layers':layer_info,
        'missing_values':'finite values only; zero is valid'}
    if statistics_policy=='report_discrepancies':
        info|={'mean':stats[2],'standard_deviation':stats[3],'statistics_policy':statistics_policy,
            'statistics':_statistics_record(fields[6:10],stats,matching)}
    if encoding_policy!='linear_only':
        logarithmic=flags==[1,1]
        if logarithmic and encoding_policy=='physical':
            stored_info=info
            with np.errstate(over='ignore',under='ignore',invalid='ignore'):
                physical_data=np.power(10.,data);physical_z=np.power(10.,axes[2])
            if (not np.isfinite(physical_data).all() or not np.isfinite(physical_z).all()
                or np.any(physical_data<=0) or np.any(physical_z<=0) or np.any(np.diff(physical_z)<=0)):
                raise BridgeError('VOLUME_LOG10_RANGE: physical reconstruction overflows, underflows to zero, or collapses distinctZ nodes')
            data=physical_data;axes=[axes[0],axes[1],physical_z]
            physical_bounds=bounds.copy();physical_bounds[[2,5]]=physical_z[[0,-1]]
            physical_stats=_stats(data)
            physical_layers=[]
            for index,(layer,raw_layer) in enumerate(zip(data,stored_info['layers'])):
                layer_stats=_stats(layer)
                physical_layers.append({'nx':raw_layer['nx'],'ny':raw_layer['ny'],'bounds_xy':raw_layer['bounds_xy'],
                    'z':float(physical_z[index]),'range':layer_stats[:2],'mean':layer_stats[2],
                    'standard_deviation':layer_stats[3]})
            info={k:v for k,v in stored_info.items() if k not in {'bounds','range','mean','standard_deviation','layers','statistics'}}
            info|={'bounds':physical_bounds.tolist(),'range':physical_stats[:2],'mean':physical_stats[2],
                'standard_deviation':physical_stats[3],'layers':physical_layers,'stored_metrics':stored_info,
                'statistics_domain':'recomputed from individually reconstructed physical voxels; not exponentiated stored mean or standard deviation'}
        info['encoding']={'policy':encoding_policy,'flags_at_file_bytes48_49':flags,
            'stored_profile':'log10_z_and_value' if logarithmic else 'linear_z_and_value','base':10 if logarithmic else None,
            'returned_domain':'physical_reconstructed' if logarithmic and encoding_policy=='physical' else 'stored' if encoding_policy=='stored' else 'physical_linear',
            'inverse':'float64 10**each savedfloat32logZ/value' if logarithmic and encoding_policy=='physical' else 'none',
            'precision':'reconstructed physical samples inherit storedfloat32log quantization; original prelog measurements are not recovered exactly',
            'units_and_crs':'not inferred; use the explicit bound source frame when available',
            'native_scope':'original information window reads stored headers; physical decoding is independently computed',
            'profile_evidence':'pinned originalW60_Anly.exe419680 writes jointflags and log10Z/value usingFYL2X*log10(2)'}
    return axes,data,info


def _statistics_record(declared,computed,matching):
    def summary(values):return {'range':[float(values[0]),float(values[1])],
        'mean':float(values[2]),'standard_deviation':float(values[3])}
    return {'declared':summary(declared),'computed':summary(computed),'matches':matching,
        'comparison':{'relative_tolerance':2e-6,'absolute_tolerance':1e-7},
        'policy':'Report existing saved statistics separately from independently recomputed finite voxel values; no input repair, clipping, missing-value inference or source mutation'}


def _weights(tree,queries,power,neighbors):
    k=min(neighbors,tree.n)
    distances,indices=tree.query(queries,k=k,workers=1)
    if k==1:distances=distances[:,None];indices=indices[:,None]
    zero=distances[:,0]==0
    with np.errstate(divide='ignore',invalid='ignore'):
        weights=(distances[:,:1]/distances)**power
    weights[zero]=0;weights[zero,0]=1
    return indices,weights/weights.sum(axis=1,keepdims=True)


def interpolate_volume(points,bounds,shape,algorithm,power,neighbors,vertical_extrapolation):
    points=_finite(points,'VOLUME_SCATTER');bounds,axes=_grid(bounds,shape)
    if points.ndim!=2 or points.shape[1]!=4 or not 1<=len(points)<=MAX_POINTS:raise BridgeError('VOLUME_SCATTER_SHAPE')
    if algorithm not in {'idw_3d','borehole_linear_idw'} or not 1<=power<=16 or type(neighbors) is not int or not 1<=neighbors<=256:
        raise BridgeError('VOLUME_INTERPOLATION_PARAMETERS')
    if vertical_extrapolation not in {'reject','clamp'}:raise BridgeError('VOLUME_VERTICAL_EXTRAPOLATION')
    output=np.empty(tuple(shape[::-1]),dtype=np.float64)
    if algorithm=='idw_3d':
        if vertical_extrapolation!='reject':raise BridgeError('VOLUME_VERTICAL_POLICY: idw_3d uses explicit full-box 3D extrapolation, not vertical clamp')
        xyz,first,inverse=np.unique(points[:,:3],axis=0,return_index=True,return_inverse=True)
        scalar=points[first,3]
        if not np.array_equal(points[:,3],scalar[inverse]):raise BridgeError('VOLUME_DUPLICATE_VALUES')
        tree=cKDTree(xyz)
        flat=output.reshape(-1);chunk=max(1,min(16384,1_000_000//min(neighbors,len(xyz))))
        for start in range(0,len(flat),chunk):
            n=np.arange(start,min(start+chunk,len(flat)))
            k,j,i=np.unravel_index(n,output.shape)
            queries=np.column_stack((axes[0][i],axes[1][j],axes[2][k]))
            indices,weights=_weights(tree,queries,power,neighbors)
            flat[start:start+len(n)]=np.sum(weights*scalar[indices],axis=1)
        extra={'unique_points':len(xyz),'extrapolation':'full rectangular XYZ box; no hull mask'}
    else:
        starts,ends=_groups(points);holes=[points[a:b] for a,b in zip(starts,ends)]
        xy=points[starts,:2]
        if len(np.unique(xy,axis=0))!=len(holes):raise BridgeError('VOLUME_BOREHOLE_ORDER: each XY group must be contiguous')
        if any(len(h)<2 or np.any(np.diff(h[:,2])<=0) for h in holes):raise BridgeError('VOLUME_BOREHOLE_ORDER: at least two strictly increasing Z samples per XY group')
        if vertical_extrapolation=='reject' and any(bounds[2]<h[0,2] or bounds[5]>h[-1,2] for h in holes):
            raise BridgeError('VOLUME_VERTICAL_EXTRAPOLATION_REQUIRED')
        vertical=np.array([np.interp(axes[2],h[:,2],h[:,3]) for h in holes])
        tree=cKDTree(xy);xx,yy=np.meshgrid(axes[0],axes[1]);queries=np.column_stack((xx.ravel(),yy.ravel()))
        chunk=max(1,min(16384,1_000_000//min(neighbors,len(holes))))
        for start in range(0,len(queries),chunk):
            indices,weights=_weights(tree,queries[start:start+chunk],power,neighbors)
            for k in range(shape[2]):output[k].ravel()[start:start+len(indices)]=np.sum(weights*vertical[indices,k],axis=1)
        extra={'borehole_groups':len(holes),'vertical_extrapolation':vertical_extrapolation,
            'extrapolation':'XY inverse-distance extrapolation over the full rectangle'}
    return output,{'algorithm':algorithm,'implementation':'assistant NumPy/SciPy; not the original MapGIS solver',
        'power':power,'neighbors':neighbors,'coincidence':'exact XYZ or XY match keeps its value','log_transform':False,**extra}


def slice_volume(axes,values,axis,position,method):
    if axis not in 'xyz' or len(axis)!=1 or method not in {'linear','nearest'} or not math.isfinite(position):raise BridgeError('VOLUME_SLICE_PARAMETERS')
    index='xyz'.index(axis);grid=np.asarray(axes[index]);dimension=2-index
    if position<grid[0] or position>grid[-1]:raise BridgeError('VOLUME_SLICE_OUTSIDE')
    high=int(np.searchsorted(grid,position,side='left'));high=min(high,len(grid)-1)
    if method=='nearest':
        selected=int(np.argmin(np.abs(grid-position)));result=np.take(values,selected,axis=dimension)
        actual=float(grid[selected]);slots=[selected,selected];fraction=0.
    else:
        low=max(0,high-1)
        fraction=0. if high==low else float((position-grid[low])/(grid[high]-grid[low]))
        result=np.take(values,low,axis=dimension)*(1-fraction)+np.take(values,high,axis=dimension)*fraction
        actual=float(position);slots=[low,high]
    remaining=[i for i in range(3) if i!=index]
    u,v=axes[remaining[0]],axes[remaining[1]]
    uu,vv=np.meshgrid(u,v)
    world=np.empty(result.shape+(3,));world[...,index]=actual
    world[...,remaining[0]]=uu;world[...,remaining[1]]=vv
    return u,v,result,world,{'fixed_axis':axis,'requested_position_m':position,'actual_position_m':actual,
        'column_axis':'xyz'[remaining[0]],'row_axis':'xyz'[remaining[1]],'method':method,
        'bracketing_indices':slots,'fraction':fraction,'nearest_tie':'lower coordinate'}


def _source(file,expected_sha256,frame=None):
    path=Path(file).resolve(strict=True)
    limit={'.dat':128_000_000,'.3bn':128+16*MAX_POINTS,'.3dm':58+256*260+MAX_CELLS*4}.get(path.suffix.lower())
    if limit is None or path.stat().st_size>limit:raise BridgeError('VOLUME_INPUT_PROFILE_OR_SIZE')
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    sources=[{'file':str(path),'sha256':expected_sha256}]
    metadata_file=path.with_name(path.name+'.json')
    metadata=None
    if path.suffix.lower()!='.dat' and metadata_file.exists():
        if metadata_file.stat().st_size>4_000_000:raise BridgeError('VOLUME_METADATA_SIZE')
        raw=metadata_file.read_bytes();metadata=json.loads(raw)
        if metadata.get('schema')!='mapgis-agent/volume/1' or metadata.get('native_sha256')!=expected_sha256:
            raise BridgeError('VOLUME_METADATA_MISMATCH')
        saved_frame=VolumeFrame.model_validate(metadata['frame']).model_dump()
        if frame is not None and saved_frame!=frame:raise BridgeError('VOLUME_FRAME_MISMATCH')
        sources.append({'file':str(metadata_file),'sha256':sha(raw)})
    return path,sources,metadata


def _unchanged(sources):
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in sources):raise BridgeError('STALE_SOURCE')


def _data_info(path,statistics_policy='strict',encoding_policy='linear_only'):
    if path.suffix.lower()=='.3bn':
        if statistics_policy!='strict':raise BridgeError('VOLUME_3DM_STATISTICS_POLICY: report_discrepancies is currently3DM-only')
        if encoding_policy!='linear_only':raise BridgeError('VOLUME_3DM_ENCODING_POLICY: explicitstored/physical is currently3DM-only')
        data,info=read_3bn(path);return None,data,info
    if path.suffix.lower()=='.3dm':return read_3dm(path,statistics_policy,encoding_policy)
    raise BridgeError('VOLUME_EXTENSION: .3bn or .3dm required')


def volume_source_info(root,file,expected_sha256,native_reopen=False,statistics_policy='strict',encoding_policy='linear_only'):
    path,sources,metadata=_source(file,expected_sha256)
    _,_,info=_data_info(path,statistics_policy,encoding_policy)
    result={'file':str(path),'sha256':expected_sha256,'metrics':info,'sources':sources,
        'frame':metadata['frame'] if metadata else None,'source_preserved':True,
        'native_readback_status':'NOT_REQUESTED','semantics':'XYZV volume data; not a closed geological solid'}
    if native_reopen:
        _native_route_required(root,{'native_reopen':True})
        folder=Bridge(root).destination_path(Path(root)/'work/volume-info'/uuid.uuid4().hex)
        folder.mkdir(parents=True)
        result['native_readback']=_native_validation(Path(root),path,folder)
        _verify_original(info,result['native_readback'])
        stored_info=info.get('stored_metrics',info)
        discrepancy=not stored_info.get('statistics',{}).get('matches',True) or any(not layer.get('statistics',{}).get('matches',True) for layer in stored_info.get('layers',[]))
        result['native_readback_status']='VERIFIED_DECLARED_HEADERS_WITH_REPORTED_VOXEL_STATISTICS_DISCREPANCIES' if discrepancy else 'VERIFIED_HEADERS_BY_ORIGINAL_APPLICATION'
        if encoding_policy!='linear_only':result['native_readback_domain']='stored headers; physical values are reconstructed independently, not a native numeric read'
    _unchanged(sources)
    return result


def read_volume_samples(file,expected_sha256,offset=0,limit=1000,statistics_policy='strict',encoding_policy='linear_only'):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=1000:raise BridgeError('VOLUME_READ_WINDOW')
    path,sources,metadata=_source(file,expected_sha256)
    axes,data,info=_data_info(path,statistics_policy,encoding_policy)
    if axes is None:
        rows=[{'index':i,'xyz':row[:3].tolist(),'value':float(row[3])} for i,row in enumerate(data[offset:offset+limit],offset)]
        count=len(data)
    else:
        count=data.size;rows=[]
        for index in range(offset,min(count,offset+limit)):
            k,j,i=np.unravel_index(index,data.shape)
            rows.append({'index':index,'ijk':[int(i),int(j),int(k)],'xyz':[float(axes[0][i]),float(axes[1][j]),float(axes[2][k])],
                'value':float(data[k,j,i])})
    _unchanged(sources)
    stored_info=info.get('stored_metrics',info)
    result={'file':str(path),'sha256':expected_sha256,'total':count,'offset':offset,'count':len(rows),'records':rows,
        'frame':metadata['frame'] if metadata else None,'physical_order':'scatter records or x-fastest/y/z model nodes','source_preserved':True,
        'statistics_policy':statistics_policy,'statistics_discrepancies':not stored_info.get('statistics',{}).get('matches',True)
            or any(not layer.get('statistics',{}).get('matches',True) for layer in stored_info.get('layers',[]))}
    if encoding_policy!='linear_only':result['encoding']=info['encoding']
    return result


def _native_validation(root,path,folder):
    if path.suffix.lower()=='.grd':
        from .native_terrain import _native_grid_read
        return _native_grid_read(root,path,folder/'grid')
    # Original 3DEM has no exported reader in the installed analysis DLL.
    # The isolated app route is kept separate from all numerical file methods.
    from .volume_native import native_volume_readback
    return native_volume_readback(root,path,folder)


def _native_route_required(root,parameters):
    if parameters.get('native_reopen'):
        try:
            from .volume_native import native_route_enabled
        except ImportError as exc:
            raise BridgeError('VOLUME_NATIVE_ROUTE_NOT_ENABLED: original analysis UI requires the project-approved driver route; file-format operations remain available with native_reopen=false') from exc
        if not native_route_enabled(root):raise BridgeError('VOLUME_NATIVE_ROUTE_NOT_ENABLED: native_volume_private_ui=true and existing legacy-host opt-in required')


def _native_fixed(value,places):
    # This pinned 2005 build uses the legacy Microsoft CRT printf rule, verified
    # on the original4172.9140625 header and positive/negative CRT probes.
    # Modern Python formatting instead uses ties-to-even. Preserve the exact
    # displayed precision; do not replace ordered comparisons with a tolerance.
    from decimal import Decimal,localcontext,ROUND_HALF_UP
    with localcontext() as context:
        context.prec=80
        rounded=Decimal.from_float(float(value)).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
    return format(rounded,f'.{places}f')


def _verify_original(info,readback):
    info=info.get('stored_metrics',info)
    if not readback.get('source_preserved') or not readback.get('own_process_ended'):
        raise BridgeError('VOLUME_NATIVE_READBACK_LIFECYCLE')
    normalize=lambda text:''.join(text.split())
    lines=[normalize(s) for s in readback['lines']]
    if 'count' in info:
        b=info['bounds'];v=info['range']
        expected=[f'离散点数 : {info["count"]}',f'钻孔个数 : {info["header_groups"]}',
            *[f'{axis}{label}值 : {_native_fixed(value,6)}' for axis,low,high in zip('XYZ',b[:3],b[3:]) for label,value in [('最小',low),('最大',high)]],
            f'V最小值 : {_native_fixed(v[0],6)}',f'V最大值 : {_native_fixed(v[1],6)}',f'V均  值 : {_native_fixed(info["mean"],6)}',f'V方  差 : {_native_fixed(info["standard_deviation"],6)}']
    else:
        expected=[f'当前立体模型共{len(info["layers"])}层']
        for index,layer in enumerate(info['layers'],1):
            xmin,xmax,ymin,ymax=layer['bounds_xy']
            declared=layer.get('statistics',{}).get('declared',layer)
            expected.extend([f'第{index}层',f'X方向点数:{layer["nx"]}',f'Y方向点数:{layer["ny"]}',
                f'X坐标范围:{_native_fixed(xmin,2)}-->{_native_fixed(xmax,2)}',f'Y坐标范围:{_native_fixed(ymin,2)}-->{_native_fixed(ymax,2)}',
                f'最 小 值 :{_native_fixed(declared["range"][0],6)}',f'最 大 值 :{_native_fixed(declared["range"][1],6)}',f'平 均 值 :{_native_fixed(declared["mean"],6)}'])
    if [normalize(s) for s in expected]!=lines:
        raise BridgeError('VOLUME_NATIVE_HEADER_DIFF: ordered native model/layer headers differ')


def _name(template,extension):
    if set(template)!={'name'}:raise BridgeError('VOLUME_TEMPLATE: only an explicit new filename is accepted')
    name=template['name']
    if (not isinstance(name,str) or Path(name).name!=name or not name.isascii() or name!=name.lower()
            or Path(name).suffix!=extension or any(c in name for c in '/\\:<>"|?*') or name.endswith((' ','.'))):
        raise BridgeError('VOLUME_TEMPLATE_NAME: lowercase ASCII filename with '+extension+' required')
    return name


def _start(bridge,payload,request_id,sources,name):
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if new:
        process=psutil.Process()
        (stage/'producer.json').write_text(json_text({'pid':process.pid,'start':process.create_time(),'executable':process.exe()}),encoding='utf8')
        (stage/'files').mkdir()
    return stage,receipt,new


def _checkpoint(path,record):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf8') as stream:
        stream.write(json_text(record));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _save_candidate(bridge,request_id,stage,name,metadata,sources):
    files=stage/'files'
    record={'name':name,'metadata':metadata,'sources':sources,
        'members':{p.name:sha(p.read_bytes()) for p in files.iterdir() if p.is_file()}}
    encoded=json_text(record)
    _checkpoint(stage/'candidate.json',record)
    with bridge.connect() as db:
        row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
        if row is None or row['state']!='RUNNING':raise BridgeError('VOLUME_CANDIDATE_REQUEST_STATE')
        receipt=bridge._receipt(row);receipt['volume_candidate_sha256']=sha(encoded.encode('utf8'))
        receipt.pop('state',None)
        db.execute('UPDATE commands SET receipt=? WHERE id=?',(json_text(receipt),request_id))


def _frozen_candidate(bridge,request_id,stage):
    raw=(stage/'candidate.json').read_bytes()
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None or bridge._receipt(row).get('volume_candidate_sha256')!=sha(raw):raise BridgeError('VOLUME_CANDIDATE_MANIFEST_CHANGED')
    candidate=json.loads(raw)
    _unchanged(candidate['sources'])
    files=stage/'files';name=candidate['name'];metadata=candidate['metadata']
    for member,digest in candidate['members'].items():
        if sha((files/member).read_bytes())!=digest:raise BridgeError('VOLUME_CANDIDATE_CHANGED')
    return candidate


def _seal(bridge,request_id,stage):
    candidate=_frozen_candidate(bridge,request_id,stage)
    files=stage/'files';name=candidate['name'];metadata=candidate['metadata']
    validation=json.loads((stage/'native-validation.json').read_text(encoding='utf8'))
    if validation['candidate_sha256']!=candidate['members'][name]:raise BridgeError('VOLUME_VALIDATION_IDENTITY')
    if Path(name).suffix!='.grd' and metadata['native_validation_policy']=='original_headers':
        _,_,info=_data_info(files/name);_verify_original(info,validation['readback'])
    elif Path(name).suffix=='.grd' and not validation['readback'].get('all_cells_equal'):raise BridgeError('VOLUME_NATIVE_SLICE_DIFF')
    metadata=metadata|{'native_readback':validation['readback'],'source_preserved':True}
    sidecar={'schema':'mapgis-agent/volume/1','native_sha256':candidate['members'][name],**metadata}
    if Path(name).suffix=='.grd':
        sidecar.update(schema='mapgis-agent/grid/1',grid_sha256=candidate['members'][name])
    target=files/(name+'.json')
    encoded=json_text(sidecar)
    if target.exists() and target.read_text(encoding='utf8')!=encoded:raise BridgeError('VOLUME_SIDECAR_CHANGED')
    if not target.exists():target.write_text(encoded,encoding='utf8')
    if {p.name for p in files.iterdir()}!=set(candidate['members'])|{target.name}:raise BridgeError('VOLUME_ARTIFACT_MEMBER_CHANGED')
    return finish_bundle(bridge,request_id,files,metadata)


def _finish(bridge,request_id,stage,name,metadata,sources):
    _save_candidate(bridge,request_id,stage,name,metadata,sources)
    folder=stage/'native';folder.mkdir()
    if metadata['native_validation_policy']=='format_only':
        readback={'status':'NOT_REQUESTED','format_values_checked':True,'original_application_headers':'NOT_VERIFIED_FOR_THIS_OUTPUT'}
    else:readback=_native_validation(bridge.root,stage/'files'/name,folder)
    _checkpoint(stage/'native-validation.json',{'candidate_sha256':sha((stage/'files'/name).read_bytes()),'readback':readback})
    return _seal(bridge,request_id,stage)


def prepare_volume_import(bridge,file,expected_sha256,parameters,template,request_id):
    parameters=VolumeImportParameters.model_validate(parameters);p=parameters.model_dump();frame=p['frame']
    _native_route_required(bridge.root,p)
    name=_name(template,'.3bn');path,sources,_=_source(file,expected_sha256)
    if path.suffix.lower()!='.dat':raise BridgeError('VOLUME_DAT_EXTENSION')
    points=parse_dat(path.read_bytes())
    blob,info=encode_3bn(points,p['max_coordinate_error_m'],p['max_value_error'])
    payload={'operation':'volume.scatter.import','file':str(path),'expected_sha256':expected_sha256,'parameters':p,'template':template,'source_members':sources}
    stage,receipt,new=_start(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        target=stage/'files'/name;target.write_bytes(blob)
        actual,_=read_3bn(target)
        if not np.array_equal(actual,points.astype('<f4')):raise BridgeError('VOLUME_SCATTER_SAVED_DIFF')
        metadata={'frame':frame,'metrics':info,'writer':'assistant compatible count-first MapGIS 6.7 3BN',
            'native_validation_policy':'original_headers' if p['native_reopen'] else 'format_only',
            'input':{'file':str(path),'sha256':expected_sha256},'record_order':'unchanged','missing_values':'rejected, never substituted'}
        return _finish(bridge,request_id,stage,name,metadata,sources)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def prepare_volume_grid(bridge,file,expected_sha256,parameters,template,request_id):
    parameters=VolumeGridParameters.model_validate(parameters);p=parameters.model_dump();frame=p['frame']
    _native_route_required(bridge.root,p)
    name=_name(template,'.3dm');path,sources,source_metadata=_source(file,expected_sha256,frame)
    if path.suffix.lower()!='.3bn':raise BridgeError('VOLUME_GRID_3BN_REQUIRED')
    points,source_info=read_3bn(path);_grid(p['bounds'],p['shape'])
    payload={'operation':'volume.grid','file':str(path),'expected_sha256':expected_sha256,'parameters':p,'template':template,'source_members':sources}
    stage,receipt,new=_start(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        values,algorithm=interpolate_volume(points,p['bounds'],p['shape'],p['algorithm'],p['power'],p['neighbors'],p['vertical_extrapolation'])
        blob,info=encode_3dm(values,p['bounds'],p['max_coordinate_error_m'],p['max_value_error'])
        target=stage/'files'/name;target.write_bytes(blob)
        _,actual,_=read_3dm(target)
        if not np.array_equal(actual,values.astype('<f4')):raise BridgeError('VOLUME_GRID_SAVED_DIFF')
        metadata={'frame':frame,'metrics':info,'algorithm':algorithm,'writer':'assistant compatible 3DEM',
            'native_validation_policy':'original_headers' if p['native_reopen'] else 'format_only',
            'input':{'file':str(path),'sha256':expected_sha256,'metrics':source_info},
            'interpretation':'numeric XYZV field; no lithological contact, fault, closed solid or resource classification inferred'}
        return _finish(bridge,request_id,stage,name,metadata,sources)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def prepare_volume_slice(bridge,file,expected_sha256,parameters,template,request_id):
    parameters=VolumeSliceParameters.model_validate(parameters);p=parameters.model_dump();frame=p['frame']
    name=_name(template,'.grd');path,sources,_=_source(file,expected_sha256,frame)
    if path.suffix.lower()!='.3dm':raise BridgeError('VOLUME_SLICE_3DM_REQUIRED')
    axes,values,_=read_3dm(path)
    u,v,section,world,mapping=slice_volume(axes,values,p['axis'],p['position_m'],p['method'])
    quantized,loss=_round32(section[...,None],0,p['max_value_error'],0);quantized=quantized[...,0]
    if np.any(np.abs(quantized)>=3.4e18):raise BridgeError('VOLUME_SLICE_NATIVE_VALUE_RANGE: existing MapGIS DTM finite values must have absolute magnitude below 3.4e18')
    if len(u)>32767 or len(v)>32767:raise BridgeError('VOLUME_SLICE_SURFER_DIMENSIONS')
    payload={'operation':'volume.slice','file':str(path),'expected_sha256':expected_sha256,'parameters':p,'template':template,'source_members':sources}
    stage,receipt,new=_start(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        target=stage/'files'/name
        dx=float(u[1]-u[0]);dy=float(v[1]-v[0])
        transform=from_origin(float(u[0]-dx/2),float(v[-1]+dy/2),dx,dy)
        with rasterio.open(target,'w',driver='GSBG',width=len(u),height=len(v),count=1,dtype='float32',transform=transform) as dataset:
            dataset.write(np.flipud(quantized),1)
        from .native_terrain import read_grid
        _,_,saved,info=read_grid(target)
        if not np.array_equal(saved.data,quantized):raise BridgeError('VOLUME_SLICE_SAVED_DIFF')
        csv_name=Path(name).stem+'-xyzv.csv'
        with (stage/'files'/csv_name).open('w',encoding='utf8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['column','row','x_m','y_m','z_m','value'])
            for row in range(len(v)):
                for col in range(len(u)):writer.writerow([col,row,*world[row,col].tolist(),float(quantized[row,col])])
        metadata={'frame':frame,'metrics':info,'axis_mapping':mapping,'rounding':loss,'native_validation_policy':'native_grd_all_cells',
            'grid_coordinate_frame':'columns='+mapping['column_axis']+'; rows='+mapping['row_axis']+'; both metres',
            'input':{'file':str(path),'sha256':expected_sha256},'numeric_section':'world XYZ retained in CSV; X/Y sections are section coordinates, not a geographic XY map'}
        metadata['crs']=frame['crs'] if p['axis']=='z' else {
            'kind':'local','name':f'Volume section {p["axis"]}={mapping["actual_position_m"]:.17g}; columns {mapping["column_axis"]}, rows {mapping["row_axis"]}; {frame["z_axis"]}',
            'unit':'metre','axis_order':'xy','reference_sha256':expected_sha256}
        return _finish(bridge,request_id,stage,name,metadata,sources)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def reconcile_volume(bridge,request_id):
    bridge.request_id(request_id)
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('artifact_type') not in {'volume.scatter.import','volume.grid','volume.slice'}:raise BridgeError('VOLUME_REQUEST_REQUIRED')
    if row['state'] not in {'RUNNING','FAILED'}:return bridge.status(request_id)
    stage=bridge.destination_path(receipt['stage'])
    if (stage/'complete.json').exists():return reconcile_bundle(bridge,request_id)
    if row['state']=='RUNNING' and (stage/'producer.json').exists():
        producer=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(producer['pid'])
            if abs(process.create_time()-producer['start'])<1e-5 and process.exe()==producer['executable']:
                return receipt|{'producer':'RUNNING','replayed':False}
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied as exc:raise BridgeError('VOLUME_PRODUCER_IDENTITY_UNAVAILABLE') from exc
    if (stage/'candidate.json').exists() and (stage/'native-validation.json').exists():
        return _seal(bridge,request_id,stage)
    if (stage/'candidate.json').exists():
        candidate=_frozen_candidate(bridge,request_id,stage)
        folders=[stage/'native',*stage.glob('native-verify-*')]
        for folder in folders:
            for name in ('reader.started.json','reader.actual.json','application.started.json','grid/native-read.json.started.json'):
                path=folder/name
                if not path.exists():continue
                identity=json.loads(path.read_text(encoding='utf8'))
                try:
                    process=psutil.Process(identity['pid'])
                    start=identity.get('start',identity.get('process_start'))
                    if abs(process.create_time()-start)<1e-5 and process.exe()==identity['executable']:
                        return receipt|{'native_reader':'RUNNING','replayed':False}
                except psutil.NoSuchProcess:pass
                except psutil.AccessDenied as exc:raise BridgeError('VOLUME_NATIVE_IDENTITY_UNAVAILABLE') from exc
        from .native_paths import cleanup_aliases
        for folder in folders:
            if folder.exists():cleanup_aliases(folder)
        name=candidate['name'];metadata=candidate['metadata']
        if metadata['native_validation_policy']=='format_only':
            readback={'status':'NOT_REQUESTED','format_values_checked':True,'original_application_headers':'NOT_VERIFIED_FOR_THIS_OUTPUT'}
        else:
            attempt=stage/('native-verify-'+uuid.uuid4().hex[:12]);attempt.mkdir()
            readback=_native_validation(bridge.root,stage/'files'/name,attempt)
            readback|={'readonly_verification_retried':True,'producing_algorithm_replayed':False}
        _checkpoint(stage/'native-validation.json',{'candidate_sha256':candidate['members'][name],'readback':readback})
        return _seal(bridge,request_id,stage)
    raise BridgeError('ARTIFACT_INCOMPLETE: retained volume candidate; neither computation nor native commands were replayed')
