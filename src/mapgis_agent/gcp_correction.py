"""Explicit-order GDAL GCP models with fit, inverse and withheld-point checks."""
from __future__ import annotations
import ctypes as C
import math
import numpy as np
from shapely.geometry import MultiPoint,Point
from .core import BridgeError
from .grid_analysis import _gdal,_LOCK
from .raster import GroundControlPoint,fit_affine,AffineFitError


class _GCP(C.Structure):
    _fields_=[('id',C.c_char_p),('info',C.c_char_p),('pixel',C.c_double),('line',C.c_double),
        ('x',C.c_double),('y',C.c_double),('z',C.c_double)]


class _GDALModel:
    def __init__(self,gcps,method):
        with _LOCK:
            self.library,self.runtime=_gdal()
            pointer=C.c_void_p;doubles=C.POINTER(C.c_double)
            signatures=[('GDALCreateGCPTransformer',pointer,[C.c_int,C.POINTER(_GCP),C.c_int,C.c_int]),
                ('GDALCreateTPSTransformer',pointer,[C.c_int,C.POINTER(_GCP),C.c_int])]
            for name in ['GCP','TPS']:
                signatures.extend([('GDAL'+name+'Transform',C.c_int,[pointer,C.c_int,C.c_int,doubles,doubles,doubles,C.POINTER(C.c_int)]),
                    ('GDALDestroy'+name+'Transformer',None,[pointer])])
            for name,result,args in signatures:
                f=getattr(self.library,name);f.restype=result;f.argtypes=args
        self.kind='TPS' if method=='tps' else 'GCP'
        rows=(_GCP*len(gcps))(*[_GCP(b'',b'',p.col,p.row,p.x,p.y,0.) for p in gcps])
        factory=getattr(self.library,'GDALCreate'+self.kind+'Transformer')
        self.handle=factory(len(gcps),rows,0) if method=='tps' else factory(len(gcps),rows,int(method[-1]),0)
        if not self.handle:raise BridgeError('GCP_MODEL_FAILED: '+self.library.CPLGetLastErrorMsg().decode('utf8',errors='replace'))
    def transform(self,points,inverse=False):
        n=len(points);x=(C.c_double*n)(*points[:,0]);y=(C.c_double*n)(*points[:,1]);z=(C.c_double*n)();ok=(C.c_int*n)()
        result=getattr(self.library,'GDAL'+self.kind+'Transform')(self.handle,int(inverse),n,x,y,z,ok)
        output=np.column_stack([x,y])
        # GDAL3.10 could return true when only some points succeeded.
        if not result or not all(ok) or not np.all(np.isfinite(output)):raise BridgeError('GCP_TRANSFORM_FAILED: every point must succeed')
        return output
    def close(self):
        if self.handle:getattr(self.library,'GDALDestroy'+self.kind+'Transformer')(self.handle);self.handle=None


def _tolerance(value,label,optional=False):
    if value is None and optional:return
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise BridgeError(label+': positive finite tolerance required')


def _statistics(predicted,expected):
    residual=predicted-expected;errors=np.linalg.norm(residual,axis=1)
    return {'rms_world_error':float(np.sqrt(np.mean(errors**2))),'max_world_error':float(errors.max()),
        'residuals_xy':residual.tolist()}


def validate_gcp_model(gcps,method,checkpoints,max_error,max_inverse_error,max_checkpoint_error=None):
    minimum={'affine':3,'polynomial2':6,'polynomial3':10,'tps':3}
    if method not in minimum:raise BridgeError('WARP_GCP_MODEL: affine, polynomial2, polynomial3 or tps required')
    if not minimum[method]<=len(gcps)<=1000:raise BridgeError(f'GCP_MINIMUM: {method} requires {minimum[method]}..1000 control points')
    _tolerance(max_error,'WARP_GCP_TOLERANCE');_tolerance(max_inverse_error,'WARP_GCP_INVERSE_TOLERANCE')
    _tolerance(max_checkpoint_error,'WARP_GCP_CHECKPOINT_TOLERANCE',True)
    xy=np.array([[p.col,p.row] for p in gcps],dtype=float);world=np.array([[p.x,p.y] for p in gcps],dtype=float)
    if not np.all(np.isfinite(xy)) or not np.all(np.isfinite(world)):raise BridgeError('GCP_FINITE_COORDINATES')
    if len(set(map(tuple,xy)))!=len(xy):raise BridgeError('GCP_DUPLICATE_PIXEL')
    degree=1 if method in {'affine','tps'} else int(method[-1]);span=np.ptp(xy,axis=0)
    if np.any(span==0):raise BridgeError('GCP_DEGENERATE: spatially distributed controls required')
    normalized=(xy-xy.mean(axis=0))/span
    matrix=np.column_stack([normalized[:,0]**a*normalized[:,1]**b for total in range(degree+1) for a in range(total+1) for b in [total-a]])
    if np.linalg.matrix_rank(matrix)<matrix.shape[1] or np.linalg.matrix_rank(world-world[0])<2:raise BridgeError('GCP_DEGENERATE: requested order is not identifiable in both frames')
    if not isinstance(checkpoints,list) or len(checkpoints)>1000:raise BridgeError('GCP_CHECKPOINTS')
    if max_checkpoint_error is not None and not checkpoints:raise BridgeError('GCP_CHECKPOINTS_REQUIRED: a checkpoint tolerance needs actual withheld points')
    checked=[];heldout=set()
    for point in checkpoints:
        if not isinstance(point,dict) or set(point)!={'pixel','world'} or any(not isinstance(point[k],list) or len(point[k])!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in point[k]) for k in ['pixel','world']):raise BridgeError('GCP_CHECKPOINTS: finite pixel/world pairs required')
        pixel=tuple(point['pixel'])
        if pixel in set(map(tuple,xy)):raise BridgeError('GCP_CHECKPOINT_REUSED: fitting controls cannot be independent checkpoints')
        if pixel in heldout:raise BridgeError('GCP_CHECKPOINT_DUPLICATE')
        heldout.add(pixel);checked.append(point)
    model=None
    try:
        if method=='affine':
            try:fit=fit_affine([GroundControlPoint(p.col,p.row,p.x,p.y) for p in gcps]);fit.solve(*fit.apply(0,0))
            except AffineFitError as exc:raise BridgeError('WARP_GCP_AFFINE_REQUIRED') from exc
            def transform(points,inverse=False):return np.array([(fit.solve if inverse else fit.apply)(*row) for row in points])
            runtime={'api':'existing affine least-squares fit','coefficients':list(fit.coefficients)}
        else:
            model=_GDALModel(gcps,method);transform=model.transform;runtime=model.runtime|{'api':'GDALCreate'+model.kind+'Transformer/GDAL'+model.kind+'Transform'}
        fit_stats=_statistics(transform(xy),world);inverse=transform(world,True)-xy
        inverse_errors=np.linalg.norm(inverse,axis=1)
        # A small, declared in-hull probe set catches a poor inverse fit; it
        # does not turn polynomial fitting into a global invertibility proof.
        hull=MultiPoint(xy).convex_hull
        probes=np.array([[x,y] for x in np.linspace(xy[:,0].min(),xy[:,0].max(),5) for y in np.linspace(xy[:,1].min(),xy[:,1].max(),5) if hull.covers(Point(x,y))])
        probe_error=np.linalg.norm(transform(transform(probes),True)-probes,axis=1)
        inverse_stats={'rms_pixel_error':float(np.sqrt(np.mean(inverse_errors**2))),'max_pixel_error':float(inverse_errors.max()),
            'in_hull_probes':len(probes),'probe_roundtrip_max_pixel_error':float(probe_error.max())}
        if fit_stats['max_world_error']>max_error:raise BridgeError('WARP_GCP_AFFINE_RESIDUAL' if method=='affine' else 'WARP_GCP_MODEL_RESIDUAL')
        if max(inverse_errors.max(),probe_error.max())>max_inverse_error:raise BridgeError('WARP_GCP_INVERSE_RESIDUAL: increase model quality or explicitly choose a suitable pixel tolerance')
        independent=None
        if checked:
            pixels=np.array([c['pixel'] for c in checked]);expected=np.array([c['world'] for c in checked])
            independent={'count':len(checked),**_statistics(transform(pixels),expected)}
            if max_checkpoint_error is not None and independent['max_world_error']>max_checkpoint_error:raise BridgeError('WARP_GCP_CHECKPOINT_RESIDUAL')
        return {'model':method,'control_points':len(gcps),'controls':[{'pixel':[float(p.col),float(p.row)],'world':[float(p.x),float(p.y)]} for p in gcps],
            'withheld_points':checked,'fit':fit_stats,'inverse':inverse_stats,'independent_checkpoints':independent,
            'world_error_units':'source GCP CRS axes; not assumed metres','runtime':runtime,
            'ground_accuracy':'independent supplied checkpoint residuals only; fitting residual alone is not accuracy' if independent else 'NOT_VERIFIED: no independent checkpoints supplied',
            'inverse_scope':'controls and declared in-hull5x5 probes; no global invertibility guarantee'}
    finally:
        if model is not None:model.close()
