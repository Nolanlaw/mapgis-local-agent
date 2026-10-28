"""Native EPS composition through verified winout60/mapdsp60 exports."""
from __future__ import annotations
import json,math,re,subprocess
from pathlib import Path
import cv2
import numpy as np
from .core import Bridge,BridgeError,json_text,native_ids,read_native,sha
from .native_backend import KINDS,backend_configuration,_run_child


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


def _render_check(executable: Path,folder: Path,eps: Path,paper: list[float]) -> dict:
    # Bounded independent rasterization. A successful native call may still be blank.
    preview=folder/'preview.png'
    command=[str(executable),'-dSAFER','-dBATCH','-dNOPAUSE','-sDEVICE=png16m','-dEPSCrop','-r72','-sOutputFile='+str(preview),str(eps)]
    try:
        result=subprocess.run(command,capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError('PLOT_RENDER_TIMEOUT') from exc
    (folder/'ghostscript.json').write_text(json_text({'returncode':result.returncode,'stdout':result.stdout.decode('utf8',errors='replace'),'stderr':result.stderr.decode('utf8',errors='replace')}),encoding='utf8')
    if result.returncode or not preview.is_file():raise BridgeError('PLOT_RENDER_FAILED: independent EPS interpretation failed')
    image=cv2.imdecode(np.frombuffer(preview.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise BridgeError('PLOT_RENDER_FAILED: invalid preview pixels')
    expected=[round(paper[1]*72/25.4),round(paper[0]*72/25.4)]
    if any(abs(image.shape[i]-expected[i])>1 for i in (0,1)):raise BridgeError('PLOT_RENDER_PAGE: rasterized size differs')
    mask=np.any(image<245,axis=2)
    ys,xs=np.where(mask)
    if not len(xs):raise BridgeError('EMPTY_PLOT: native output contains no visible marks; inspect styles, frame, symbols and fonts')
    return {'preview':str(preview),'size_px':[int(image.shape[1]),int(image.shape[0])],
            'ink_pixels':int(mask.sum()),'ink_bounds_px':[int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())],
            'independent_interpretation':'Ghostscript','geological_review':'REQUIRED_FOR_SOURCE_DERIVED_MAPS'}


def prepare_native_plot(bridge: Bridge,layers: list[dict],view: dict,request_id: str) -> dict:
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
    payload={'version':1,'kind':'NATIVE_PLOT','layers':sources,'view':view}
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
             'sources':sources,'output':str(output),'view':view,'stage':str(stage),
             'mode':'isolated_native_x86','renderer':'MapGIS winout60/mapdsp60; EPS text as curves',
             'rollback':'new output branch only; all input files preserved'}
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
        execution,events=_run_child([str(executable),'plot',str(dll.parent),str(stage/'layers.txt'),str(stage/'view.txt'),str(candidate)],stage,'native-plot.json')
        drawn=[e for e in events if e.get('event')=='layer_drawn']
        if len(drawn)!=len(sources) or any(e.get('index')!=i or e.get('kind')!=KINDS[sources[i]['type']] or e.get('records')!=sources[i]['count'] for i,e in enumerate(drawn)):
            raise BridgeError('PLOT_PROTOCOL: native layer receipt differs')
        eps=validate_eps(candidate,view['paper_mm'])
        pixels=_render_check(ghostscript,stage,candidate,view['paper_mm'])
        if any(sha(Path(source['file']).read_bytes())!=source['sha256'] for source in sources):raise BridgeError('STALE_DURING_PLOT')
        receipt.update(candidate_sha256=eps['sha256'],eps=eps,render_check=pixels,native_execution=execution,source_preserved=True)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='PREPARED',after_blob=?,receipt=? WHERE id=? AND state='RUNNING'",(candidate.read_bytes(),json_text(receipt),request_id))
    except Exception as exc:
        receipt['failure']=str(exc)
        with bridge.connect() as db:db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state='RUNNING'",(json_text(receipt),request_id))
        raise
    return {**receipt,'state':'PREPARED'}
