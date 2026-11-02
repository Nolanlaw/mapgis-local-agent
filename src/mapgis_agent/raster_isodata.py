"""Bounded ISOCLUS-style split/merge clustering using installed SciPy kernels.

Equations: Memarsadeghi et al.(2007), section2, steps2..11. This is an explicit
variant: seeded kmeans++/one Lloyd initialization, explicit maximum classes,
alternating topology opportunities without the original2*initial split ceiling,
coordinate-axis mean±split_fraction*std, and early stop after two quiet updates.
No external implementation source is copied; no proprietary equivalence claimed.
"""
from __future__ import annotations
import hashlib
import numpy as np
import scipy
from scipy.cluster.vq import kmeans2,ClusterError
from scipy.spatial.distance import cdist
from pydantic import BaseModel,ConfigDict,Field,StrictFloat,StrictInt,model_validator
from .core import BridgeError


class IsodataParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    initial_classes:StrictInt=Field(default=5,ge=1,le=32)
    max_classes:StrictInt=Field(default=32,ge=1,le=32)
    min_cluster_pixels:StrictInt=Field(ge=1,description='Minimum assigned fitting-sample pixels before pruning, not a promise about unsampled pixels or capped unconverged final predictions.')
    split_stddev:StrictFloat=Field(gt=0,description='Largest population band standard deviation in working feature units. Working units are standardized only when requested.')
    merge_distance:StrictFloat=Field(ge=0,description='Maximum Euclidean centre distance for a merge; close centres merge. Zero permits only exact duplicate centres.')
    max_merge_pairs:StrictInt=Field(default=1,ge=1,le=16)
    split_fraction:StrictFloat=Field(default=.5,gt=0,le=1)
    center_tolerance:StrictFloat=Field(default=1e-8,ge=0)

    @model_validator(mode='after')
    def class_bounds(self):
        if self.initial_classes>self.max_classes:raise ValueError('initial_classes must not exceed max_classes')
        return self


def assign_nearest(points,centres):
    codes=np.empty(len(points),dtype='int32');distances=np.empty(len(points),dtype='float64')
    for start in range(0,len(points),65536):
        stop=min(start+65536,len(points));squared=cdist(points[start:stop],centres,metric='sqeuclidean')
        if not np.isfinite(squared).all():raise BridgeError('ISODATA_NONFINITE_DISTANCE: explicitly rescale features')
        assigned=np.argmin(squared,axis=1)
        codes[start:stop]=assigned
        distances[start:stop]=np.sqrt(squared[np.arange(stop-start),assigned])
    return codes,distances


def _means(points,codes,counts):
    sums=np.stack([np.bincount(codes,weights=band,minlength=len(counts)) for band in points.T],axis=1)
    means=np.divide(sums,counts[:,None],out=np.zeros_like(sums),where=counts[:,None]!=0)
    if not np.isfinite(means).all():raise BridgeError('ISODATA_NONFINITE_CENTRE: explicitly rescale features')
    return means


def fit_isodata(points,options,iterations,rng):
    unique_count=len(np.unique(points,axis=0));initial=min(options.initial_classes,unique_count)
    if initial==1:centres=points.mean(axis=0,keepdims=True)
    else:
        try:centres,_=kmeans2(points,initial,iter=1,minit='++',missing='raise',rng=rng)
        except (ValueError,ClusterError) as exc:raise BridgeError('ISODATA_INITIALIZATION: '+str(exc)) from exc
    history=[];previous_codes=None;quiet=0;termination='max_iterations'
    for iteration in range(1,iterations+1):
        count=len(centres);codes,_=assign_nearest(points,centres)
        counts=np.bincount(codes,minlength=count);means=_means(points,codes,counts)
        keep=counts>=options.min_cluster_pixels
        if not keep.all():
            if not keep.any():raise BridgeError('ISODATA_MINIMUM: every fitting cluster is below min_cluster_pixels; use an explicit smaller minimum or different initialization')
            centres=means[keep];previous_codes=None;quiet=0
            history.append({'iteration':iteration,'action':'prune','before':count,'after':len(centres),
                'temporarily_unassigned_fit_pixels':int(counts[~keep].sum())})
            continue
        shift=float(np.max(np.linalg.norm(means-centres,axis=1)));centres=means
        delta=points-centres[codes]
        squared=delta*delta
        if not np.isfinite(squared).all():raise BridgeError('ISODATA_NONFINITE_DISPERSION: explicitly rescale features')
        deviations=np.sqrt(np.stack([np.bincount(codes,weights=band,minlength=count) for band in squared.T],axis=1)/counts[:,None])
        distance=np.sqrt(squared.sum(axis=1));average=np.bincount(codes,weights=distance,minlength=count)/counts
        overall=float(distance.mean());last=iteration==iterations
        # initial_classes is a seed count, not an implicit2*K ceiling. The
        # explicit max_classes governs growth; even passes offer merging.
        merge_stage=last or (2*count>options.initial_classes and iteration%2==0)
        splits=[]
        if not merge_stage and count<options.max_classes:
            axes=np.argmax(deviations,axis=1);maximum=deviations[np.arange(count),axes]
            eligible=[index for index in range(count) if maximum[index]>options.split_stddev
                and ((average[index]>overall and counts[index]>2*(options.min_cluster_pixels+1))
                    or count<=options.initial_classes/2
                    or (count==1 and counts[index]>=2*options.min_cluster_pixels))]
            eligible=sorted(eligible,key=lambda i:(-maximum[i],i))[:options.max_classes-count]
            updated=[]
            for index,centre in enumerate(centres):
                if index in eligible:
                    axis=int(axes[index]);offset=options.split_fraction*maximum[index]
                    left=centre.copy();right=centre.copy();left[axis]-=offset;right[axis]+=offset
                    if not np.isfinite([left,right]).all():raise BridgeError('ISODATA_NONFINITE_SPLIT: explicitly rescale features')
                    if not np.array_equal(left,right):
                        updated.extend([left,right]);splits.append({'cluster':index,'band_index':axis,
                            'population_stddev':float(maximum[index]),'mean_distance':float(average[index])});continue
                updated.append(centre)
            if splits:
                centres=np.asarray(updated);previous_codes=None;quiet=0
                history.append({'iteration':iteration,'action':'split','before':count,'after':len(centres),
                    'overall_mean_distance':overall,'splits':splits});continue
        pairwise=cdist(centres,centres,metric='euclidean');threshold=0. if last else options.merge_distance
        candidates=sorted((float(pairwise[i,j]),i,j) for i in range(count) for j in range(i+1,count) if pairwise[i,j]<=threshold)
        occupied=set();pairs=[]
        for gap,i,j in candidates:
            if i in occupied or j in occupied:continue
            pairs.append((i,j,gap));occupied.update((i,j))
            if len(pairs)>=options.max_merge_pairs:break
        if pairs:
            merged={i:centres[i]+(centres[j]-centres[i])*(counts[j]/(counts[i]+counts[j])) for i,j,_ in pairs}
            removed={j for _,j,_ in pairs}
            centres=np.asarray([merged.get(i,centre) for i,centre in enumerate(centres) if i not in removed])
            previous_codes=None;quiet=0
            history.append({'iteration':iteration,'action':'merge','before':count,'after':len(centres),
                'pairs':[{'first':i,'second':j,'distance':gap,'populations':[int(counts[i]),int(counts[j])]} for i,j,gap in pairs]});continue
        stable=previous_codes is not None and np.array_equal(codes,previous_codes) and shift<=options.center_tolerance
        quiet=quiet+1 if stable else 0;previous_codes=codes.copy()
        history.append({'iteration':iteration,'action':'update','before':count,'after':count,
            'max_centre_shift':shift,'mean_distance':overall,'fit_counts':counts.tolist()})
        if quiet>=2:termination='converged';break
    # After a capped topology update, drop only unoccupied predictor centres.
    # Do not invent a last successful optimization step or discard source pixels.
    removed_empty=0
    while True:
        codes,distances=assign_nearest(points,centres);counts=np.bincount(codes,minlength=len(centres))
        if counts.min()>0:break
        keep=counts>0;removed_empty+=int((~keep).sum());centres=centres[keep]
    return centres,{'initial_classes_requested':options.initial_classes,'initial_classes_used':initial,
        'termination':termination,'iterations':len(history),'history':history,
        'post_fit_empty_centres_removed':removed_empty,'fit_mean_squared_distance':float(np.mean(distances**2))}


def cluster_image(data,valid,p):
    options=p.isodata;rng=np.random.default_rng(p.seed)
    points=np.moveaxis(data,0,-1)[valid];size=min(len(points),p.sample_size)
    sample_indices=np.sort(rng.choice(len(points),size,replace=False)) if size<len(points) else np.arange(size)
    sample=points[sample_indices];centering=sample.mean(axis=0) if p.standardize else np.zeros(len(data))
    deviation=sample.std(axis=0,ddof=0);scales=np.where(deviation>0,deviation,1.) if p.standardize else np.ones(len(data))
    working=(sample-centering)/scales
    if not np.isfinite(working).all() or not np.isfinite(scales).all():raise BridgeError('ISODATA_NONFINITE_FEATURES: explicitly rescale features')
    centres,model=fit_isodata(working,options,p.iterations or 30,rng)
    source_centres=centres*scales+centering
    order=sorted(range(len(centres)),key=lambda i:tuple(source_centres[i]));centres=centres[order];source_centres=source_centres[order]
    codes,distances=assign_nearest((points-centering)/scales,centres)
    fit_codes,fit_distance=assign_nearest(working,centres)
    output=np.zeros(valid.shape,dtype='uint8');output[valid]=codes+1
    source_indices=np.flatnonzero(valid)[sample_indices].astype('<u8')
    fit_counts=np.bincount(fit_codes,minlength=len(centres))
    model|={'implementation':'ISOCLUS-style adaptive NumPy/SciPy variant; no copied external implementation',
        'numpy_version':np.__version__,'scipy_version':scipy.__version__,'classes':len(centres),'options':options.model_dump(),
        'centres_working_units':centres.tolist(),'centres_source_units':source_centres.tolist(),
        'centering':centering.tolist(),'scales':scales.tolist(),'standardized':p.standardize,
        'constant_bands':[p.bands[i] for i in np.flatnonzero(deviation==0)],'population_stddev_ddof':0,
        'fit_pixels':size,'valid_prediction_pixels':len(points),'seed':p.seed,
        'sample_flat_indices_sha256':hashlib.sha256(source_indices.tobytes()).hexdigest(),
        'fit_counts':fit_counts.tolist(),'prediction_counts':np.bincount(codes,minlength=len(centres)).tolist(),
        'final_fit_classes_below_pruning_minimum':[i+1 for i,c in enumerate(fit_counts) if c<options.min_cluster_pixels],
        'prediction_mean_squared_distance':float(np.mean(distances**2)),
        'labels':'1..K lexicographically ordered source-unit centres;0=NoData; no inferred geological or land-cover labels',
        'ties':'first minimum distance, hence lowest canonical final class label',
        'sampling':'seeded uniform valid pixels without replacement, sorted source flat indices; thresholds apply to fitting sample',
        'variant':'kmeans++ plus one initial Lloyd pass; ISOCLUS-style equations with alternating split/merge opportunities and no implicit2*initial_classes split ceiling; axis mean±split_fraction*population_stddev; a single diffuse centre may split when it can supply two minimum-size children; explicit max_classes cap; two quiet updates allow early stop. Last iteration disables positive-distance merges and splitting. Each update/prune/topology pass consumes one iteration.',
        'reference':'https://www.cs.umd.edu/~mount/Projects/ISODATA/ijcga07-isodata.pdf',
        'scope':'Numeric unsupervised clusters and auditable predictor state; not proprietary MapGIS parameter equivalence or independent field validation'}
    return output[None,:,:],model
