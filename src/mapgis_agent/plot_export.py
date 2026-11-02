"""Publish PDF/TIFF/JPEG from an actual native EPS using existing Ghostscript."""
from __future__ import annotations

from pathlib import Path
import math
import json
import os
import re
import shutil
import subprocess
import time
import uuid

import numpy as np
import psutil
import rasterio

from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text,sha
from .native_plot import _ghostscript,_render_check,_pixel_comparison,_matching_pixels,validate_eps,LOSSLESS_PDF_IMAGE_OPTIONS
from .raster import file_sha


NAMES={'pdf':'map.pdf','tiff':'map.tif','jpeg':'map.jpg'}


def _scoped(stage,path):
    if not path.resolve().is_relative_to(stage.resolve()):raise BridgeError('EXPORT_STAGE_IDENTITY')
    return path


def _record(path:Path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('x',encoding='utf8') as stream:
        stream.write(json_text(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _gs_identity(executable):
    return {'executable':str(executable.resolve()),'sha256':file_sha(executable),
            'dlls':[{'file':str(p.resolve()),'sha256':file_sha(p)} for p in sorted(executable.parent.glob('gsdll*.dll'))]}


def _command(context,format,target,snapshot):
    command=[context['ghostscript']['executable'],'-dSAFER','-dBATCH','-dNOPAUSE','-dEPSCrop',
             '-sDEVICE='+{'pdf':'pdfwrite','tiff':'tiff24nc','jpeg':'jpeg'}[format],'-r'+str(context['dpi']),
             *(context['pdf_image_options'] if format=='pdf' else ()),
             '-sOutputFile='+str(target),str(snapshot)]
    if format=='jpeg':command.insert(-1,'-dJPEGQ=95')
    return command


def _live_converter(folder,command):
    """Reconcile one exact command, including the small Popen/start-record gap."""
    started=folder/'process-start.json';process=None
    if started.exists():
        identity=json.loads(started.read_text(encoding='utf8'))
        if identity['command']!=command:raise BridgeError('EXPORT_CONVERSION_CHANGED')
        try:
            candidate=psutil.Process(identity['pid'])
            if (abs(candidate.create_time()-identity['start'])<.01 and Path(candidate.exe()).resolve()==Path(command[0]).resolve()
                and candidate.cmdline()==command):
                process=candidate
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied as error:raise BridgeError('EXPORT_PROCESS_IDENTITY_UNAVAILABLE') from error
    else:
        # An interrupted parent may not yet have persisted Popen's PID. Match
        # only this owned attempt's complete argv; never act on a process name.
        for candidate in psutil.process_iter(['name']):
            if (candidate.info['name'] or '').casefold()!=Path(command[0]).name.casefold():continue
            try:
                if candidate.cmdline()==command and Path(candidate.exe()).resolve()==Path(command[0]).resolve():
                    if process is not None:raise BridgeError('EXPORT_PROCESS_IDENTITY_AMBIGUOUS')
                    process=candidate
            except (psutil.NoSuchProcess,psutil.AccessDenied):continue
    if process is not None and process.is_running():
        if time.time()-process.create_time()<=125:return {'pid':process.pid,'start':process.create_time(),'executable':process.exe()}
        process.terminate();process.wait(timeout=5)
        raise BridgeError('EXPORT_CONVERSION_TIMEOUT: exact owned converter stopped; candidate retained')
    return None


def _execute(command,folder,target):
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'stdout.txt').open('xb') as stdout,(folder/'stderr.txt').open('xb') as stderr:
        process=subprocess.Popen(command,cwd=folder,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _record(folder/'process-start.json',{'pid':process.pid,'start':psutil.Process(process.pid).create_time(),'command':command})
        except BaseException:
            process.kill();process.wait();raise
        timed_out=False
        try:process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            timed_out=True;process.kill();process.wait()
    result={'command':command,'pid':process.pid,'returncode':process.returncode,'timed_out':timed_out,
            'output_sha256':file_sha(target) if target.is_file() else None}
    _record(folder/'process.json',result)
    if timed_out or process.returncode or not target.is_file():raise BridgeError('PLOT_EXPORT_FAILED: '+str(folder))
    return result


def _candidate(stage,context,format,context_sha,*,fresh=False):
    root=_scoped(stage,stage/'formats'/format);root.mkdir(parents=True,exist_ok=True);pointer=root/'attempt.json'
    if pointer.exists() and not fresh:
        attempt=json.loads(pointer.read_text(encoding='utf8'))
        if attempt['context_sha256']!=context_sha or not re.fullmatch(r'attempt-[a-f0-9]{32}',attempt['directory']):raise BridgeError('EXPORT_CONVERSION_CHANGED')
        folder=_scoped(stage,root/attempt['directory']);target=_scoped(stage,folder/NAMES[format])
        command=_command(context,format,target,stage/'source.eps')
        if attempt['command']!=command:raise BridgeError('EXPORT_CONVERSION_CHANGED')
        pending=_live_converter(folder,command)
        if pending:return {'pending':pending,'new':False}
        completed=folder/'validated.json';process=folder/'process.json'
        if process.exists():
            execution=json.loads(process.read_text(encoding='utf8'))
            if execution['command']!=command or execution.get('timed_out') or execution['returncode']!=0:raise BridgeError('EXPORT_CONVERSION_FAILED')
            if not target.is_file() or file_sha(target)!=execution['output_sha256']:raise BridgeError('EXPORT_MEMBER_CHANGED')
        else:
            execution={'command':command,'returncode':None,'adopted_by_readback':True}
            if (folder/'process-start.json').is_file():
                started=json.loads((folder/'process-start.json').read_text(encoding='utf8'))
                execution.update(pid=started['pid'],process_start=started['start'])
        if completed.exists():
            saved=json.loads(completed.read_text(encoding='utf8'))
            if saved['context_sha256']!=context_sha or not target.is_file() or file_sha(target)!=saved['sha256']:raise BridgeError('EXPORT_MEMBER_CHANGED')
            return {'file':target,'checks':saved['checks'],'execution':execution,'new':False}
        if target.is_file():return {'file':target,'execution':execution,'new':False}
    # Each attempt has a distinct output. A partially written old attempt is
    # retained; a retry never overwrites a file another converter could own.
    folder=root/('attempt-'+uuid.uuid4().hex);folder.mkdir();target=folder/NAMES[format]
    command=_command(context,format,target,stage/'source.eps')
    _record(pointer,{'context_sha256':context_sha,'directory':folder.name,'command':command})
    execution=_execute(command,folder,target)
    return {'file':target,'execution':execution,'new':True}


def _check_format(stage,context,format,target,unacknowledged=False):
    executable=Path(context['ghostscript']['executable']);paper=context['paper_mm'];pixels=context['size_px']
    diagnostics=stage/('check-'+format+'-'+uuid.uuid4().hex);diagnostics.mkdir()
    if format in {'tiff','jpeg'}:
        with rasterio.open(target) as raster:
            if [raster.width,raster.height]!=pixels or raster.count!=3 or raster.dtypes!=('uint8',)*3:raise BridgeError('PLOT_EXPORT_IMAGE_PROFILE')
            ink=sum(int(np.any(raster.read(window=window)<245,axis=0).sum()) for _,window in raster.block_windows(1))
        if not ink:raise BridgeError('PLOT_EXPORT_EMPTY_IMAGE')
        if unacknowledged:
            # A complete process receipt is unavailable after parent loss.
            # Verify every pixel against a diagnostic conversion, preserving
            # the original image and its timestamps. No native edit is replayed.
            reference=diagnostics/NAMES[format]
            _execute(_command(context,format,reference,stage/'source.eps'),diagnostics/'converter',reference)
            with rasterio.open(target) as original,rasterio.open(reference) as expected:
                if original.profile!=expected.profile:raise BridgeError('EXPORT_UNACKNOWLEDGED_IMAGE_CHANGED')
                if any(not np.array_equal(original.read(window=w),expected.read(window=w)) for _,w in original.block_windows(1)):
                    raise BridgeError('EXPORT_UNACKNOWLEDGED_IMAGE_CHANGED')
        return {'size_px':pixels,'dpi':context['dpi'],'ink_pixels':ink,'scope':'physical page; no georeferencing inferred'}
    with target.open('rb') as stream:
        if not stream.read(8).startswith(b'%PDF-'):raise BridgeError('PLOT_EXPORT_INVALID_PDF')
        stream.seek(max(0,target.stat().st_size-1024))
        if not stream.read().rstrip().endswith(b'%%EOF'):raise BridgeError('PLOT_EXPORT_INVALID_PDF')
    baseline=diagnostics/'baseline';baseline.mkdir();pdfcheck=diagnostics/'pdf';pdfcheck.mkdir()
    first=_render_check(executable,baseline,stage/'source.eps',paper);render=_render_check(executable,pdfcheck,target,paper)
    comparison=_pixel_comparison(first,render);screen_comparison=dict(comparison);comparison_dpi=72
    if (not (_matching_pixels(comparison) and comparison['fraction_within_5']>=.99)
        and math.prod(math.ceil(v*300/25.4) for v in paper)<=32_000_000):
        comparison_dpi=300;first=diagnostics/'baseline-print';first.mkdir();second=diagnostics/'pdf-print';second.mkdir()
        comparison=_pixel_comparison(_render_check(executable,first,stage/'source.eps',paper,dpi=300),
                                     _render_check(executable,second,target,paper,dpi=300))
    comparison.update(comparison_dpi=comparison_dpi,screen_comparison=screen_comparison)
    _record(stage/'pdf-comparison.json',comparison)
    if not (_matching_pixels(comparison) and comparison['fraction_within_5']>=.99):raise BridgeError('PLOT_EXPORT_PDF_VISUAL_CHANGE')
    return {'size_px_72dpi':render['size_px'],'preview_matches_eps':True,
            **comparison,'mean_channel_difference':comparison['pixel_mean_absolute_difference']}


def _advance(bridge,request_id,stage,context,recovered=False):
    receipt=bridge.status(request_id);payload=receipt['payload'];context_sha=payload['context_sha256']
    if file_sha(stage/'export-context.json')!=context_sha:raise BridgeError('EXPORT_CONTEXT_CHANGED')
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in receipt['sources']):raise BridgeError('STALE_SOURCE')
    if file_sha(stage/'source.eps')!=payload['expected_sha256']:raise BridgeError('EXPORT_SNAPSHOT_CHANGED')
    if _gs_identity(_ghostscript(bridge.root))!=context['ghostscript']:raise BridgeError('EXPORT_PRODUCER_CHANGED')
    files=_scoped(stage,stage/'files');files.mkdir(exist_ok=True);names={NAMES[f] for f in context['formats']}
    if any(p.name not in names or p.is_symlink() or not p.is_file() for p in files.iterdir()):raise BridgeError('EXPORT_MEMBER_CHANGED')
    checks={};executions={};reused=[];converted=[]
    for format in context['formats']:
        candidate=_candidate(stage,context,format,context_sha)
        if 'pending' in candidate:return receipt|{'pending_format':format,'converter':candidate['pending'],'replayed':False}
        target=candidate['file'];output=files/NAMES[format]
        if output.exists() and file_sha(output)!=file_sha(target):raise BridgeError('EXPORT_MEMBER_CHANGED')
        if 'checks' not in candidate:
            try:check=_check_format(stage,context,format,target,candidate['execution'].get('returncode') is None)
            except (BridgeError,rasterio.errors.RasterioIOError) as error:
                if candidate['execution'].get('returncode') is not None:raise
                if isinstance(error,BridgeError) and not str(error).startswith(('PLOT_EXPORT_INVALID_PDF','PLOT_EXPORT_IMAGE_PROFILE',
                    'PLOT_EXPORT_EMPTY_IMAGE','PLOT_EXPORT_PDF_VISUAL_CHANGE','EXPORT_UNACKNOWLEDGED_IMAGE_CHANGED')):raise
                # An unacknowledged partial file is not a completed format.
                # Keep it and produce one fresh, isolated output from the EPS.
                partial={'file':str(target),'sha256':file_sha(target),'reason':str(error)}
                candidate=_candidate(stage,context,format,context_sha,fresh=True);target=candidate['file']
                candidate['execution']['retained_incomplete_candidate']=partial
                check=_check_format(stage,context,format,target)
            _record(target.parent/'validated.json',{'context_sha256':context_sha,'sha256':file_sha(target),'checks':check})
        else:check=candidate['checks']
        if output.exists() and file_sha(output)!=file_sha(target):raise BridgeError('EXPORT_MEMBER_CHANGED')
        if not output.exists():
            publishing=stage/('publishing-'+uuid.uuid4().hex);publishing.mkdir();temporary=publishing/output.name
            shutil.copyfile(target,temporary)
            if file_sha(temporary)!=file_sha(target):raise BridgeError('EXPORT_COPY_CHANGED')
            os.rename(temporary,output)
        checks[format]=check;executions[format]=candidate['execution']
        (converted if candidate['new'] else reused).append(format)
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in receipt['sources']):raise BridgeError('STALE_SOURCE')
    if _gs_identity(_ghostscript(bridge.root))!=context['ghostscript']:raise BridgeError('EXPORT_PRODUCER_CHANGED')
    return finish_bundle(bridge,request_id,files,{'backend':'Ghostscript from native EPS','eps':validate_eps(stage/'source.eps',context['paper_mm']),
        'paper_mm':context['paper_mm'],'checks':checks,'source_preserved':True,'pdf_image_encoding':'lossless Flate; no image downsampling',
        'conversions':executions,'reconciled_after_interruption':recovered,'reused_formats':reused,'newly_converted_formats':converted,
        'recovery_scope':'Completed formats and native source are preserved; missing/incomplete formats use fresh conversion attempts; an unacknowledged image may require a separate readback conversion.'})


def prepare_plot_export(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
    if not isinstance(parameters,dict) or set(parameters)-{'formats','dpi'}:
        raise BridgeError('PLOT_EXPORT_PARAMETERS')
    formats=parameters.get('formats',['pdf','tiff']);dpi=parameters.get('dpi',300)
    if (not isinstance(formats,list) or not formats or any(type(f) is not str or f not in ('pdf','tiff','jpeg') for f in formats)
            or len(set(formats))!=len(formats) or type(dpi) is not int or not 72<=dpi<=600):
        raise BridgeError('PLOT_EXPORT_FORMAT: unique pdf/tiff/jpeg formats and integer dpi in 72..600 required')
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.suffix.lower()!='.eps':raise BridgeError('PLOT_EXPORT_EPS_REQUIRED')
    with source.open('rb') as stream:header=stream.read(4096)
    bounds=re.search(rb'%%HiResBoundingBox:\s*([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)',header)
    if not bounds:raise BridgeError('PLOT_EXPORT_NATIVE_PAGE_REQUIRED')
    paper=[float(v)*25.4/72 for v in bounds.groups()[2:]]
    validate_eps(source,paper)
    pixels=[round(v*dpi/25.4) for v in paper]
    if pixels[0]*pixels[1]>100_000_000:raise BridgeError('PLOT_EXPORT_TOO_LARGE: lower dpi or split the physical page')
    primary='map.pdf' if 'pdf' in formats else 'map.tif' if 'tiff' in formats else 'map.jpg'
    context={'formats':formats,'dpi':dpi,'paper_mm':paper,'size_px':pixels,'ghostscript':_gs_identity(_ghostscript(bridge.root)),
             'pdf_image_options':list(LOSSLESS_PDF_IMAGE_OPTIONS)}
    payload={'operation':'plot.export','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,'export_protocol':4,
             'context_sha256':sha(json_text(context).encode('utf8'))}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(source),'sha256':expected_sha256}],primary)
    if not new:return receipt
    try:
        owner=psutil.Process();_record(stage/'producer.json',{'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()})
        _record(stage/'export-context.json',context);shutil.copyfile(source,stage/'source.eps')
        return _advance(bridge,request_id,stage,context)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def reconcile_plot_export(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if receipt['payload'].get('export_protocol')!=4:raise BridgeError('EXPORT_RECOVERY_CONTEXT_MISSING: retain older unsealed request and use a fresh branch')
    if receipt['state']=='RUNNING':
        owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(owner['pid'])
            if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
        except psutil.NoSuchProcess:pass
    if file_sha(stage/'export-context.json')!=receipt['payload']['context_sha256']:raise BridgeError('EXPORT_CONTEXT_CHANGED')
    context=json.loads((stage/'export-context.json').read_text(encoding='utf8'))
    return _advance(bridge,request_id,stage,context,recovered=True)
