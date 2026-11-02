"""Explicit grouped mean/SD or lower-half mode equations from Section's macro."""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel,ConfigDict,Field,StrictInt

from .core import BridgeError


class GroupedParameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    estimator:Literal['mean_sd','left_mode']
    intervals:StrictInt=Field(default=10,ge=2,le=1000)
    precision:Literal['full','section_decimals']
    mode_tie:Literal['error','lowest']


def grouped_estimate(values,p,k,sides):
    minimum=float(np.min(values));maximum=float(np.max(values))
    width=(maximum-minimum)/p.intervals;origin=minimum
    rounded=p.precision=='section_decimals'
    if rounded:width=round(width,2);origin=round(origin,1)
    if not math.isfinite(width) or width<=0:raise BridgeError('GEOCHEM_HISTOGRAM_WIDTH: no invented zero-width fallback')
    edges=origin+width*(np.arange(p.intervals+2,dtype=np.float64)-.5)
    if not np.isfinite(edges).all() or not (np.diff(edges)>0).all():raise BridgeError('GEOCHEM_HISTOGRAM_WIDTH')
    indices=np.searchsorted(edges,values,side='right')-1
    if ((indices<0)|(indices>=len(edges)-1)).any():raise BridgeError('GEOCHEM_HISTOGRAM_COVERAGE: all retained values must be represented')
    counts=np.bincount(indices,minlength=len(edges)-1);centres=edges[:-1]+width*.5
    if rounded:centres=np.round(centres,3)
    histogram={'edges':edges.tolist(),'centres':centres.tolist(),'counts':counts.tolist(),
        'bin_width':width,'included_n':int(counts.sum()),'estimator':p.estimator,'precision':p.precision,
        'boundary':'Every bin left inclusive/right exclusive; no omitted samples.',
        'rounding':'width2decimals,origin1,centres3,mode3,left_n0,spread3,threshold3; Pythonbinary64/half-even, noVBA Single/productrounding orFind-state equivalence' if rounded else 'binary64; no intermediate decimal rounding'}
    if p.estimator=='mean_sd':
        peak=float(np.max(np.abs(centres)));scale=peak if peak>1e150 or 0<peak<1e-150 else 1.
        normalized=centres/scale;centre=float(np.dot(counts,normalized)/counts.sum()*scale)
        spread=float(np.sqrt(np.dot(counts,(normalized-centre/scale)**2)/(counts.sum()-1))*scale)
        if rounded:centre=round(centre,3);spread=round(spread,3)
    else:
        if sides!='upper':raise BridgeError('GEOCHEM_MODE_SIDES: left-mode spread estimates an upper threshold only')
        modes=np.flatnonzero(counts==counts.max())
        if len(modes)>1 and p.mode_tie=='error':raise BridgeError('GEOCHEM_MODE_TIE: explicitly select lowest-bin tie handling or change binning')
        index=int(modes[0]);f2=int(counts[index]);f1=int(counts[index-1]) if index else 0
        f3=int(counts[index+1]) if index+1<len(counts) else 0;denominator=2*f2-f1-f3
        if denominator<=0:raise BridgeError('GEOCHEM_MODE_UNDEFINED: no arbitrary mode=1 fallback')
        lower=float(edges[index]);centre=lower+width*(f2-f1)/denominator
        if rounded:centre=round(centre,3)
        fraction=(centre-lower)/width
        if not 0<=fraction<=1:raise BridgeError('GEOCHEM_MODE_ROUNDING: rounded mode lies outside its modal bin')
        left_n=float(counts[:index].sum())+f2*fraction
        if rounded:left_n=round(left_n,0)
        if left_n<=.5:raise BridgeError('GEOCHEM_MODE_TOO_SMALL: effective lower-half count must exceed0.5')
        offsets=centre-edges[:index];partial=centre-(centre+lower)*.5
        peak=max(float(np.max(np.abs(offsets))) if index else 0.,abs(partial))
        if peak==0:spread=0.
        else:
            # The first-bin branch is deliberately the separate published VBA
            # equation: full f2, versus the fractional f2 in subsequent bins.
            numerator=math.fsum(int(f)*(float(delta)/peak)**2 for f,delta in zip(counts[:index],offsets))
            numerator+=f2*(partial/peak)**2*(fraction if index else 1.)
            spread=peak*math.sqrt(numerator/(left_n-.5))
        if rounded:spread=round(spread,3)
        histogram.update(mode_bin=index,mode_ties=modes.tolist(),mode_tie_policy=p.mode_tie,
            previous_frequency=f1,modal_frequency=f2,next_frequency=f3,left_effective_n=left_n,
            lower_half_equation='Nonmodal lower-bin loweredges plus partialmodal midpoint; denominator n_left-0.5; separate first-bin branch. Not ordinary sample SD.')
    low=centre-k*spread if sides=='both' else None;high=centre+k*spread
    if rounded:
        low=round(low,3) if low is not None else None;high=round(high,3)
    if not all(value is None or math.isfinite(value) for value in (centre,spread,low,high)):
        raise BridgeError('GEOCHEM_NUMERIC_RANGE')
    return centre,spread,low,high,histogram
