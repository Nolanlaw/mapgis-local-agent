"""Publish PDF/TIFF from an actual native EPS using the existing Ghostscript."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import cv2
import numpy as np
import rasterio

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text
from .native_plot import _ghostscript,_render_check,validate_eps
from .raster import file_sha


def prepare_plot_export(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
    if not isinstance(parameters,dict) or set(parameters)-{'formats','dpi'}:
        raise BridgeError('PLOT_EXPORT_PARAMETERS')
    formats=parameters.get('formats',['pdf','tiff']);dpi=parameters.get('dpi',300)
    if (not isinstance(formats,list) or not formats or any(type(f) is not str or f not in ('pdf','tiff') for f in formats)
            or len(set(formats))!=len(formats) or type(dpi) is not int or not 72<=dpi<=600):
        raise BridgeError('PLOT_EXPORT_FORMAT: unique pdf/tiff formats and integer dpi in 72..600 required')
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.suffix.lower()!='.eps':raise BridgeError('PLOT_EXPORT_EPS_REQUIRED')
    with source.open('rb') as stream:header=stream.read(4096)
    bounds=re.search(rb'%%HiResBoundingBox:\s*([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)',header)
    if not bounds:raise BridgeError('PLOT_EXPORT_NATIVE_PAGE_REQUIRED')
    paper=[float(v)*25.4/72 for v in bounds.groups()[2:]]
    eps=validate_eps(source,paper)
    pixels=[round(v*dpi/25.4) for v in paper]
    if pixels[0]*pixels[1]>100_000_000:raise BridgeError('PLOT_EXPORT_TOO_LARGE: lower dpi or split the physical page')
    primary='map.pdf' if 'pdf' in formats else 'map.tif'
    payload={'operation':'plot.export','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(source),'sha256':expected_sha256}],primary)
    if not new:return receipt
    try:
        executable=_ghostscript(bridge.root);files=stage/'files';files.mkdir()
        baseline=stage/'baseline';baseline.mkdir();_render_check(executable,baseline,source,paper)
        checks={};logs=[]
        for format in formats:
            target=files/('map.pdf' if format=='pdf' else 'map.tif')
            command=[str(executable),'-dSAFER','-dBATCH','-dNOPAUSE','-dEPSCrop',
                '-sDEVICE='+('pdfwrite' if format=='pdf' else 'tiff24nc'),'-r'+str(dpi),
                '-sOutputFile='+str(target),str(source)]
            result=subprocess.run(command,capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
            logs.append({'format':format,'exit_code':result.returncode,'stdout':result.stdout.decode('utf8',errors='replace'),
                'stderr':result.stderr.decode('utf8',errors='replace')})
            if result.returncode or not target.is_file():raise BridgeError('PLOT_EXPORT_FAILED: '+format)
            if format=='tiff':
                with rasterio.open(target) as raster:
                    if [raster.width,raster.height]!=pixels or raster.count!=3 or raster.dtypes!=('uint8',)*3:
                        raise BridgeError('PLOT_EXPORT_TIFF_PROFILE')
                    ink=0
                    for _,window in raster.block_windows(1):ink+=int(np.any(raster.read(window=window)<245,axis=0).sum())
                if not ink:raise BridgeError('PLOT_EXPORT_EMPTY_TIFF')
                checks[format]={'size_px':pixels,'dpi':dpi,'ink_pixels':ink,'scope':'physical page; no georeferencing inferred'}
            else:
                with target.open('rb') as stream:
                    if not stream.read(8).startswith(b'%PDF-'):raise BridgeError('PLOT_EXPORT_INVALID_PDF')
                pdfcheck=stage/'pdf-check';pdfcheck.mkdir();render=_render_check(executable,pdfcheck,target,paper)
                def read_image(path):return cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
                before=read_image(baseline/'preview.png');after=read_image(pdfcheck/'preview.png')
                delta=np.abs(before.astype(np.int16)-after.astype(np.int16))
                mean=float(delta.mean());fraction=float(np.mean(np.all(delta<=5,axis=2)))
                if mean>1 or fraction<.99:raise BridgeError('PLOT_EXPORT_PDF_VISUAL_CHANGE')
                checks[format]={'size_px_72dpi':render['size_px'],'preview_matches_eps':True,
                    'mean_channel_difference':mean,'fraction_within_5':fraction}
        (stage/'export-log.json').write_text(json_text(logs),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'Ghostscript from native EPS','eps':eps,
            'paper_mm':paper,'checks':checks,'source_preserved':True})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
