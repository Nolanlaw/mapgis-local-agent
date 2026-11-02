"""Bounded image analysis using existing SciPy/NumPy; no desktop or source writes."""
from __future__ import annotations
import json,math
from pathlib import Path
from typing import Literal
import numpy as np
import rasterio
from rasterio.transform import Affine
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictFloat,StrictInt
from scipy import ndimage
from scipy.cluster.vq import kmeans2,vq,ClusterError

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .native_raster import _raster_sources,_snapshot_raster,raster_info,_native_mask_profile
from .raster_isodata import IsodataParameters,cluster_image

COLOR_OPERATIONS={'rgb_to_hsv','hsv_to_rgb','rgb_to_hls','hls_to_rgb'}
FFT_OPERATIONS={'fft','ifft'}


class RasterReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    file:str=Field(min_length=1)
    expected_sha256:str=Field(pattern='^[0-9a-f]{64}$')
    expected_members:dict[str,str]|None=None

class PaletteColor(BaseModel):
    model_config=ConfigDict(extra='forbid')
    value:StrictInt
    rgb:list[StrictInt]=Field(min_length=3,max_length=3)


class ImageParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    operation:Literal['mean','median','gaussian','sobel','normalized_difference','linear_combination','stretch','threshold','morphology','kmeans','colorize','pca','rgb_to_hsv','hsv_to_rgb','rgb_to_hls','hls_to_rgb','fft','ifft','sieve','isodata']
    bands:list[StrictInt]=Field(min_length=1,max_length=8)
    radius:StrictInt=Field(default=1,ge=1,le=15)
    sigma:StrictFloat=Field(default=1.,gt=0,le=30)
    edge:Literal['complete','reflect']='complete'
    weights:list[StrictFloat]|None=None
    offset:StrictFloat=0.
    percentiles:list[StrictFloat]=Field(default_factory=lambda:[2.,98.])
    threshold:StrictFloat|None=None
    morphology:Literal['erode','dilate','open','close']='open'
    iterations:StrictInt|None=Field(default=None,ge=1,le=200)
    classes:StrictInt=Field(default=5,ge=2,le=32)
    sample_size:StrictInt=Field(default=100000,ge=100,le=200000)
    seed:StrictInt=Field(default=0,ge=0,le=4294967295)
    standardize:StrictBool=False
    palette:list[PaletteColor]|None=None
    components:StrictInt|None=Field(default=None,ge=1,le=8)
    whiten:StrictBool=False
    rgb_maximum:StrictFloat|None=Field(default=None,gt=0,description='Required for rgb_to_hsv/rgb_to_hls/hsv_to_rgb/hls_to_rgb: explicit raw RGB maximum, e.g.1,254,255 or65535. HSV/HLS channels use0..1; hue is turns with1 aliasing0. No implicit calibration or stretch.')
    fft_norm:Literal['backward','forward','ortho']='ortho'
    fft_shift:StrictBool=False
    fft_reference:RasterReference|None=Field(default=None,description='Optional ifft-only original spatial grid, guarded by complete member hashes. Frequency output has no spatial CRS; inverse without a reference stays in pixel-index coordinates.')
    minimum_region_pixels:StrictInt|None=Field(default=None,ge=1,le=16000000,
        description='Sieve-only explicit minimum connected equal-label region size. Smaller regions merge into their largest eligible neighbour, under GDAL semantics; NoData is a barrier.')
    connectivity:StrictInt=Field(default=4,description='Sieve-only4 or8 neighbouring pixels. Zero is a valid class. Integer labels must fit signed32-bit.')
    isodata:IsodataParameters|None=None


def _validate(p,count):
    if len(set(p.bands))!=len(p.bands) or min(p.bands)<1 or max(p.bands)>count:raise BridgeError('IMAGE_BANDS: unique existing one-based bands required')
    common={'operation','bands'}
    extras={'mean':{'radius','edge'},'median':{'radius','edge'},'gaussian':{'radius','sigma','edge'},'sobel':{'edge'},
        'normalized_difference':set(),'linear_combination':{'weights','offset'},'stretch':{'percentiles'},
        'threshold':{'threshold'},'morphology':{'threshold','morphology','radius','iterations','edge'},
        'kmeans':{'classes','iterations','sample_size','seed','standardize'},'colorize':{'palette'},
        'pca':{'components','standardize','whiten'},**{name:{'rgb_maximum'} for name in COLOR_OPERATIONS},
        'fft':{'fft_norm','fft_shift'},'ifft':{'fft_norm','fft_shift','fft_reference'},
        'sieve':{'minimum_region_pixels','connectivity'},
        'isodata':{'isodata','iterations','sample_size','seed','standardize'}}
    if p.model_fields_set-common-extras[p.operation]:raise BridgeError('IMAGE_UNEXPECTED_PARAMETERS: remove options belonging to another operation')
    if p.operation=='normalized_difference' and len(p.bands)!=2:raise BridgeError('IMAGE_TWO_BANDS_REQUIRED')
    if p.operation in {'threshold','morphology'} and (len(p.bands)!=1 or p.threshold is None):raise BridgeError('IMAGE_THRESHOLD: one band and explicit threshold required')
    if p.operation=='morphology' and p.iterations is not None and p.iterations>10:raise BridgeError('IMAGE_MORPHOLOGY_ITERATIONS: at most 10 explicit passes')
    if p.operation=='linear_combination' and (p.weights is None or len(p.weights)!=len(p.bands)):raise BridgeError('IMAGE_WEIGHTS: one explicit weight per selected band')
    if p.operation=='stretch' and (len(p.percentiles)!=2 or not 0<=p.percentiles[0]<p.percentiles[1]<=100):raise BridgeError('IMAGE_PERCENTILES: increasing pair within 0..100 required')
    if p.operation=='colorize' and (len(p.bands)!=1 or not p.palette or len({c.value for c in p.palette})!=len(p.palette)
            or any(v<0 or v>254 for c in p.palette for v in c.rgb)):
        raise BridgeError('IMAGE_PALETTE: one label band and unique value/RGB entries with channels0..254;255 reserved NoData')
    if p.operation=='pca' and p.components is not None and p.components>len(p.bands):raise BridgeError('IMAGE_PCA_COMPONENTS: cannot exceed selected bands')
    if p.operation in COLOR_OPERATIONS and (len(p.bands)!=3 or p.rgb_maximum is None):
        raise BridgeError('IMAGE_COLOR_PARAMETERS: exactly three ordered bands and explicit positive rgb_maximum required')
    if (p.operation=='fft' and len(p.bands) not in (1,2)) or (p.operation=='ifft' and len(p.bands)!=2):
        raise BridgeError('IMAGE_FFT_BANDS: forward requires one real band or ordered real/imaginary pair; inverse requires two ordered real/imaginary bands')
    if p.operation=='sieve' and (len(p.bands)!=1 or p.minimum_region_pixels is None or p.connectivity not in (4,8)):
        raise BridgeError('IMAGE_SIEVE_PARAMETERS: one band, explicit minimum_region_pixels and connectivity4 or8 required')
    if p.operation=='isodata' and p.isodata is None:raise BridgeError('ISODATA_PARAMETERS: explicit split/merge thresholds and minimum cluster pixels required')


def _sieve(data,valid,p):
    from rasterio.features import sieve
    selected=data[0,valid];limits=np.iinfo('int32')
    if np.any(selected!=np.trunc(selected)) or np.any(selected<limits.min) or np.any(selected>limits.max):
        raise BridgeError('IMAGE_SIEVE_LABELS: exact integer labels in signed32-bit range required; no rounding or float32 conversion')
    labels=np.where(valid,data[0],0).astype('int32')
    output=sieve(labels,p.minimum_region_pixels,mask=valid,connectivity=p.connectivity)
    changed=int(np.count_nonzero(output[valid]!=labels[valid]));nodata=int(limits.min)
    if np.any(output[valid]==nodata):
        used=np.unique(output[valid]).astype('int64');gaps=np.flatnonzero(np.diff(used)>1)
        nodata=int(used[gaps[0]]+1) if len(gaps) else int(used[-1]+1)
    output[~valid]=nodata
    return output[None,:,:],float(nodata),{'implementation':'Rasterio/GDAL GDALSieveFilter',
        'rasterio_version':rasterio.__version__,'gdal_version':rasterio.__gdal_version__,
        'minimum_region_pixels':p.minimum_region_pixels,'connectivity':p.connectivity,
        'merge_rule':'replace smaller connected equal-label regions with largest neighbouring region at least as large as the threshold; islands with no eligible neighbour remain unchanged',
        'equal_size_neighbours':'GDAL traversal rule for this recorded version; no spectral/geometric nearest-class rule inferred',
        'nodata_policy':'excluded from region sizes and adjacency, unchanged validity; zero is a normal valid label',
        'changed_valid_pixels':changed,'output_dtype':'int32',
        'reference':'https://gdal.org/en/stable/api/gdal_alg.html#_CPPv415GDALSieveFilter15GDALRasterBandH15GDALRasterBandH15GDALRasterBandHiiPPc16GDALProgressFuncPv'}


def _fft(data,valid,p):
    if not valid.all():raise BridgeError('IMAGE_FFT_MISSING: Fourier transforms require a complete finite grid; explicitly crop or impute separately')
    values=data[0].astype('complex128')
    if len(data)==2:values+=1j*data[1]
    if p.operation=='fft':
        transformed=np.fft.fft2(values,norm=p.fft_norm)
        if p.fft_shift:transformed=np.fft.fftshift(transformed)
    else:
        if p.fft_shift:values=np.fft.ifftshift(values)
        transformed=np.fft.ifft2(values,norm=p.fft_norm)
    output=np.stack((transformed.real,transformed.imag))
    return output,{'implementation':'NumPy complex128 two-dimensional DFT; both components saved as float64',
        'numpy_version':np.__version__,'domain':'frequency' if p.operation=='fft' else 'spatial_complex',
        'normalization':p.fft_norm,'shifted':p.fft_shift,'shape':list(valid.shape),
        'input_components':'real' if len(data)==1 else 'ordered real,imaginary',
        'output_components':['real','imaginary'],'output_dtype':'float64',
        'missing_policy':'reject any missing sample; no implicit zero fill','output_nodata':None,
        'sign':'forward exponent -2*pi*i*(row*k/height+col*l/width); inverse positive',
        'frequency_units':'cycles per pixel on row/column axes; no physical frequency or spatial location implied',
        'frequency_index_rule':'shifted: (index-floor(N/2))/N; standard: (index if index<ceil(N/2) else index-N)/N',
        'boundary':'finite periodic grid; no implicit padding, windowing or resampling',
        'inverse_imaginary':'retained as a full second band; never silently discarded'}


def _color_space(data,valid,p):
    """Vectorized RGB/HSV/HLS in the documented Python colorsys convention.

    Hue is cyclic turns; saturation/lightness/value are normalized. RGB uses
    the caller's raw-sample maximum. No calibration, gamma or spectral mixing.
    """
    forward=p.operation.startswith('rgb_to_')
    selected=data[:,valid]
    maximum=p.rgb_maximum if forward else 1.
    if np.any(selected<0) or np.any(selected>maximum):
        raise BridgeError('IMAGE_COLOR_RANGE: samples must be inside the declared RGB or normalized HSV/HLS gamut')
    def divide(a,b):return np.divide(a,b,out=np.zeros_like(a),where=b!=0)
    if forward:
        red,green,blue=data/p.rgb_maximum
        high=np.maximum(np.maximum(red,green),blue);low=np.minimum(np.minimum(red,green),blue);delta=high-low
        hue=np.select([high==red,high==green],[divide(green-blue,delta),2+divide(blue-red,delta)],default=4+divide(red-green,delta))
        hue=np.mod(hue/6,1);hue[delta==0]=0
        if p.operation=='rgb_to_hsv':
            output=np.stack((hue,divide(delta,high),high));names=['H_turn','S','V'];space='HSV'
        else:
            light=(high+low)/2
            saturation=divide(delta,np.where(light<=.5,high+low,2-high-low))
            output=np.stack((hue,light,saturation));names=['H_turn','L','S'];space='HLS'
    else:
        hue=np.mod(data[0],1)
        if p.operation=='hsv_to_rgb':
            saturation,value=data[1:];sector=np.floor(hue*6).astype('int8');fraction=hue*6-sector
            base=value*(1-saturation);falling=value*(1-saturation*fraction);rising=value*(1-saturation*(1-fraction))
            output=np.stack((np.choose(sector,[value,falling,base,base,rising,value]),
                             np.choose(sector,[rising,value,value,falling,base,base]),
                             np.choose(sector,[base,base,rising,value,value,falling])))
        else:
            light,saturation=data[1:]
            high=np.where(light<=.5,light*(1+saturation),light+saturation-light*saturation);low=2*light-high
            def channel(offset):
                h=np.mod(hue+offset,1)
                return np.select([h<1/6,h<.5,h<2/3],[low+(high-low)*6*h,high,low+(high-low)*(2/3-h)*6],default=low)
            output=np.stack((channel(1/3),channel(0),channel(-1/3)))
        output=np.clip(output,0,1)*p.rgb_maximum;names=['R','G','B'];space='RGB'
    with np.errstate(over='ignore',invalid='ignore'):
        quantized=output.astype('float32')
        errors=[float(np.max(np.abs(a[valid]-b[valid]))) for a,b in zip(output,quantized)]
    details={'implementation':'vectorized NumPy; Python colorsys RGB/HSV/HLS convention','conversion':p.operation,
        'source_rgb_maximum':p.rgb_maximum,'output_space':space,'band_names':names,'hue_unit':'turn; 1 aliases 0',
        'normalized_channels':'HSV/HLS in0..1; hue is cyclic; achromatic forward hue is0',
        'input_gamut':'strict; no clipping or implicit per-band stretch','rgb_policy':'raw samples / explicit maximum; no gamma, colour-profile or scale/offset calibration',
        'inverse_roundoff':'canonical clip to0..1 after valid inverse conversion, before RGB scaling',
        'float32_component_max_abs_error':errors,
        'float32_underflow_values':[int(np.count_nonzero((a[valid]!=0)&(b[valid]==0))) for a,b in zip(output,quantized)],
        'semantics':'colour-space components, not geological or spectral classes'}
    return output,details


def _classify(data,valid,p):
    values=data[:,valid].T;rng=np.random.default_rng(p.seed)
    indexes=np.sort(rng.choice(len(values),min(p.sample_size,len(values)),replace=False));training=values[indexes]
    centre=training.mean(axis=0) if p.standardize else np.zeros(data.shape[0])
    scales=training.std(axis=0) if p.standardize else np.ones(data.shape[0]);scales[scales==0]=1
    normalized=(training-centre)/scales
    if len(np.unique(normalized,axis=0))<p.classes:raise BridgeError('IMAGE_KMEANS_DISTINCT_VALUES: reduce classes; training sample has too few distinct observations')
    try:centroids,labels=kmeans2(normalized,p.classes,iter=p.iterations or 30,minit='++',missing='raise',check_finite=True,rng=rng)
    except ClusterError as exc:raise BridgeError('IMAGE_KMEANS_EMPTY_CLUSTER: change explicit class count/sample/seed; no empty class is silently invented') from exc
    original=centroids*scales+centre;order=sorted(range(len(original)),key=lambda i:tuple(original[i]));remap=np.zeros(p.classes,dtype='uint8')
    for label,old in enumerate(order,1):remap[old]=label
    prediction=np.empty(len(values),dtype='uint8')
    for start in range(0,len(values),250000):
        labels,_=vq((values[start:start+250000]-centre)/scales,centroids,check_finite=True);prediction[start:start+len(labels)]=remap[labels]
    counts=np.bincount(prediction,minlength=p.classes+1)[1:]
    if np.any(counts==0):raise BridgeError('IMAGE_KMEANS_EMPTY_CLASS')
    output=np.zeros(valid.shape,dtype='uint8');output[valid]=prediction
    return output[None,:,:],{'centres_source_units':original[order].tolist(),'class_counts':counts.tolist(),
        'training_pixels':len(training),'iterations':p.iterations or 30,'seed':p.seed,'standardize':p.standardize,
        'training_mean':centre.tolist(),'training_scale':scales.tolist(),'class_order':'lexicographic source-unit centroids; IDs1..K;0=NoData',
        'semantic_labels':'unsupervised numeric clusters; no geological/class meaning inferred'}


def _pca(data,valid,p):
    values=data[:,valid].copy();bands,n=values.shape
    if n<2:raise BridgeError('IMAGE_PCA_PIXELS: at least two jointly valid observations required')
    count=p.components or min(bands,n)
    if count>min(bands,n):raise BridgeError('IMAGE_PCA_COMPONENTS: cannot exceed selected bands or jointly valid observations')
    reference=values[:,0].copy()
    with np.errstate(over='ignore',invalid='ignore'):
        values-=reference[:,None];offset=values.mean(axis=1);values-=offset[:,None]
        scale=values.std(axis=1,ddof=1) if p.standardize else np.ones(bands)
    constant=np.all(values==0,axis=1);scale[scale==0]=1.
    if not np.isfinite(values).all() or not np.isfinite(scale).all():raise BridgeError('IMAGE_PCA_NUMERIC_RANGE: rescale raw samples explicitly')
    values/=scale[:,None]
    try:axes,singular,right=np.linalg.svd(values,full_matrices=False)
    except np.linalg.LinAlgError as exc:raise BridgeError('IMAGE_PCA_SVD_FAILED') from exc
    eigenvalues=singular*singular/(n-1)
    if not np.isfinite(eigenvalues).all():raise BridgeError('IMAGE_PCA_VARIANCE_RANGE')
    tolerance=float(singular[0]*max(values.shape)*np.finfo(float).eps)
    rank=int(np.count_nonzero(singular>tolerance))
    if p.whiten and count>rank:raise BridgeError('IMAGE_PCA_WHITEN_NULL_COMPONENT: retain only independently varying components')
    components=axes[:,:count].T.copy()
    signs=np.array([1. if row[np.argmax(np.abs(row))]>=0 else -1. for row in components])
    components*=signs[:,None]
    whitening=np.sqrt(eigenvalues[:count]) if p.whiten else np.ones(count)
    scores=(right[:count]*singular[:count,None])*signs[:,None]/whitening[:,None]
    output=np.zeros((count,*valid.shape));output[:,valid]=scores
    total=float(np.sum(eigenvalues))
    details={'implementation':'NumPy thin SVD/LAPACK; assistant open backend','numpy_version':np.__version__,'source_bands':p.bands,
        'components':components.tolist(),'mean':(reference+offset).tolist(),'centering_reference':reference.tolist(),
        'centering_offset':offset.tolist(),'centering_formula':'((raw-reference)-offset)/scale',
        'scale':scale.tolist(),'standardize':p.standardize,'whiten':p.whiten,'whitening_scale':whitening.tolist(),
        'eigenvalues':eigenvalues[:count].tolist(),'full_eigenvalue_spectrum':eigenvalues.tolist(),
        'explained_variance_ratio':(eigenvalues[:count]/total if total else np.zeros(count)).tolist(),
        'variance_ddof':1,'training_pixels':n,'effective_rank':rank,'singular_value_rank_tolerance':tolerance,
        'zero_variance_bands':[p.bands[i] for i in range(bands) if constant[i]],
        'component_sign':'largest absolute loading positive; ties use the first selected band',
        'degenerate_axes':bool(np.any(np.isclose(eigenvalues[:-1],eigenvalues[1:],rtol=1e-12,atol=tolerance*tolerance/(n-1)))),
        'basis_scope':'equal-eigenvalue subspaces have no unique individual axes; retain this saved component matrix',
        'semantics':'numeric components; no geological labels inferred'}
    return output,details


def _calculate(data,valid,p):
    operation=p.operation;working=np.where(valid[None,:,:],data,0.);mask=valid.copy();details={};byte=False;nodata=None
    if operation=='sieve':
        output,nodata,details['sieve']=_sieve(data,valid,p)
        return output,mask,nodata,details
    if operation in {'mean','median','gaussian','sobel'}:
        radius=1 if operation=='sobel' else p.radius;size=2*radius+1;mode='constant' if p.edge=='complete' else 'reflect'
        mask=ndimage.minimum_filter(valid.astype('uint8'),size=size,mode=mode,cval=0)>0
        output=[]
        for band in working:
            if operation=='mean':values=ndimage.uniform_filter(band,size=size,mode=mode,cval=0.)
            elif operation=='median':values=ndimage.median_filter(band,size=size,mode=mode,cval=0.)
            elif operation=='gaussian':values=ndimage.gaussian_filter(band,sigma=p.sigma,radius=radius,mode=mode,cval=0.)
            else:values=np.hypot(ndimage.sobel(band,axis=1,mode=mode,cval=0.),ndimage.sobel(band,axis=0,mode=mode,cval=0.))/8.
            output.append(values)
        output=np.array(output);details['neighborhood']={'radius':radius,'edge':p.edge,'missing':'complete valid stencil, no missing-pixel interpolation',
            'sobel_unit':'source value per pixel, normalized kernel/8; not map slope' if operation=='sobel' else None}
    elif operation=='normalized_difference':
        denominator=working[0]+working[1];bad=mask&(denominator==0);details['division_by_zero_pixels']=int(bad.sum());mask&=denominator!=0
        values=np.zeros(mask.shape);np.divide(working[0]-working[1],denominator,out=values,where=mask);output=values[None,:,:]
    elif operation=='linear_combination':output=(np.einsum('b,bhw->hw',np.array(p.weights),working)+p.offset)[None,:,:]
    elif operation=='pca':output,details['pca']=_pca(working,mask,p)
    elif operation in COLOR_OPERATIONS:output,details['color_space']=_color_space(working,mask,p)
    elif operation in FFT_OPERATIONS:output,details['fft']=_fft(working,mask,p)
    elif operation=='isodata':output,details['isodata']=cluster_image(working,mask,p);byte=True;nodata=0
    elif operation=='stretch':
        bands=[];ranges=[]
        for band in working:
            low,high=map(float,np.percentile(band[valid],p.percentiles,method='linear'));ranges.append([low,high])
            bands.append(np.zeros(mask.shape,dtype='uint8') if high==low else np.rint(np.clip((band-low)/(high-low),0,1)*254).astype('uint8'))
        output=np.array(bands);byte=True;nodata=255;details.update(stretch_input_ranges=ranges,output_value_range=[0,254])
    elif operation in {'threshold','morphology'}:
        values=working[0]>=p.threshold
        if operation=='morphology':
            size=2*p.radius+1;mode='constant' if p.edge=='complete' else 'reflect'
            passes=1 if p.morphology in {'erode','dilate'} else 2
            count=p.iterations or 1
            steps={'erode':['min'],'dilate':['max'],'open':['min','max'],'close':['max','min']}[p.morphology]
            for step in steps:
                function=ndimage.minimum_filter if step=='min' else ndimage.maximum_filter
                for _ in range(count):
                    values=function(values,size=size,mode=mode,cval=0);mask=ndimage.minimum_filter(mask.astype('uint8'),size=size,mode=mode,cval=0)>0
            details['neighborhood']={'radius':p.radius,'edge':p.edge,'passes':passes*count,'missing':'valid support required through all passes'}
        output=values.astype('uint8')[None,:,:];byte=True;nodata=255;details['output_value_range']=[0,1]
    elif operation=='colorize':
        labels=set(map(float,np.unique(working[0,valid])));palette={c.value:c.rgb for c in p.palette}
        if labels-set(palette):raise BridgeError('IMAGE_UNMAPPED_LABEL: every valid class must have an explicit RGB entry')
        output=np.zeros((3,*mask.shape),dtype='uint8')
        for label,color in palette.items():output[:,valid&(working[0]==label)]=np.array(color,dtype='uint8')[:,None]
        byte=True;nodata=255;details['palette']=[c.model_dump() for c in p.palette]
    else:
        output,classification=_classify(working,mask,p);byte=True;nodata=0;details['classification']=classification
    if byte:output[:,~mask]=nodata
    else:
        if operation=='pca':
            with np.errstate(over='ignore',invalid='ignore'):
                details['pca']['float32_score_max_abs_error']=[float(np.max(np.abs(b[mask]-b[mask].astype('float32')))) for b in output]
                details['pca']['float32_underflow_values']=[int(np.count_nonzero((b[mask]!=0)&(b[mask].astype('float32')==0))) for b in output]
        dtype=np.dtype('float64' if operation in FFT_OPERATIONS else 'float32')
        with np.errstate(over='ignore',invalid='ignore'):output=output.astype(dtype)
        if np.any(~np.isfinite(output[:,mask])):raise BridgeError(f'IMAGE_NONFINITE_RESULT: values exceed finite {dtype} output range')
        if operation not in FFT_OPERATIONS:
            nodata=dtype.type(np.finfo(dtype).min)
            while np.any(output[:,mask]==nodata):nodata=np.nextafter(nodata,dtype.type(0))
            output[:,~mask]=nodata
    return output,mask,float(nodata) if nodata is not None else None,details


def prepare_raster_processing(bridge,file,expected_sha256,parameters,template,request_id,expected_members=None):
    p=parameters if isinstance(parameters,ImageParameters) else ImageParameters.model_validate(parameters)
    source=Path(file).resolve(strict=True);input_sources=_raster_sources(source,expected_sha256,expected_members)
    reference=None;reference_sources=[]
    if p.fft_reference is not None:
        reference=Path(p.fft_reference.file).resolve(strict=True)
        reference_sources=_raster_sources(reference,p.fft_reference.expected_sha256,p.fft_reference.expected_members)
    sources=list({s['file']:s for s in input_sources+reference_sources}.values())
    if set(template)!={'name'} or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower() not in {'.tif','.tiff'}:raise BridgeError('IMAGE_TEMPLATE: new GeoTIFF basename required')
    with rasterio.open(source) as ds:
        _validate(p,ds.count)
        if ds.width*ds.height*len(p.bands)>16000000:raise BridgeError('IMAGE_MEMORY_LIMIT: crop/tile first; at most16000000 selected band cells per request')
        if p.operation=='sieve' and p.minimum_region_pixels>ds.width*ds.height:
            raise BridgeError('IMAGE_SIEVE_SIZE: region threshold cannot exceed the full raster cell count')
    omitted=set() if p.operation=='pca' else {'components','whiten'}
    if p.operation not in COLOR_OPERATIONS:omitted.add('rgb_maximum')
    if p.operation not in FFT_OPERATIONS:omitted.update({'fft_norm','fft_shift','fft_reference'})
    if p.operation!='sieve':omitted.update({'minimum_region_pixels','connectivity'})
    if p.operation!='isodata':omitted.add('isodata')
    serialized=p.model_dump(exclude=omitted)
    payload={'operation':'raster.processing','file':str(source),'expected_sha256':expected_sha256,'parameters':serialized,
        'explicit_parameters':sorted(p.model_fields_set),'template':template,'processing_protocol':1}
    if p.operation in FFT_OPERATIONS:payload['fft_protocol']=1
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        snapshot=_snapshot_raster(source,input_sources,stage/'input')
        reference_snapshot=_snapshot_raster(reference,reference_sources,stage/'reference') if reference is not None else None
        with rasterio.open(snapshot) as ds:
            data=ds.read(p.bands,out_dtype='float64');masks=ds.read_masks(p.bands)
            if np.any((masks!=0)&(masks!=255)):raise BridgeError('IMAGE_BINARY_MASK: partial alpha needs an explicit compositing step')
            valid=np.all(masks==255,axis=0)
            if not valid.any():raise BridgeError('IMAGE_NO_VALID_PIXELS')
            if np.any(~np.isfinite(data[:,valid])):raise BridgeError('IMAGE_NONFINITE_SOURCE: mark nonfinite input values as NoData explicitly')
            if p.operation=='ifft' and ds.tags().get('MAPGIS_FFT'):
                try:convention=json.loads(ds.tags()['MAPGIS_FFT'])
                except (TypeError,ValueError) as exc:raise BridgeError('IMAGE_FFT_CONVENTION: invalid frequency metadata') from exc
                if (not isinstance(convention,dict) or convention.get('domain')!='frequency'
                        or convention.get('normalization')!=p.fft_norm or convention.get('shifted')!=p.fft_shift):
                    raise BridgeError('IMAGE_FFT_CONVENTION: inverse normalization and shift must match the saved frequency convention')
            source_scaling=[{'band':b,'scale':ds.scales[b-1],'offset':ds.offsets[b-1]} for b in p.bands]
            output,mask,nodata,details=_calculate(data,valid,p)
            files=stage/'files';files.mkdir();target=files/template['name'];gcps,gcp_crs=ds.gcps
            spatial={'crs':ds.crs,'transform':ds.transform}
            if p.operation in FFT_OPERATIONS:
                spatial={'crs':None,'transform':Affine.identity()};gcps=[];gcp_crs=None
                details['fft']['georeference']='pixel-index only; frequency bins are not source map locations'
                if reference_snapshot is not None:
                    with rasterio.open(reference_snapshot) as grid:
                        if (grid.width,grid.height)!=(ds.width,ds.height):raise BridgeError('IMAGE_FFT_REFERENCE_SHAPE: inverse grid must match both frequency dimensions')
                        spatial={'crs':grid.crs,'transform':grid.transform};gcps,gcp_crs=grid.gcps
                    details['fft']['georeference']='original spatial grid copied from explicit guarded reference; no resampling'
                elif p.operation=='ifft':details['fft']['georeference']='pixel-index only; no original spatial reference supplied'
            profile={'driver':'GTiff','width':ds.width,'height':ds.height,'count':len(output),'dtype':output.dtype,'nodata':nodata,
                **spatial,'compress':'deflate'}
            with rasterio.open(target,'w',**profile) as result:
                result.write(output)
                if gcps:result.gcps=(gcps,gcp_crs)
                if p.operation in {'stretch','colorize'} and len(output)==3:result.colorinterp=(rasterio.enums.ColorInterp.red,rasterio.enums.ColorInterp.green,rasterio.enums.ColorInterp.blue)
                if p.operation=='pca':
                    for band in range(1,len(output)+1):result.set_band_description(band,f'PC{band}')
                if p.operation in COLOR_OPERATIONS:
                    for band,name in enumerate(details['color_space']['band_names'],1):result.set_band_description(band,name)
                    if details['color_space']['output_space']=='RGB':result.colorinterp=(rasterio.enums.ColorInterp.red,rasterio.enums.ColorInterp.green,rasterio.enums.ColorInterp.blue)
                    else:result.colorinterp=(rasterio.enums.ColorInterp.undefined,)*3
                    result.update_tags(COLOR_SPACE=details['color_space']['output_space'])
                if p.operation in FFT_OPERATIONS:
                    for band,component in enumerate(('REAL','IMAG'),1):result.set_band_description(band,p.operation.upper()+'_'+component)
                    result.colorinterp=(rasterio.enums.ColorInterp.undefined,)*2
                    result.update_tags(MAPGIS_FFT=json_text({'schema':1,'domain':details['fft']['domain'],
                        'normalization':p.fft_norm,'shifted':p.fft_shift}))
                result.update_tags(AREA_OR_POINT='Area' if p.operation=='fft' else ds.tags().get('AREA_OR_POINT','Area'))
        info=raster_info(target);_native_mask_profile(target,info)
        with rasterio.open(target) as check:
            if not np.array_equal(check.read(),output) or not np.array_equal(check.read_masks(),np.broadcast_to(mask.astype('uint8')*255,output.shape)):
                raise BridgeError('IMAGE_SAVED_PIXEL_OR_MASK_DIFF')
        metrics={'shape':list(mask.shape),'bands':len(output),'valid_pixels':int(mask.sum()),'masked_pixels':int(mask.size-mask.sum()),
            'valid_pixels_input':int(valid.sum()),'ranges':[None if not mask.any() else [float(b[mask].min()),float(b[mask].max())] for b in output],
            'dtype':str(output.dtype),'nodata':nodata}
        metadata={'schema':'mapgis-agent/raster-processing/1','sources':sources,'parameters':payload['parameters'],'explicit_parameters':payload['explicit_parameters'],
            'source_scaling':source_scaling,'scale_policy':'operations use raw stored samples; no implicit scale/offset calibration',
            'mask_policy':'intersection of selected-band binary validity; complete support through neighborhood passes',
            'metrics':metrics,'saved_raster':info,**details}
        (files/(target.name+'.json')).write_text(json_text(metadata),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'SciPy1.16.2 ndimage/vq, NumPy and Rasterio/GDAL GeoTIFF',
            'metrics':metrics,'raster':info,'source_preserved':True,'empty_result':not bool(mask.any()),'native_reopen':'separate native MSI conversion/readback; not inferred',**details})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
