"""Cartesian control-point fits using existing NumPy/SciPy and affine code.

Homogeneous column-vector matrices define every result. Similarity rotations
are proper active rotations; no angular/geodetic coordinates are inferred.
"""
from __future__ import annotations
import json,math,warnings
import numpy as np
import scipy
from scipy.spatial.transform import Rotation
import pyproj
from .core import BridgeError,json_text,sha
from .raster import GroundControlPoint,fit_affine,AffineFitError

MODELS={'translation2d':(2,1,2),'similarity2d':(2,2,4),'affine2d':(2,3,6),
        'translation3d':(3,1,3),'similarity3d':(3,3,7)}


def _coordinates(value,dimension):
    if not isinstance(value,(list,tuple)) or len(value)!=dimension or any(type(v) not in (int,float) or not math.isfinite(v) for v in value):
        raise BridgeError('CONTROL_COORDINATES: finite Cartesian pairs/triples required; no bool or inferred Z')
    return value


def _pairs(values,dimension,minimum=0):
    if not isinstance(values,list) or not minimum<=len(values)<=1000:raise BridgeError('CONTROL_COUNT: insufficient controls or more than1000 pairs')
    source=[];target=[]
    for point in values:
        if not isinstance(point,dict) or set(point)!={'source','target'}:raise BridgeError('CONTROL_PAIRS: explicit source/target coordinates required')
        source.append(_coordinates(point['source'],dimension));target.append(_coordinates(point['target'],dimension))
    return np.asarray(source,dtype=float).reshape(-1,dimension),np.asarray(target,dtype=float).reshape(-1,dimension)


def _statistics(predicted,expected):
    residual=predicted-expected;lengths=np.linalg.norm(residual,axis=1)
    return {'count':len(expected),'rms_error':float(np.sqrt(np.mean(lengths**2))),
        'max_error':float(lengths.max()),'residuals':residual.tolist()}


def _apply(points,linear,translation):return points@linear.T+translation


def _proj_angles(rotation):
    # Verify the installed PROJ convention using basis vectors. This avoids
    # treating Euler order or a manual from another release as executable proof.
    fitted=Rotation.from_matrix(rotation)
    for sequence in ('XYZ','xyz'):
        with warnings.catch_warnings(record=True) as messages:
            warnings.simplefilter('always');angles=fitted.as_euler(sequence,degrees=True)*3600
        expression=' '.join(f'+{key}={value:.17g}' for key,value in zip(('rx','ry','rz'),angles))
        operation=pyproj.Transformer.from_pipeline('+proj=helmert '+expression+' +convention=position_vector +exact')
        actual=np.asarray(operation.transform(*np.eye(3)))
        if np.max(np.abs(actual-rotation))<=64*np.finfo(float).eps:return angles,sequence,bool(messages)
    raise BridgeError('CONTROL_PROJ_EXPORT_CONVENTION: no verified exact Euler representation in the installed PROJ runtime')


def fit_control_transform(model,controls,coordinate_frame,max_fit_error,*,checkpoints=None,max_checkpoint_error=None,points=None,direction='forward'):
    if not isinstance(model,str) or model not in MODELS:raise BridgeError('CONTROL_MODEL: translation2d, similarity2d, affine2d, translation3d or similarity3d required')
    dimension,minimum,parameters=MODELS[model]
    frame=coordinate_frame
    if not isinstance(frame,dict) or set(frame)-{'kind','unit','axis_order','source_name','target_name'} or not {'kind','unit','axis_order'}<=set(frame):raise BridgeError('CONTROL_FRAME')
    if any(not isinstance(frame[k],str) for k in ('kind','unit','axis_order')):raise BridgeError('CONTROL_FRAME')
    kinds={'planar'} if dimension==2 else {'cartesian','geocentric'}
    if frame['kind'] not in kinds or frame['unit'] not in {'metre','millimetre','centimetre','foot','inch','unknown'} or frame['axis_order']!=('xy' if dimension==2 else 'xyz') or (frame['kind']=='geocentric' and frame['unit']!='metre'):
        raise BridgeError('CONTROL_FRAME: common explicit Cartesian units and axis order required; longitude/latitude are not Cartesian controls')
    if any(not isinstance(frame[k],str) or not frame[k].strip() or len(frame[k])>200 for k in ('source_name','target_name') if k in frame):raise BridgeError('CONTROL_FRAME_NAME')
    for value,label in [(max_fit_error,'CONTROL_FIT_TOLERANCE'),(max_checkpoint_error,'CONTROL_CHECKPOINT_TOLERANCE')]:
        if label=='CONTROL_CHECKPOINT_TOLERANCE' and value is None:continue
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise BridgeError(label+': positive finite tolerance in declared coordinate units required')
    x,y=_pairs(controls,dimension,minimum);checks=[] if checkpoints is None else checkpoints
    cx,cy=_pairs(checks,dimension)
    identities=set(map(tuple,x));heldout=set()
    if len(identities)!=len(x):raise BridgeError('CONTROL_DUPLICATE: source control coordinates must be unique')
    for p in cx:
        identity=tuple(p)
        if identity in identities:raise BridgeError('CONTROL_CHECKPOINT_REUSED: a fitting control is not an independent check')
        if identity in heldout:raise BridgeError('CONTROL_CHECKPOINT_DUPLICATE')
        heldout.add(identity)
    if max_checkpoint_error is not None and not len(cx):raise BridgeError('CONTROL_CHECKPOINTS_REQUIRED')
    if direction not in {'forward','inverse'}:raise BridgeError('CONTROL_DIRECTION')
    points=[] if points is None else points
    if not isinstance(points,list) or len(points)>100000:raise BridgeError('CONTROL_QUERY_COUNT')
    query=np.asarray([_coordinates(p,dimension) for p in points],dtype=float).reshape(-1,dimension)
    try:
        with np.errstate(over='raise',invalid='raise',divide='raise'):
            rotation=np.eye(dimension);scale=1.;linear=np.eye(dimension);rank=0
            if model.startswith('translation'):
                translation=(y-x).mean(axis=0);solver='NumPy mean of paired Cartesian displacements'
                rank=int(np.linalg.matrix_rank(x-x[0])) if len(x)>1 else 0
            else:
                xmean=x.mean(axis=0);ymean=y.mean(axis=0);xcenter=x-xmean;ycenter=y-ymean
                xspan=float(np.max(np.abs(xcenter)));yspan=float(np.max(np.abs(ycenter)))
                if xspan==0 or yspan==0:raise BridgeError('CONTROL_DEGENERATE: nonzero spread required in both frames')
                xn=xcenter/xspan;yn=ycenter/yspan;rank=int(np.linalg.matrix_rank(xn));target_rank=int(np.linalg.matrix_rank(yn))
                if rank<(2 if dimension==3 or model=='affine2d' else 1) or target_rank<(2 if dimension==3 or model=='affine2d' else 1):raise BridgeError('CONTROL_DEGENERATE: requested model is not identifiable')
                if model=='similarity2d':
                    design=np.empty((2*len(x),2));design[0::2]=np.column_stack((xn[:,0],-xn[:,1]));design[1::2]=np.column_stack((xn[:,1],xn[:,0]))
                    coefficients,_,fitted_rank,_=np.linalg.lstsq(design,yn.reshape(-1),rcond=None)
                    if fitted_rank!=2:raise BridgeError('CONTROL_DEGENERATE')
                    a,b=coefficients*yspan/xspan;linear=np.array([[a,-b],[b,a]]);scale=float(math.hypot(a,b))
                    if scale==0:raise BridgeError('CONTROL_SINGULAR')
                    rotation=linear/scale;translation=ymean-linear@xmean;solver='NumPy centered least squares, proper2D similarity'
                elif model=='affine2d':
                    fitted=fit_affine([GroundControlPoint(*a,*b) for a,b in zip(xn,yn)])
                    linear=np.array([[fitted.a,fitted.b],[fitted.d,fitted.e]])*yspan/xspan
                    translation=ymean+yspan*np.array([fitted.c,fitted.f])-linear@xmean
                    solver='Existing vendored stratigraphic_amenity fit_affine on centered, normalized coordinates'
                else:
                    fitted,_=Rotation.align_vectors(yn,xn);rotation=fitted.as_matrix()
                    scale=float(np.sum(yn*fitted.apply(xn))/np.sum(xn*xn)*yspan/xspan)
                    if scale<=0:raise BridgeError('CONTROL_SINGULAR: positive scale required')
                    linear=scale*rotation;translation=ymean-linear@xmean;solver='SciPy Rotation.align_vectors Kabsch plus least-squares positive uniform scale'
            if np.linalg.matrix_rank(linear)!=dimension:raise BridgeError('CONTROL_SINGULAR')
            matrix=np.eye(dimension+1);matrix[:dimension,:dimension]=linear;matrix[:dimension,-1]=translation;inverse=np.linalg.inv(matrix)
            predicted=_apply(x,linear,translation);fit=_statistics(predicted,y)
            if fit['max_error']>max_fit_error:raise BridgeError('CONTROL_FIT_RESIDUAL: fit exceeds declared coordinate-unit tolerance')
            independent=_statistics(_apply(cx,linear,translation),cy) if len(cx) else None
            if independent and max_checkpoint_error is not None and independent['max_error']>max_checkpoint_error:raise BridgeError('CONTROL_CHECKPOINT_RESIDUAL')
            reverse=_apply(predicted,inverse[:dimension,:dimension],inverse[:dimension,-1]);roundtrip=float(np.max(np.linalg.norm(reverse-x,axis=1)))
            requested=matrix if direction=='forward' else inverse;output=_apply(query,requested[:dimension,:dimension],requested[:dimension,-1])
            if any(not np.all(np.isfinite(a)) for a in (matrix,inverse,output)):raise BridgeError('CONTROL_NUMERIC_RANGE')
    except (FloatingPointError,np.linalg.LinAlgError,AffineFitError) as exc:raise BridgeError('CONTROL_NUMERIC_RANGE: model cannot be solved reliably for these coordinates') from exc
    values={'translation':translation.tolist()};native_transform=None;native_chain=[];pipeline=None
    if model!='affine2d':
        values|={'scale':scale,'rotation_matrix':rotation.tolist()}
        if dimension==2:
            angle=math.degrees(math.atan2(rotation[1,0],rotation[0,0]));values['rotation_degrees']=angle
            native_transform={'translation':translation.tolist(),'scale':[scale,scale],'rotation_degrees':angle,
                'order':['scale','rotate','translate'],'coordinate_frame':'source'}
            native_chain=[native_transform]
            if frame['unit']=='metre':pipeline=f'+proj=helmert +x={translation[0]:.17g} +y={translation[1]:.17g} +s={scale:.17g} +theta={-angle*3600:.17g}'
        else:
            fitted=Rotation.from_matrix(rotation);values['rotation_vector_radians']=fitted.as_rotvec().tolist()
            if frame['unit']=='metre':
                angles,sequence,gimbal=_proj_angles(rotation)
                values|={'proj_euler_sequence':sequence,'proj_euler_gimbal_lock':gimbal}
                pipeline=('+proj=helmert '+ ' '.join(f'+{k}={v:.17g}' for k,v in zip(('x','y','z','s','rx','ry','rz'),[*translation,(scale-1.)*1e6,*angles]))+' +convention=position_vector +exact')
    else:
        values['linear_matrix']=linear.tolist()
        # Any nonsingular2D affine is two proper rotations with a signed
        # diagonal scale between them. Reuse existing native Transform calls;
        # no new geometry encoder or shear approximation is needed.
        u,singular,v=np.linalg.svd(linear);left=-1. if np.linalg.det(u)<0 else 1.;right=-1. if np.linalg.det(v)<0 else 1.
        u=u@np.diag([1.,left]);v=np.diag([1.,right])@v
        for rotation,scales,offset in [(v,[1.,1.],[0.,0.]),(u,[float(singular[0]),float(singular[1]*left*right)],translation.tolist())]:
            native_chain.append({'translation':offset,'scale':scales,'rotation_degrees':math.degrees(math.atan2(rotation[1,0],rotation[0,0])),
                'order':['scale','rotate','translate'],'coordinate_frame':'source'})
    result={'schema':'mapgis-agent/control-transform/1','model':model,'dimension':dimension,'coordinate_frame':dict(frame),
        'parameters':values,'matrix':matrix.tolist(),'inverse_matrix':inverse.tolist(),'matrix_convention':'homogeneous column vectors: target = matrix @ [source,1]',
        'fit':fit,'degrees_of_freedom':dimension*len(x)-parameters,'normalized_source_control_rank':rank,
        'independent_checkpoints':independent,'inverse_roundtrip_max_error':roundtrip,'linear_condition_number':float(np.linalg.cond(linear)),
        'error_units':frame['unit'],'ground_accuracy':'CHECKPOINT_ERRORS_REPORTED: supplied withheld observations; no certified datum or measurement accuracy implied' if independent else 'NOT_VERIFIED: no independent checkpoints supplied',
        'controls':json.loads(json_text(controls)),'checkpoints':json.loads(json_text(checks)),'points':output.tolist(),'direction':direction,
        'native_subset_transform':native_transform,'native_subset_transforms':native_chain,'proj_pipeline':pipeline,
        'runtime':{'solver':solver,'numpy':np.__version__,'scipy':scipy.__version__,'pyproj':pyproj.__version__,'proj':pyproj.proj_version_str},
        'scope':'Cartesian fitted model only. Not a native MapGIS transformation-file writer, a global extrapolation-accuracy guarantee or geographic-degree transformation.2D native_subset_transform retains existing metadata/styles and acts in the declared source units.3D results do not establish native3D-file editing.'}
    if dimension==2:result['affine_matrix']=[*linear[0],*linear[1],*translation]
    result['control_fingerprint']=sha(json_text({'model':model,'controls':controls,'coordinate_frame':frame}).encode('utf8'))
    return result
