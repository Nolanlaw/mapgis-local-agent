"""Native EPS composition through verified winout60/mapdsp60 exports."""
from __future__ import annotations
import json,math,re,subprocess
from contextlib import ExitStack
from pathlib import Path
import cv2
import numpy as np
from .core import Bridge,BridgeError,json_text,native_ids,read_native,sha
from .native_backend import KINDS,backend_configuration,_run_child
from .native_paths import NativePathAlias
from .symbol_library import freeze_system_library,snapshot_system_library

# Automatic DCT selection otherwise adds a JPEG generation to EPS/PDF imagery.
LOSSLESS_PDF_IMAGE_OPTIONS=('-dAutoFilterColorImages=false','-dColorImageFilter=/FlateEncode',
    '-dAutoFilterGrayImages=false','-dGrayImageFilter=/FlateEncode',
    '-dDownsampleColorImages=false','-dDownsampleGrayImages=false','-dDownsampleMonoImages=false')
LOSSLESS_PS_IMAGE_OPTIONS=tuple(v.replace('/FlateEncode','/LZWEncode') for v in LOSSLESS_PDF_IMAGE_OPTIONS)


def _view(view: dict) -> dict:
    if not isinstance(view,dict) or set(view)!={'native_bounds','paper_mm','coordinate_frame'} or view.get('coordinate_frame')!='native':
        raise BridgeError('PLOT_VIEW: native_bounds, paper_mm and coordinate_frame=native required; use native_file_info for the native bounds')
    bounds,paper=view['native_bounds'],view['paper_mm']
    if (not isinstance(bounds,list) or len(bounds)!=4 or not isinstance(paper,list) or len(paper)!=2
        or any(type(x) not in (int,float) or not math.isfinite(x) for x in bounds+paper)
        or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]
        or any(not math.isfinite(bounds[i+2]-bounds[i]) for i in (0,1))
        or any(x<1 or x>2000 for x in paper)):
        raise BridgeError('PLOT_VIEW: finite increasing native bounds and paper_mm in 1..2000 required')
    return view


def validate_eps(path: Path,paper: list[float]) -> dict:
    blob=path.read_bytes()
    if not blob.startswith(b'%!PS-Adobe-3.0 EPSF-3.0') or not blob.rstrip().endswith(b'%%EOF'):
        raise BridgeError('EPS_INCOMPLETE: native EPS header/footer missing')
    match=re.search(rb'%%HiResBoundingBox:\s*([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)',blob[:4096])
    if not match:
        raise BridgeError('EPS_PAGE: missing native physical bounds')
    box=[float(x) for x in match.groups()]
    if any(not math.isfinite(v) for v in box) or abs(box[0])>0.01 or abs(box[1])>0.01 or any(abs(box[i+2]-paper[i]*72/25.4)>0.03 for i in range(2)):
        raise BridgeError('EPS_PAGE: native page differs from requested paper_mm')
    return {'sha256':sha(blob),'bytes':len(blob),'hires_bounding_box_pt':box,'native_eps_header_and_eof':True}


def _ghostscript(root: Path) -> Path:
    developer=Path(__file__).resolve().parents[2]
    for base in (root,developer):
        for relative in ('runtime/ghostscript/bin/gswin64c.exe','work/native-output-runtime/ghostscript/bin/gswin64c.exe'):
            executable=base/relative
            if executable.is_file():return executable
    raise BridgeError('GHOSTSCRIPT_MISSING: restore the packaged output runtime')


def _render_check(executable: Path,folder: Path,eps: Path,paper: list[float],*,dpi:int=72,page:int|None=None) -> dict:
    # Bounded independent rasterization. A successful native call may still be blank.
    preview=folder/'preview.png'
    command=[str(executable),'-dSAFER','-dBATCH','-dNOPAUSE','-sDEVICE=png16m','-dEPSCrop','-r'+str(dpi),
             *([] if page is None else ['-dFirstPage='+str(page),'-dLastPage='+str(page)]),'-sOutputFile='+str(preview),str(eps)]
    try:
        result=subprocess.run(command,capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError('PLOT_RENDER_TIMEOUT') from exc
    (folder/'ghostscript.json').write_text(json_text({'returncode':result.returncode,'stdout':result.stdout.decode('utf8',errors='replace'),'stderr':result.stderr.decode('utf8',errors='replace')}),encoding='utf8')
    if result.returncode or not preview.is_file():raise BridgeError('PLOT_RENDER_FAILED: independent EPS interpretation failed')
    image=cv2.imdecode(np.frombuffer(preview.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise BridgeError('PLOT_RENDER_FAILED: invalid preview pixels')
    expected=[round(paper[1]*dpi/25.4),round(paper[0]*dpi/25.4)]
    if any(abs(image.shape[i]-expected[i])>1 for i in (0,1)):raise BridgeError('PLOT_RENDER_PAGE: rasterized size differs')
    mask=np.any(image<245,axis=2)
    ys,xs=np.where(mask)
    if not len(xs):raise BridgeError('EMPTY_PLOT: native output contains no visible marks; inspect styles, frame, symbols and fonts')
    return {'preview':str(preview),'size_px':[int(image.shape[1]),int(image.shape[0])],'dpi':dpi,
            'ink_pixels':int(mask.sum()),'ink_bounds_px':[int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())],
            'independent_interpretation':'Ghostscript','geological_review':'REQUIRED_FOR_SOURCE_DERIVED_MAPS'}


def _pixel_comparison(a:dict,b:dict)->dict:
    first=cv2.imdecode(np.frombuffer(Path(a['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    second=cv2.imdecode(np.frombuffer(Path(b['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    if first.shape!=second.shape:raise BridgeError('POSTSCRIPT_CONVERSION_PIXELS: page dimensions differ')
    delta=np.abs(first.astype(np.int16)-second)
    ink_a=np.any(first<245,axis=2);ink_b=np.any(second<245,axis=2)
    kernel=np.ones((3,3),np.uint8)
    # At the comparison resolution allow at most one pixel of edge movement,
    # while independently requiring overlapping colors and page-wide agreement.
    unmatched=int((ink_a&~cv2.dilate(ink_b.astype(np.uint8),kernel).astype(bool)).sum()
                 +(ink_b&~cv2.dilate(ink_a.astype(np.uint8),kernel).astype(bool)).sum())
    overlap=ink_a&ink_b
    return {'pixel_mean_absolute_difference':float(delta.mean()),
            'fraction_within_5':float(np.mean(np.all(delta<=5,axis=2))),
            'ink_count_relative_difference':abs(a['ink_pixels']-b['ink_pixels'])/max(a['ink_pixels'],1),
            'pixel_max_difference':int(delta.max()),'unmatched_ink_pixels_beyond_one_pixel':unmatched,
            'overlapping_mark_color_mean_difference':float(delta[overlap].mean()) if np.any(overlap) else float('inf')}


def _matching_pixels(comparison:dict)->bool:
    return (comparison['pixel_mean_absolute_difference']<=1
            and comparison['unmatched_ink_pixels_beyond_one_pixel']==0
            and comparison['overlapping_mark_color_mean_difference']<=2)


def normalize_native_ps(executable:Path,folder:Path,source:Path,target:Path,paper:list[float])->dict:
    """Interpret native Level-2 PS, retaining vectors and the requested page.

    EPS2write's ink box can include a tiling-pattern cell's untransformed bounds.
    Our contract is the full physical page, not a tight ink crop. Native DSC and
    PageSize must first agree with that page; then only the EPS DSC page bounds
    are set explicitly. The PDF MediaBox/clip and all vector streams stay intact.
    A second interpretation compares actual native-PS and final-EPS pixels.
    """
    raw=source.read_bytes()
    if not raw.rstrip().endswith(b'%EOF'):raise BridgeError('POSTSCRIPT_INCOMPLETE')
    bounds=re.search(rb'%%BoundingBox:\s*([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)',raw[:1024])
    page=re.search(rb'/PageSize\s*\[\s*([\d.-]+)\s+([\d.-]+)\s*\]',raw[:1024])
    expected=[v*72/25.4 for v in paper]
    if bounds is None or page is None:raise BridgeError('POSTSCRIPT_PAGE: missing native physical page')
    box=[float(v) for v in bounds.groups()];size=[float(v) for v in page.groups()]
    if (any(not math.isfinite(v) for v in box+size) or any(abs(v)>.01 for v in box[:2])
        or any(abs(box[i+2]-expected[i])>.03 or abs(size[i]-expected[i])>.03 for i in range(2))):
        raise BridgeError('POSTSCRIPT_PAGE: native page differs from requested paper_mm')
    converted=folder/'normalized-full-page.eps'
    command=[str(executable),'-dSAFER','-dBATCH','-dNOPAUSE','-sDEVICE=eps2write',
             '-dAutoRotatePages=/None',*LOSSLESS_PS_IMAGE_OPTIONS,'-sOutputFile='+str(converted),str(source)]
    try:result=subprocess.run(command,capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:raise BridgeError('POSTSCRIPT_CONVERSION_TIMEOUT') from exc
    (folder/'native-ps-normalize.json').write_text(json_text({'args':command,'returncode':result.returncode,
        'stdout':result.stdout.decode('utf8',errors='replace'),'stderr':result.stderr.decode('utf8',errors='replace')}),encoding='utf8')
    if result.returncode or not converted.is_file():raise BridgeError('POSTSCRIPT_CONVERSION_FAILED')
    eps=converted.read_bytes()
    old=re.search(rb'(?m)^%%HiResBoundingBox:[^\r\n]+',eps[:4096])
    integer=('%%BoundingBox: 0 0 '+str(math.ceil(expected[0]))+' '+str(math.ceil(expected[1]))).encode('ascii')
    precise=('%%HiResBoundingBox: 0 0 '+format(expected[0],'.9f')+' '+format(expected[1],'.9f')).encode('ascii')
    eps,n1=re.subn(rb'(?m)^%%BoundingBox:[^\r\n]+',integer,eps,count=1)
    eps,n2=re.subn(rb'(?m)^%%HiResBoundingBox:[^\r\n]+',precise,eps,count=1)
    if n1!=1 or n2!=1:raise BridgeError('POSTSCRIPT_CONVERSION_DSC')
    target.write_bytes(eps);validate_eps(target,paper)
    native_check=folder/'native-ps-check';native_check.mkdir()
    eps_check=folder/'normalized-eps-check';eps_check.mkdir()
    a=_render_check(executable,native_check,source,paper);b=_render_check(executable,eps_check,target,paper)
    comparison=_pixel_comparison(a,b);screen_comparison=dict(comparison);dpi=72
    # A dense native tiling cell can alias differently after vector re-encoding
    # at screen resolution. Keep the original EPS vectors and all thresholds;
    # repeat the same gate at 300 dpi (0.085 mm per pixel) before accepting it.
    # Bound this diagnostic to the existing screen-check maximum page pixels.
    if not _matching_pixels(comparison) and math.prod(math.ceil(v*300/25.4) for v in paper)<=32_000_000:
        dpi=300;native_print=folder/'native-ps-print-check';native_print.mkdir()
        eps_print=folder/'normalized-eps-print-check';eps_print.mkdir()
        comparison=_pixel_comparison(_render_check(executable,native_print,source,paper,dpi=dpi),
                                     _render_check(executable,eps_print,target,paper,dpi=dpi))
    comparison.update(comparison_dpi=dpi,screen_comparison=screen_comparison)
    (folder/'native-ps-pixel-comparison.json').write_text(json_text(comparison),encoding='utf8')
    if not _matching_pixels(comparison):raise BridgeError('POSTSCRIPT_CONVERSION_PIXELS: native and final visible marks differ')
    return {'native_postscript_sha256':sha(raw),'native_page_mm':paper,'eps_page_policy':'full physical page',
            'interpreter_ink_box':old.group().decode('ascii') if old else None,
            **comparison,'source':'MapGIS native curve PostScript; Ghostscript eps2write'}


def prepare_native_plot(bridge: Bridge,layers: list[dict],view: dict,request_id: str,system_library:dict|None=None) -> dict:
    view=_view(view)
    request_id=bridge.request_id(request_id)
    if not isinstance(layers,list) or not 1<=len(layers)<=100:raise BridgeError('PLOT_LAYERS: 1..100 ordered saved layers required')
    sources=[];snapshots=[]
    for layer in layers:
        if not isinstance(layer,dict) or set(layer)!={'file','expected_sha256'}:raise BridgeError('PLOT_LAYERS: file and expected_sha256 required')
        path=Path(layer['file']).resolve(strict=True)
        blob,reader,_=read_native(path)
        if sha(blob)!=layer['expected_sha256']:raise BridgeError('STALE_SOURCE: plot layer changed')
        ids=native_ids(reader)
        if len(ids)>1000000:raise BridgeError('PLOT_LAYERS: too many records for one plot')
        sources.append({'file':str(path),'sha256':sha(blob),'type':reader.shapeType,'count':len(ids)})
        snapshots.append((blob,reader.shapeType,ids,path.suffix.lower()))
    library,library_sources=freeze_system_library(system_library)
    layer_sources=list(sources);sources+=library_sources
    payload={'version':2,'kind':'NATIVE_PLOT','layers':layer_sources,'view':view,'system_library':system_library}
    encoded=json_text(payload).encode('utf8');payload_hash=sha(encoded)
    with bridge.connect() as db:
        old=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
        if old:
            if old['payload_hash']!=payload_hash:raise BridgeError('REQUEST_ID_CONFLICT')
            return bridge._receipt(old)
    executable,dll=backend_configuration(bridge.root)
    ghostscript=_ghostscript(bridge.root)
    stage=bridge.destination_path(bridge.root/'work/native-plots'/sha(request_id.encode())[:24])
    output=bridge.destination_path(bridge.root/'deliverables/plots'/request_id/'map.eps')
    manifest=stage/'source-manifest.json'
    receipt={'id':request_id,'kind':'NATIVE_PLOT','source':str(manifest),'source_sha256':sha(encoded),
             'sources':sources,'layers':layer_sources,'output':str(output),'view':view,'stage':str(stage),
             'mode':'isolated_native_x86','plot_protocol':'native_level2_ps_lossless','renderer':'MapGIS winout60/mapdsp60; native text as curves; Ghostscript standard EPS',
             'rollback':'new output branch only; all input files preserved'}
    if library:receipt['system_library']={'folder':library['folder'],'manifest_sha256':library['manifest_sha256']}
    with bridge.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
        if old:
            if old['payload_hash']!=payload_hash:raise BridgeError('REQUEST_ID_CONFLICT')
            return bridge._receipt(old)
        if stage.exists():raise BridgeError('PLOT_STAGE_CONFLICT')
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',(request_id,payload_hash,'RUNNING',str(manifest),encoded,b'','[]',json_text(receipt)))
    try:
        stage.mkdir(parents=True);manifest.write_bytes(encoded)
        commands=[str(len(snapshots))]
        for index,(blob,kind,ids,suffix) in enumerate(snapshots):
            name=f'layer{index:03d}'+suffix
            (stage/name).write_bytes(blob)
            commands.append(f"{KINDS[kind]} {name} {len(ids)} "+' '.join(map(str,ids)))
        (stage/'layers.txt').write_text('\n'.join(commands)+'\n',encoding='ascii')
        (stage/'view.txt').write_text(' '.join(map(repr,view['native_bounds']+view['paper_mm'])),encoding='ascii')
        candidate=stage/'candidate.eps'
        postscript=stage/'candidate.ps'
        with ExitStack() as stack:
            alias=stack.enter_context(NativePathAlias(stage))
            args=[str(executable),'plot.ps',str(dll.parent),str(alias.path/'layers.txt'),str(alias.path/'view.txt'),str(alias.path/'candidate.ps')]
            if library:
                snapshot_system_library(library,stage);args.append(str(alias.path/'system-library'))
            execution,events=_run_child(args,stage,'native-plot.json')
        drawn=[e for e in events if e.get('event')=='layer_drawn']
        if len(drawn)!=len(layer_sources) or any(e.get('index')!=i or e.get('kind')!=KINDS[layer_sources[i]['type']] or e.get('records')!=layer_sources[i]['count'] for i,e in enumerate(drawn)):
            raise BridgeError('PLOT_PROTOCOL: native layer receipt differs')
        saved=[e for e in events if e.get('event')=='plot_saved']
        if len(saved)!=1 or saved[0]['sha256']!=sha(postscript.read_bytes()):raise BridgeError('PLOT_POSTSCRIPT_CHANGED')
        normalization=normalize_native_ps(ghostscript,stage,postscript,candidate,view['paper_mm'])
        eps=validate_eps(candidate,view['paper_mm'])
        pixels=_render_check(ghostscript,stage,candidate,view['paper_mm'])
        if any(sha(Path(source['file']).read_bytes())!=source['sha256'] for source in sources):raise BridgeError('STALE_DURING_PLOT')
        receipt.update(candidate_sha256=eps['sha256'],eps=eps,render_check=pixels,normalization=normalization,native_execution=execution,source_preserved=True)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='PREPARED',after_blob=?,receipt=? WHERE id=? AND state='RUNNING'",(candidate.read_bytes(),json_text(receipt),request_id))
    except Exception as exc:
        receipt['failure']=str(exc)
        with bridge.connect() as db:db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state='RUNNING'",(json_text(receipt),request_id))
        raise
    return {**receipt,'state':'PREPARED'}
