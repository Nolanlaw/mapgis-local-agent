"""Explicit AOI supervised classification and disjoint held-out validation."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated,Literal
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictFloat,StrictInt
from shapely.geometry import Polygon

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .native_raster import _raster_sources,_snapshot_raster,raster_info,_native_mask_profile

Point=Annotated[list[StrictFloat],Field(min_length=2,max_length=2)]
Ring=Annotated[list[Point],Field(min_length=4,max_length=4096)]


class ClassAOI(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    class_id:StrictInt=Field(ge=1,le=254)
    name:str=Field(default='',max_length=120)
    polygon:list[Ring]=Field(min_length=1,max_length=16,description='Closed outer ring followed by optional closed holes, in the declared pixel or map coordinate frame.')


class ClassificationParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    method:Literal['minimum_distance','mahalanobis','parallelepiped']
    bands:list[StrictInt]=Field(min_length=1,max_length=8)
    training:list[ClassAOI]=Field(min_length=2,max_length=256)
    validation:list[ClassAOI]=Field(default_factory=list,max_length=256)
    aoi_space:Literal['pixel','map']='pixel'
    aoi_crs:str|None=None
    standardize:StrictBool=False
    covariance_ridge:StrictFloat=Field(default=0.,ge=0)
    max_distance:StrictFloat|None=Field(default=None,gt=0)
    ambiguity:Literal['unclassified','lowest_id']='unclassified'


def _aoi_labels(items,ds,p):
    labels=np.zeros(ds.shape,dtype='uint8')
    inverse=~ds.transform if p.aoi_space=='map' else None
    for item in items:
        rings=item.polygon
        if any(ring[0]!=ring[-1] for ring in rings):raise BridgeError('CLASSIFICATION_AOI_GEOMETRY: all rings must be explicitly closed')
        if inverse is not None:rings=[[list(inverse*tuple(point)) for point in ring] for ring in rings]
        geometry=Polygon(rings[0],rings[1:])
        if not geometry.is_valid or geometry.is_empty or geometry.area<=0:raise BridgeError('CLASSIFICATION_AOI_GEOMETRY: valid positive-area polygons required')
        x0,y0,x1,y1=geometry.bounds
        if x0< -1e-8 or y0< -1e-8 or x1>ds.width+1e-8 or y1>ds.height+1e-8:
            raise BridgeError('CLASSIFICATION_AOI_BOUNDS: polygons must lie within the image footprint')
        selected=geometry_mask([geometry.__geo_interface__],ds.shape,Affine.identity(),invert=True,all_touched=False)
        if not selected.any():raise BridgeError('CLASSIFICATION_AOI_EMPTY: polygon selects no pixels under the declared GDAL centre/edge rule')
        if np.any(selected&(labels!=0)&(labels!=item.class_id)):
            raise BridgeError('CLASSIFICATION_AOI_CONFLICT: different classes select the same pixels')
        labels[selected]=item.class_id
    return labels


def _fit(data,valid,training,p):
    ids=sorted({a.class_id for a in p.training})
    if not 2<=len(ids)<=32:raise BridgeError('CLASSIFICATION_CLASSES:2..32 explicit classes required')
    selected=data[:,(training>0)&valid].T
    with np.errstate(over='ignore',invalid='ignore'):
        centre=selected.mean(axis=0) if p.standardize else np.zeros(len(p.bands))
        scale=selected.std(axis=0,ddof=0) if p.standardize else np.ones(len(p.bands))
    scale[scale==0]=1
    if not np.isfinite(centre).all() or not np.isfinite(scale).all():raise BridgeError('CLASSIFICATION_NUMERIC_RANGE: explicitly rescale features')
    models=[]
    for class_id in ids:
        samples=data[:,(training==class_id)&valid].T
        if not len(samples):raise BridgeError('CLASSIFICATION_TRAINING_EMPTY: each class needs jointly valid training pixels')
        with np.errstate(over='ignore',invalid='ignore'):
            values=(samples-centre)/scale;mean=values.mean(axis=0)
        if not np.isfinite(values).all() or not np.isfinite(mean).all():raise BridgeError('CLASSIFICATION_NUMERIC_RANGE: explicitly rescale features')
        names={a.name for a in p.training if a.class_id==class_id and a.name}
        if len(names)>1:raise BridgeError('CLASSIFICATION_CLASS_NAME: one explicit name per class ID')
        model={'class_id':class_id,'name':next(iter(names),'class '+str(class_id)),
            'training_pixels':len(samples),'mean':mean.tolist(),'minimum':values.min(axis=0).tolist(),'maximum':values.max(axis=0).tolist()}
        if p.method=='mahalanobis':
            if len(samples)<2:raise BridgeError('CLASSIFICATION_COVARIANCE_SAMPLES: at least two training observations per class required')
            with np.errstate(over='ignore',invalid='ignore'):
                covariance=np.atleast_2d(np.cov(values,rowvar=False,ddof=1))
                regularized=covariance+p.covariance_ridge*np.eye(len(p.bands))
            if not np.isfinite(regularized).all():raise BridgeError('CLASSIFICATION_NUMERIC_RANGE: covariance is not finite')
            rank=int(np.linalg.matrix_rank(covariance))
            if rank<len(p.bands) and p.covariance_ridge==0:
                raise BridgeError('CLASSIFICATION_COVARIANCE_SINGULAR: use independent features or an explicit positive covariance_ridge')
            try:cholesky=np.linalg.cholesky(regularized)
            except np.linalg.LinAlgError as exc:
                raise BridgeError('CLASSIFICATION_COVARIANCE_SINGULAR: use independent features or an explicit positive covariance_ridge') from exc
            model.update(covariance=covariance.tolist(),covariance_rank=rank,cholesky=cholesky.tolist())
        models.append(model)
    return {'method':p.method,'classes':models,'feature_center':centre.tolist(),'feature_scale':scale.tolist(),
        'standardize':p.standardize,'covariance_ridge':p.covariance_ridge,'covariance_ddof':1,
        'covariance_policy':'class-specific covariance plus explicit ridge*identity in transformed feature units; Cholesky solve; no determinant or priors',
        'distance_policy':'Euclidean for minimum_distance; square root of class-specific Mahalanobis quadratic form for mahalanobis',
        'box_policy':'closed per-band training min/max intersection; overlapping boxes use explicit ambiguity policy',
        'max_distance':p.max_distance,'ambiguity':p.ambiguity,'aoi_space':p.aoi_space,'aoi_crs':p.aoi_crs,
        'aoi_pixel_rule':'GDAL rasterize all_touched=False, pixel centre/default edge rule; same-class AOIs unioned',
        'tie_policy':'exact equal minimum distances use the declared ambiguity policy; class IDs sorted ascending',
        'labels':'1..254 caller class IDs;0 valid unclassified/rejected;255 NoData',
        'semantics':'caller-provided training names, no inferred geological identity; assistant algorithms, not proprietary MapGIS classifier equivalence'}


def _predict(data,valid,model,p):
    observations=data[:,valid].T
    ids=np.array([c['class_id'] for c in model['classes']],dtype='uint8')
    predictions=np.zeros(len(observations),dtype='uint8');ambiguous=0
    for start in range(0,len(observations),100000):
        with np.errstate(over='ignore',invalid='ignore'):
            values=(observations[start:start+100000]-np.array(model['feature_center']))/np.array(model['feature_scale'])
            if p.method=='parallelepiped':
                matches=np.array([np.all((values>=c['minimum'])&(values<=c['maximum']),axis=1) for c in model['classes']])
                count=matches.sum(axis=0);chosen=np.argmax(matches,axis=0);accepted=count>0;ties=count>1
            else:
                distances=[]
                for item in model['classes']:
                    delta=(values-np.array(item['mean'])).T
                    if p.method=='mahalanobis':delta=np.linalg.solve(np.array(item['cholesky']),delta)
                    distances.append(np.einsum('ij,ij->j',delta,delta))
                distances=np.array(distances)
                if not np.isfinite(distances).all():raise BridgeError('CLASSIFICATION_NUMERIC_RANGE: distance overflow; explicitly rescale features')
                chosen=np.argmin(distances,axis=0);minimum=distances[chosen,np.arange(len(values))]
                accepted=np.ones(len(values),dtype=bool) if p.max_distance is None else np.sqrt(minimum)<=p.max_distance
                ties=(distances==minimum).sum(axis=0)>1
        if not np.isfinite(values).all():raise BridgeError('CLASSIFICATION_NUMERIC_RANGE: explicitly rescale features')
        ambiguous+=int(ties.sum())
        if p.ambiguity=='unclassified':accepted&=~ties
        predictions[start:start+len(values)]=np.where(accepted,ids[chosen],0)
    output=np.full(valid.shape,255,dtype='uint8');output[valid]=predictions
    return output,ambiguous


def _validate_predictions(truth,output,valid,model):
    ids=[c['class_id'] for c in model['classes']];prediction_ids=[0,*ids]
    selected=(truth>0)&valid
    if not selected.any():raise BridgeError('CLASSIFICATION_VALIDATION_EMPTY: validation AOIs need jointly valid pixels')
    if set(map(int,np.unique(truth[truth>0])))-set(ids):raise BridgeError('CLASSIFICATION_VALIDATION_CLASS: validation classes must be defined by training AOIs')
    matrix=np.array([[int(np.count_nonzero(selected&(truth==actual)&(output==prediction))) for prediction in prediction_ids] for actual in ids])
    total=int(matrix.sum());correct=sum(int(matrix[i,i+1]) for i in range(len(ids)));per_class=[]
    for i,class_id in enumerate(ids):
        tp=int(matrix[i,i+1]);actual=int(matrix[i].sum());predicted=int(matrix[:,i+1].sum())
        per_class.append({'class_id':class_id,'support':actual,'precision':tp/predicted if predicted else None,
            'recall':tp/actual if actual else None,'f1':2*tp/(actual+predicted) if actual+predicted else None})
    return {'status':'VERIFIED_ON_DISJOINT_AOI_PIXELS','class_ids':ids,'prediction_ids':prediction_ids,
        'rows':'provided validation truth','columns':'predicted IDs including valid rejected0','confusion_matrix':matrix.tolist(),
        'total_pixels':total,'overall_accuracy':correct/total,'per_class':per_class,
        'excluded_missing_pixels':int(np.count_nonzero((truth>0)&~valid)),
        'training_validation_overlap_pixels':0,
        'independence':'disjoint pixel annotations only; no claim of spatially or externally independent field validation'}


def prepare_raster_classification(bridge,file,expected_sha256,parameters,template,request_id,expected_members=None):
    p=parameters if isinstance(parameters,ClassificationParameters) else ClassificationParameters.model_validate(parameters)
    source=Path(file).resolve(strict=True);sources=_raster_sources(source,expected_sha256,expected_members)
    if set(template)!={'name'} or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower() not in {'.tif','.tiff'}:
        raise BridgeError('CLASSIFICATION_TEMPLATE: new GeoTIFF basename required')
    if p.method!='mahalanobis' and 'covariance_ridge' in p.model_fields_set:raise BridgeError('CLASSIFICATION_PARAMETERS: covariance_ridge belongs only to mahalanobis')
    if p.method=='parallelepiped' and p.max_distance is not None:raise BridgeError('CLASSIFICATION_PARAMETERS: boxes do not use max_distance')
    if p.aoi_space=='pixel' and p.aoi_crs is not None:raise BridgeError('CLASSIFICATION_AOI_CRS: pixel AOIs cannot carry a map CRS')
    with rasterio.open(source) as ds:
        if len(set(p.bands))!=len(p.bands) or min(p.bands)<1 or max(p.bands)>ds.count:raise BridgeError('CLASSIFICATION_BANDS: unique existing one-based bands required')
        if ds.width*ds.height*len(p.bands)>16000000:raise BridgeError('CLASSIFICATION_MEMORY_LIMIT: crop/tile before classification')
    payload={'operation':'raster.supervised','file':str(source),'expected_sha256':expected_sha256,
        'parameters':p.model_dump(),'explicit_parameters':sorted(p.model_fields_set),'template':template,'classification_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        snapshot=_snapshot_raster(source,sources,stage/'input')
        with rasterio.open(snapshot) as ds:
            if p.aoi_space=='map':
                if p.aoi_crs is None or ds.crs is None or rasterio.crs.CRS.from_user_input(p.aoi_crs)!=ds.crs:
                    raise BridgeError('CLASSIFICATION_AOI_CRS: explicit AOI CRS must equal the raster CRS; reproject AOIs separately')
                if ds.gcps[0]:raise BridgeError('CLASSIFICATION_AOI_GCP: rectify GCP imagery before using map AOIs; pixel AOIs remain available')
            data=ds.read(p.bands,out_dtype='float64');masks=ds.read_masks(p.bands)
            if np.any((masks!=0)&(masks!=255)):raise BridgeError('CLASSIFICATION_BINARY_MASK: explicitly composite partial alpha first')
            valid=np.all(masks==255,axis=0)
            if not valid.any() or not np.isfinite(data[:,valid]).all():raise BridgeError('CLASSIFICATION_FINITE_PIXELS: finite jointly valid observations required')
            training=_aoi_labels(p.training,ds,p);validation=_aoi_labels(p.validation,ds,p)
            if np.any((training>0)&(validation>0)):raise BridgeError('CLASSIFICATION_LEAKAGE: training and validation AOIs select overlapping pixels')
            model=_fit(data,valid,training,p);output,ambiguous=_predict(data,valid,model,p)
            report=_validate_predictions(validation,output,valid,model) if p.validation else {'status':'NOT_PROVIDED','overall_accuracy':None}
            metrics={'valid_pixels':int(valid.sum()),'masked_pixels':int(valid.size-valid.sum()),
                'unclassified_pixels':int(np.count_nonzero(output==0)),'ambiguous_pixels':ambiguous,
                'class_counts':{str(c['class_id']):int(np.count_nonzero(output==c['class_id'])) for c in model['classes']}}
            files=stage/'files';files.mkdir();target=files/template['name'];gcps,gcp_crs=ds.gcps
            profile={'driver':'GTiff','width':ds.width,'height':ds.height,'count':1,'dtype':'uint8',
                'nodata':255,'transform':ds.transform,'crs':ds.crs,'compress':'deflate'}
            for path,array,description in [(target,output,'CLASS_ID'),(files/'training-aoi.tif',np.where(valid,training,255).astype('uint8'),'TRAINING_AOI')]+(
                    [(files/'validation-aoi.tif',np.where(valid,validation,255).astype('uint8'),'VALIDATION_AOI')] if p.validation else []):
                with rasterio.open(path,'w',**profile) as saved:
                    saved.write(array,1);saved.set_band_description(1,description)
                    if gcps:saved.gcps=(gcps,gcp_crs)
                    saved.update_tags(AREA_OR_POINT=ds.tags().get('AREA_OR_POINT','Area'))
        with rasterio.open(target) as check:
            if not np.array_equal(check.read(1),output) or not np.array_equal(check.read_masks(1)>0,valid):raise BridgeError('CLASSIFICATION_SAVED_DIFF')
        info=raster_info(target);_native_mask_profile(target,info)
        metadata={'schema':'mapgis-agent/raster-supervised/1','sources':sources,'parameters':p.model_dump(),
            'model':model,'validation':report,'metrics':metrics,'raster':info,
            'scale_policy':'raw stored features; optional standardization fitted only on training AOI samples; no implicit radiometric calibration'}
        (files/'model.json').write_text(json_text(metadata),encoding='utf8')
        (files/'validation.json').write_text(json_text(report),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'NumPy class statistics/Cholesky, Rasterio/GDAL AOIs',
            'model':model,'validation':report,'metrics':metrics,'raster':info,'source_preserved':True,
            'native_reopen':'separate actual native MSI conversion required; not inferred'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
