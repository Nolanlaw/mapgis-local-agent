"""Explicit registered-image fusion variants with numeric, mask and quality evidence."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
import rasterio
from scipy import ndimage

from .artifacts import begin_bundle, fail_bundle, finish_bundle
from .core import BridgeError, json_text
from .native_raster import _native_mask_profile, _raster_sources, _snapshot_raster, raster_info
from .raster_processing import RasterReference


class FusionParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    method: Literal['weighted', 'ihs', 'wavelet_ihs', 'wavelet_awlp', 'wavelet_maxabs']
    bands: list[StrictInt] = Field(min_length=1, max_length=8)
    pan_band: StrictInt = Field(default=1, ge=1)
    matching: Literal['none', 'mean_std']
    radiometry: Literal['stored', 'scale_offset'] = 'stored'
    pan_weights: list[StrictFloat] | None = None
    intensity_weights: list[StrictFloat] | None = None
    levels: StrictInt = Field(default=1, ge=1, le=6)
    edge: Literal['reflect', 'complete'] = 'reflect'
    intensity_epsilon: StrictFloat = Field(default=0., ge=0)
    zero_intensity: Literal['mask', 'reject'] = 'mask'
    nodata: StrictFloat = -9999.
    reference_scale_ratio: StrictFloat | None = Field(default=None, gt=0)


def _validate(p, count, pan_count, has_reference):
    if len(set(p.bands)) != len(p.bands) or min(p.bands) < 1 or max(p.bands) > count or p.pan_band > pan_count:
        raise BridgeError('FUSION_BANDS: unique existing one-based spectral bands and existing PAN band required')
    if p.method in {'ihs', 'wavelet_ihs'} and len(p.bands) != 3:
        raise BridgeError('FUSION_IHS_RGB: exactly three ordered RGB bands required for the declared linear IHS variant')
    common = {'method', 'bands', 'pan_band', 'matching', 'radiometry', 'nodata', 'reference_scale_ratio'}
    extra = {'weighted': {'pan_weights', 'intensity_weights'}, 'ihs': set(),
        'wavelet_ihs': {'levels', 'edge'}, 'wavelet_awlp': {'levels', 'edge', 'intensity_weights', 'intensity_epsilon', 'zero_intensity'},
        'wavelet_maxabs': {'levels', 'edge', 'intensity_weights'}}
    if p.model_fields_set-common-extra[p.method]:
        raise BridgeError('FUSION_UNEXPECTED_PARAMETERS: remove options belonging to another fusion method')
    if p.method == 'weighted' and (p.pan_weights is None or len(p.pan_weights) != len(p.bands)
            or any(not 0 <= value <= 1 for value in p.pan_weights)):
        raise BridgeError('FUSION_PAN_WEIGHTS: explicit fraction0..1 for each output band required')
    if p.intensity_weights is not None and (len(p.intensity_weights) != len(p.bands)
            or any(value < 0 for value in p.intensity_weights) or not np.isfinite(sum(p.intensity_weights))
            or sum(p.intensity_weights) <= 0):
        raise BridgeError('FUSION_INTENSITY_WEIGHTS: nonnegative weights with positive sum required')
    if has_reference != (p.reference_scale_ratio is not None):
        raise BridgeError('FUSION_REFERENCE_RATIO: a reference image and explicit originalMS/PAN scale ratio must be provided together')


def _frame(ds):
    if ds.tags(ns='RPC') or ds.tags(ns='GEOLOCATION'):
        raise BridgeError('FUSION_GEOREFERENCE: rectify RPC/geolocation images explicitly first')
    controls, crs = ds.gcps
    return (ds.width, ds.height, ds.transform, ds.crs,
            tuple((g.row, g.col, g.x, g.y, g.z) for g in controls), crs,
            ds.tags().get('AREA_OR_POINT', 'Area'))


def _read(ds, bands, radiometry):
    values = ds.read(bands, out_dtype='float64'); masks = ds.read_masks(bands)
    if np.any((masks != 0) & (masks != 255)):
        raise BridgeError('FUSION_BINARY_MASK: partial alpha must be explicitly composited first')
    valid = np.all(masks == 255, axis=0)
    if np.any(~np.isfinite(values[:, valid])):
        raise BridgeError('FUSION_NONFINITE_SOURCE: valid samples must be finite; mark missing observations explicitly')
    calibration = [{'band': b, 'scale': ds.scales[b-1], 'offset': ds.offsets[b-1], 'unit': ds.units[b-1]} for b in bands]
    if radiometry == 'scale_offset':
        for index, row in enumerate(calibration): values[index] = values[index]*row['scale']+row['offset']
    if np.any(~np.isfinite(values[:, valid])):
        raise BridgeError('FUSION_CALIBRATION: explicit scale/offset produced nonfinite samples')
    return np.where(valid, values, 0.), valid, calibration


def _step(values, valid, level, edge):
    spacing = 2**level; kernel = np.zeros(4*spacing+1); kernel[::spacing] = np.array([1,4,6,4,1])/16.
    footprint = (kernel != 0).astype('int16'); mode = 'reflect' if edge == 'reflect' else 'constant'
    filtered = values
    support = valid
    for axis in (-2, -1):
        filtered = ndimage.convolve1d(filtered, kernel, axis=axis, mode=mode, cval=0.)
        support = ndimage.convolve1d(support.astype('int16'), footprint, axis=axis, mode=mode, cval=0) == 5
    return filtered, support


def _moments(pan, basis, intensity, valid, matching):
    if matching == 'none': return 1., 0.
    source_mean = float(basis[valid].mean()); target_mean = float(intensity[valid].mean())
    source_std = float(basis[valid].std()); target_std = float(intensity[valid].std())
    if source_std == 0:
        if target_std != 0 or np.any(pan[valid] != pan[valid][0]):
            raise BridgeError('FUSION_MATCH_VARIANCE: constant PAN matching basis cannot determine a finite contrast gain')
        return 0., target_mean
    gain = target_std/source_std
    return gain, target_mean-gain*source_mean


def _fuse(ms, pan, ms_valid, pan_valid, p):
    weights = np.array(p.intensity_weights if p.intensity_weights is not None else [1.]*len(ms), dtype='float64')
    weights /= weights.sum()
    intensity = ms.mean(axis=0) if p.intensity_weights is None else np.einsum('i,iyx->yx', weights, ms)
    valid = ms_valid & pan_valid; input_count = int(valid.sum())
    low_pan = pan.copy(); low_ms = ms.copy(); support = pan_valid.copy()
    feature_details = np.zeros_like(ms) if p.method == 'wavelet_maxabs' else None
    if p.method.startswith('wavelet_'):
        # Moment fitting uses the final valid footprint and lowpass PAN. No
        # invalid neighbour is filled, averaged or renormalized into a result.
        if feature_details is not None: support &= ms_valid
        for level in range(p.levels):
            previous = low_pan; low_pan, support = _step(previous, support, level, p.edge)
        valid &= support
    undefined = np.zeros(valid.shape, dtype=bool)
    if p.method == 'wavelet_awlp':
        undefined = valid & (np.abs(intensity) <= p.intensity_epsilon)
        if undefined.any() and p.zero_intensity == 'reject':
            raise BridgeError('FUSION_ZERO_INTENSITY: undefined luminance-proportional gain; choose an explicit mask policy or another method')
        valid &= ~undefined
    if not valid.any(): raise BridgeError('FUSION_NO_SUPPORT: no jointly valid output pixels remain under the declared support/division policy')
    gain, offset = _moments(pan, low_pan, intensity, valid, p.matching)
    matched = pan*gain+offset
    selected_counts = []
    comparison_tolerance = (64*np.finfo('float64').eps*np.maximum(
        np.max(abs(ms), axis=(1,2)), np.max(abs(gain*pan)))) if p.method == 'wavelet_maxabs' else None
    if p.method == 'weighted':
        fractions = np.array(p.pan_weights)[:, None, None]
        output = (1-fractions)*ms+fractions*matched
    elif p.method == 'ihs':
        output = ms+(matched-intensity)
    elif p.method == 'wavelet_ihs':
        output = ms+gain*(pan-low_pan)
    elif p.method == 'wavelet_awlp':
        ratios = np.zeros_like(ms); np.divide(ms, intensity, out=ratios, where=valid[None])
        output = ms+ratios*(gain*(pan-low_pan))
    else:
        ms_support = ms_valid & pan_valid
        feature_pan = pan.copy()
        for level in range(p.levels):
            next_pan, _ = _step(feature_pan, pan_valid, level, p.edge)
            pan_detail = gain*(feature_pan-next_pan); feature_pan = next_pan
            following, ms_support = _step(low_ms, ms_support, level, p.edge)
            ms_detail = low_ms-following
            # A tiny convolution-roundoff difference must not choose an
            # opposite-signed coefficient of otherwise equal magnitude.
            choose = abs(pan_detail)[None]-abs(ms_detail) > comparison_tolerance[:,None,None]
            feature_details += np.where(choose, pan_detail, ms_detail)
            selected_counts.append([int(np.count_nonzero(band & valid)) for band in choose])
            low_ms = following
        output = low_ms+feature_details
    if not np.isfinite(output[:, valid]).all(): raise BridgeError('FUSION_NONFINITE_RESULT')
    metadata = {'method': p.method, 'working_dtype': 'float64', 'clipping': 'none', 'resampling': 'none; grids checked equal',
        'intensity_weights': weights.tolist(), 'moment_matching': p.matching, 'pan_gain': gain, 'pan_offset': offset,
        'moment_basis': 'final lowpass PAN for wavelet methods, original PAN otherwise; final shared valid footprint',
        'radiometry': p.radiometry, 'valid_input_pixels': input_count, 'valid_output_pixels': int(valid.sum()),
        'undefined_intensity_pixels': int(undefined.sum()),
        'ihs_convention': 'linear neutral-axis transform: I=(R+G+B)/3,V1=(2R-G-B)/sqrt6,V2=(B-G)/sqrt2; preserve V1,V2 and replace/add intensity',
        'wavelet': 'undecimated separable cubicB3 spline [1,4,6,4,1]/16; levelj holes spaced2**j; detail=c_j-c_(j+1); no decimation',
        'support_radius_pixels': 2*(2**p.levels-1) if p.method.startswith('wavelet_') else 0,
        'edge': p.edge if p.method.startswith('wavelet_') else 'pointwise',
        'mask_policy': 'joint selected-band validity, complete nonzero-kernel support at every wavelet pass; no missing fill or weight renormalization',
        'maxabs_rule': 'keep MS lowpass, select signed larger absolute detail per pixel/band/scale; magnitudes within64eps*max(source-band peak,scaled-PAN peak) retain MS',
        'maxabs_comparison_roundoff_factor': 64 if comparison_tolerance is not None else None,
        'maxabs_magnitude_tolerance_by_band': comparison_tolerance.tolist() if comparison_tolerance is not None else None,
        'maxabs_pan_selections_by_level_band': selected_counts,
        'scope': 'explicit assistant mathematical variants, not proprietary MapGIS parameter-equivalent solvers; AWLP is the common-luminance moment-matched variant, without MTF/haze/SRF estimation'}
    return output, valid, metadata


def _quality(output, ms, valid, reference=None, reference_valid=None, ratio=None):
    before = ms[:, valid]; after = output[:, valid]; delta = after-before
    diagnostics = {'kind': 'change relative to registered MS; not accuracy without a true reference',
        'rmse_change_by_band': np.sqrt(np.mean(delta**2, axis=1)).tolist(),
        'bias_change_by_band': delta.mean(axis=1).tolist(), 'source_std_by_band': before.std(axis=1).tolist(),
        'output_std_by_band': after.std(axis=1).tolist()}
    result = {'diagnostics': diagnostics, 'reference': None}
    if reference is not None:
        mask = valid & reference_valid
        if not mask.any(): raise BridgeError('FUSION_REFERENCE_SUPPORT: no valid reference overlap')
        truth = reference[:, mask]; actual = output[:, mask]; difference = actual-truth
        rmse = np.sqrt(np.mean(difference**2, axis=1)); means = truth.mean(axis=1)
        truth_scale = np.max(abs(truth), axis=0); actual_scale = np.max(abs(actual), axis=0)
        angular = (truth_scale > 0) & (actual_scale > 0)
        if angular.any():
            a = actual[:, angular]/actual_scale[angular]; b = truth[:, angular]/truth_scale[angular]
            cos = np.sum(a*b, axis=0)/np.sqrt(np.sum(a*a, axis=0)*np.sum(b*b, axis=0))
            cos[np.all(actual[:, angular] == truth[:, angular], axis=0)] = 1.
            sam = float(np.degrees(np.arccos(np.clip(cos, -1., 1.))).mean())
        else: sam = None
        result['reference'] = {'valid_pixels': int(mask.sum()), 'rmse_by_band': rmse.tolist(),
            'mae_by_band': np.mean(abs(difference), axis=1).tolist(), 'sam_degrees': sam,
            'sam_valid_pixels': int(angular.sum()), 'sam_zero_vector_pixels': int((~angular).sum()),
            'ergas': None if np.any(means == 0) else float(100/ratio*np.sqrt(np.mean((rmse/means)**2))),
            'ergas_zero_mean_bands': (np.flatnonzero(means == 0)+1).tolist(), 'scale_ratio': ratio,
            'reference_policy': 'caller-supplied hash-bound reference; its suitability as ground truth is not inferred'}
    return result


def prepare_raster_fusion(bridge, multispectral, panchromatic, parameters, template, request_id, reference=None):
    p = parameters if isinstance(parameters, FusionParameters) else FusionParameters.model_validate(parameters)
    refs = [v if isinstance(v, RasterReference) else RasterReference.model_validate(v)
            for v in [multispectral, panchromatic]+([reference] if reference is not None else [])]
    paths = [Path(v.file).resolve(strict=True) for v in refs]
    guarded = [_raster_sources(path, v.expected_sha256, v.expected_members) for path, v in zip(paths, refs)]
    sources = list({v['file']: v for group in guarded for v in group}.values())
    name = template.get('name', '')
    if (set(template) != {'name'} or not name or Path(name).name != name or name.endswith((' ', '.'))
            or any(c in name for c in '/\\:<>"|?*') or Path(name).suffix.lower() not in {'.tif', '.tiff'}):
        raise BridgeError('FUSION_TEMPLATE: new GeoTIFF basename required')
    with ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(path)) for path in paths]
        _validate(p, datasets[0].count, datasets[1].count, reference is not None)
        if datasets[0].width*datasets[0].height*len(p.bands) > 16000000:
            raise BridgeError('FUSION_SIZE: at most16000000 selected spectral band cells per request')
        if any(_frame(ds) != _frame(datasets[0]) for ds in datasets[1:]):
            raise BridgeError('FUSION_ALIGNMENT: dimensions, affine/CRS/GCP and pixel interpretation must match exactly; explicitly register/resample first')
        if reference is not None and datasets[2].count != len(p.bands):
            raise BridgeError('FUSION_REFERENCE_BANDS: reference must contain exactly the ordered selected output bands')
    payload = {'operation': 'raster.fusion', 'sources': [v.model_dump() for v in refs],
        'parameters': p.model_dump(), 'explicit_parameters': sorted(p.model_fields_set), 'template': template,
        'fusion_protocol': 2 if p.method == 'wavelet_maxabs' else 1}
    stage, receipt, new = begin_bundle(bridge, payload, request_id, sources, name)
    if not new: return receipt
    try:
        snapshots = [_snapshot_raster(path, group, stage/f'input-{i}') for i, (path, group) in enumerate(zip(paths, guarded))]
        with ExitStack() as stack, np.errstate(over='raise', invalid='raise', divide='raise'):
            datasets = [stack.enter_context(rasterio.open(path)) for path in snapshots]; ms_ds, pan_ds = datasets[:2]
            if any(_frame(ds) != _frame(ms_ds) for ds in datasets[1:]): raise BridgeError('FUSION_ALIGNMENT')
            ms, ms_valid, ms_cal = _read(ms_ds, p.bands, p.radiometry)
            pan, pan_valid, pan_cal = _read(pan_ds, [p.pan_band], p.radiometry)
            output, valid, details = _fuse(ms, pan[0], ms_valid, pan_valid, p)
            truth, truth_valid, ref_cal = _read(datasets[2], list(range(1,len(p.bands)+1)), p.radiometry) if reference is not None else (None, None, None)
            quality = _quality(output, ms, valid, truth, truth_valid, p.reference_scale_ratio)
            nodata = p.nodata if not valid.all() else None
            if nodata is not None and np.any(output[:, valid] == nodata):
                raise BridgeError('FUSION_NODATA_COLLISION: choose a finite marker outside all valid results')
            output[:, ~valid] = 0. if nodata is None else nodata
            files = stage/'files'; files.mkdir(); target = files/name
            gcps, gcp_crs = ms_ds.gcps
            spatial = {'gcps': gcps, 'crs': gcp_crs} if gcps else {'transform': ms_ds.transform, 'crs': ms_ds.crs}
            with rasterio.open(target, 'w', driver='GTiff', width=ms_ds.width, height=ms_ds.height,
                               count=len(output), dtype='float64', nodata=nodata, compress='deflate', **spatial) as saved:
                saved.write(output)
                for index, band in enumerate(p.bands, 1): saved.set_band_description(index, f'FUSED_{p.method}_B{band}')
                saved.update_tags(AREA_OR_POINT=ms_ds.tags().get('AREA_OR_POINT', 'Area'),
                                  MAPGIS_FUSION=json_text({'method': p.method, 'radiometry': p.radiometry, 'protocol': payload['fusion_protocol']}))
            with rasterio.open(target) as check:
                if (not np.array_equal(check.read(), output) or not np.array_equal(check.read_masks()>0, np.broadcast_to(valid, output.shape))
                        or _frame(check) != _frame(ms_ds)):
                    raise BridgeError('FUSION_SAVED_PIXEL_MASK_OR_FRAME_DIFF')
            info = raster_info(target); _native_mask_profile(target, info)
        metadata = {'schema': 'mapgis-agent/raster-fusion/1', 'sources': sources, 'parameters': p.model_dump(),
            'fusion': details, 'quality': quality, 'source_calibration': {'multispectral': ms_cal, 'panchromatic': pan_cal, 'reference': ref_cal},
            'registration_policy': 'equal frame metadata required; physical co-registration and sensor spectral response remain caller-supplied facts',
            'raster': info, 'source_preserved': True}
        (files/(name+'.json')).write_text(json_text(metadata), encoding='utf8')
        return finish_bundle(bridge, request_id, files, {'backend': 'NumPy/SciPy ndimage cubicB3 wavelet and Rasterio/GDAL',
            'fusion': details, 'quality': quality, 'raster': info, 'source_preserved': True,
            'native_reopen': 'separate actual MSI roundtrip and native plotting; not inferred from TIFF write'})
    except Exception as exc:
        fail_bundle(bridge, request_id, exc); raise
