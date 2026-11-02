"""Ordered native MPJ views, portable editable source and checked multi-page PDF."""
from __future__ import annotations
import csv,io,json,math,os,shutil,subprocess,uuid
from pathlib import Path
import cv2,numpy as np,psutil,rasterio
from .core import Bridge,BridgeError,json_text,sha
from .raster import file_sha
from .tables import write_literal_workbook
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .native_backend import backend_configuration
from .native_project import _plot_inputs,native_project_info,prepare_project_plot,reconcile_project_plot
from .native_plot import _view,_ghostscript,_render_check,_pixel_comparison,_matching_pixels,LOSSLESS_PDF_IMAGE_OPTIONS

def _parameters(parameters):
    if not isinstance(parameters,dict) or set(parameters)-{'pages','dpi','include_tiff'}:raise BridgeError('MAP_SERIES_PARAMETERS')
    pages=parameters.get('pages');dpi=parameters.get('dpi',300);tiff=parameters.get('include_tiff',False)
    if not isinstance(pages,list) or not 1<=len(pages)<=100:raise BridgeError('MAP_SERIES_PAGES:1..100 explicitly ordered pages')
    if type(dpi) is not int or not 72<=dpi<=600 or type(tiff) is not bool:raise BridgeError('MAP_SERIES_PARAMETERS: integer dpi72..600 and boolean include_tiff')
    names=set()
    for page in pages:
        if not isinstance(page,dict) or set(page)!={'name','view'} or not isinstance(page['name'],str) or not page['name'].strip() or len(page['name'])>128 or any(ord(c)<32 and c not in '\t\n\r' for c in page['name']):
            raise BridgeError('MAP_SERIES_PAGE: unique nonempty name<=128 characters and explicit view required')
        if page['name'] in names:raise BridgeError('MAP_SERIES_PAGE: duplicate name')
        names.add(page['name']);_view(page['view'])
        if math.prod(round(v*dpi/25.4) for v in page['view']['paper_mm'])>100_000_000:raise BridgeError('MAP_SERIES_TOO_LARGE: lower dpi or split the physical page')
    return {'pages':pages,'dpi':dpi,'include_tiff':tiff}

def _copy(source,target,digest):
    if target.exists():
        if file_sha(target)!=digest:raise BridgeError('MAP_SERIES_MEMBER_CHANGED: '+target.name)
    else:shutil.copyfile(source,target)
    if file_sha(target)!=digest:raise BridgeError('MAP_SERIES_COPY_CHANGED: '+target.name)

def _run(executable,args,log):
    command=[str(executable),'-dSAFER','-dBATCH','-dNOPAUSE',*args]
    try:result=subprocess.run(command,capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:raise BridgeError('MAP_SERIES_CONVERSION_TIMEOUT') from error
    log.write_text(json_text({'args':command,'exit_code':result.returncode,'stdout':result.stdout.decode('utf8',errors='replace'),
        'stderr':result.stderr.decode('utf8',errors='replace')}),encoding='utf8')
    if result.returncode:raise BridgeError('MAP_SERIES_CONVERSION_FAILED: '+str(log))

def _merge_pdf(executable,stage,files,eps_files):
    target=files/'series.pdf';record=stage/'merged.json';inputs=[{'name':f.name,'sha256':file_sha(f)} for f in eps_files]
    if record.exists():
        cached=json.loads(record.read_text(encoding='utf8'))
        if cached['inputs']!=inputs or file_sha(target)!=cached['sha256']:raise BridgeError('MAP_SERIES_MERGE_CHANGED')
        return cached
    if target.exists():
        retained=stage/('incomplete-pdf-'+uuid.uuid4().hex[:8]+'.pdf')
        if target.parent!=stage/'files' or retained.parent!=stage:raise BridgeError('MAP_SERIES_STAGE_IDENTITY')
        target.rename(retained)
    _run(executable,['-dEPSCrop','-dAutoRotatePages=/None','-sDEVICE=pdfwrite',*LOSSLESS_PDF_IMAGE_OPTIONS,
        '-sOutputFile='+str(target),*[str(f) for f in eps_files]],stage/'merge-log.json')
    with target.open('rb') as stream:
        if not stream.read(8).startswith(b'%PDF-'):raise BridgeError('MAP_SERIES_INVALID_PDF')
    cached={'inputs':inputs,'sha256':file_sha(target)};temporary=stage/('merged-'+uuid.uuid4().hex[:8]+'.tmp')
    with temporary.open('x',encoding='utf8') as stream:stream.write(json_text(cached));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,record);return cached

def _image_info(file,paper,dpi):
    image=cv2.imdecode(np.frombuffer(file.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    expected=[round(paper[1]*dpi/25.4),round(paper[0]*dpi/25.4)]
    if image is None or any(abs(image.shape[i]-expected[i])>1 for i in (0,1)):raise BridgeError('MAP_SERIES_PAGE_SIZE')
    ink=int(np.any(image<245,axis=2).sum())
    if not ink:raise BridgeError('MAP_SERIES_EMPTY_PAGE')
    return {'preview':str(file),'ink_pixels':ink,'size_px':[int(image.shape[1]),int(image.shape[0])],'dpi':dpi}

def _check_pages(executable,stage,files,parameters,plots):
    diagnostics=stage/('pdf-check-'+uuid.uuid4().hex[:8]);diagnostics.mkdir();document=files/'series.pdf'
    _run(executable,['-sDEVICE=png16m','-r72','-sOutputFile='+str(diagnostics/'page-%03d.png'),str(document)],diagnostics/'render-log.json')
    images=sorted(diagnostics.glob('page-*.png'));count=len(parameters['pages'])
    if [v.name for v in images]!=[f'page-{i:03d}.png' for i in range(1,count+1)]:raise BridgeError('MAP_SERIES_PAGE_COUNT')
    results=[]
    for i,(page,plot,image) in enumerate(zip(parameters['pages'],plots,images),1):
        paper=page['view']['paper_mm'];info=_image_info(image,paper,72);comparison=_pixel_comparison(plot['render_check'],info);dpi=72
        if not _matching_pixels(comparison) and math.prod(math.ceil(v*300/25.4) for v in paper)<=32_000_000:
            before=diagnostics/f'eps-{i:03d}';after=diagnostics/f'pdf-{i:03d}';before.mkdir();after.mkdir();dpi=300
            comparison=_pixel_comparison(_render_check(executable,before,files/f'page-{i:03d}.eps',paper,dpi=dpi),
                _render_check(executable,after,document,paper,dpi=dpi,page=i))
        if not _matching_pixels(comparison):raise BridgeError('MAP_SERIES_PIXELS: PDF page differs from original native EPS; '+str(i))
        _copy(image,files/image.name,file_sha(image));tiff=None
        if parameters['include_tiff']:
            tiff=files/f'page-{i:03d}.tif'
            candidate=diagnostics/tiff.name if tiff.exists() else tiff
            _run(executable,['-sDEVICE=tiff24nc','-r'+str(parameters['dpi']),'-dFirstPage='+str(i),'-dLastPage='+str(i),
                '-sOutputFile='+str(candidate),str(document)],diagnostics/f'tiff-{i:03d}.json')
            with rasterio.open(candidate) as raster:
                if raster.count!=3 or raster.dtypes!=('uint8',)*3 or [raster.width,raster.height]!=[round(v*parameters['dpi']/25.4) for v in paper]:raise BridgeError('MAP_SERIES_TIFF_PROFILE')
                if not any(np.any(raster.read(window=window)<245) for _,window in raster.block_windows(1)):raise BridgeError('MAP_SERIES_EMPTY_TIFF')
                if candidate!=tiff:
                    with rasterio.open(tiff) as existing:
                        if (existing.count!=raster.count or existing.dtypes!=raster.dtypes or existing.shape!=raster.shape
                            or existing.crs!=raster.crs or existing.transform!=raster.transform
                            or any(existing.tags().get(k)!=raster.tags().get(k) for k in ('TIFFTAG_XRESOLUTION','TIFFTAG_YRESOLUTION','TIFFTAG_RESOLUTIONUNIT'))
                            or any(not np.array_equal(existing.read(window=window),raster.read(window=window)) for _,window in raster.block_windows(1))):
                            raise BridgeError('MAP_SERIES_TIFF_CHANGED')
        results.append({'number':i,'name':page['name'],'view':page['view'],'paper_mm':paper,
            'native_display_scale':plot['native_display_scale'],'native_execution':plot['native_execution'],
            'eps':f'page-{i:03d}.eps','preview':image.name,'tiff':None if tiff is None else tiff.name,
            'pdf_matches_native_eps':True,'comparison_dpi':dpi,'comparison':comparison,'preview_size_px':info['size_px']})
    return results

def _advance(bridge,request_id,stage,context,parameters,recovered=False):
    receipt=bridge.status(request_id)
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in receipt['sources']):raise BridgeError('STALE_SOURCE')
    files=bridge.destination_path(stage/'files');files.mkdir(exist_ok=True)
    for source in receipt['sources']:_copy(Path(source['file']),files/Path(source['file']).name,source['sha256'])
    project=files/Path(context['info']['file']).name;readback=native_project_info(bridge.root,str(project),context['info']['source_sha256'])
    if any(Path(layer['file']).parent!=files for layer in readback['layers']):raise BridgeError('MAP_SERIES_NONPORTABLE_PROJECT')
    if any(any(a.get(k)!=b.get(k) for k in ('kind','active','state','display_limits','sha256')) for a,b in zip(context['info']['layers'],readback['layers'])) or len(readback['layers'])!=len(context['info']['layers']):raise BridgeError('MAP_SERIES_PROJECT_REOPEN_DIFF')
    _,bas=backend_configuration(bridge.root);renderer=Bridge(stage/'renderer');config=renderer.root/'config.local.json'
    expected=json_text({'mapgis_installation':str(bas.parent.parent),'native_backend_legacy_dep_compatibility':True})
    if config.exists() and config.read_text(encoding='utf8')!=expected:raise BridgeError('MAP_SERIES_RENDERER_CONFIG_CHANGED')
    if not config.exists():config.write_text(expected,encoding='utf8')
    plots=[];eps_files=[]
    for i,page in enumerate(parameters['pages'],1):
        identity=f'page-{i:03d}-plot'
        with renderer.connect() as db:row=db.execute('SELECT state FROM commands WHERE id=?',(identity,)).fetchone()
        if row is None:prepare_project_plot(renderer,str(project),context['info']['source_sha256'],page['view'],identity)
        elif row['state'] in {'RUNNING','FAILED'}:reconcile_project_plot(renderer,identity)
        result=renderer.apply(identity);target=files/f'page-{i:03d}.eps';_copy(Path(result['output']),target,result['candidate_sha256'])
        plots.append(result);eps_files.append(target)
    executable=_ghostscript(bridge.root);merged=_merge_pdf(executable,stage,files,eps_files)
    pages=_check_pages(executable,stage,files,parameters,plots)
    manifest={'schema':'mapgis-agent/map-series/1','project':project.name,'project_sha256':context['info']['source_sha256'],
        'pages':[{k:v for k,v in page.items() if k!='native_execution'} for page in pages],
        'dpi':parameters['dpi'],'source_files':receipt['sources'],'merged_pdf_sha256':merged['sha256'],
        'scope':'Ordered native-coordinate view windows, portable saved MPJ and editable dependencies. Views are in this manifest, not a native multi-page layout object.'}
    target=files/'series.json';body=json_text(manifest)
    if target.exists() and target.read_text(encoding='utf8')!=body:raise BridgeError('MAP_SERIES_MANIFEST_CHANGED')
    if not target.exists():target.write_text(body,encoding='utf8')
    index=files/'pages.csv';stream=io.StringIO(newline='');writer=csv.writer(stream)
    rows=[['page','name','xmin','ymin','xmax','ymax','width_mm','height_mm','eps','preview','tiff']]
    for page in pages:rows.append([page['number'],page['name'],*page['view']['native_bounds'],*page['paper_mm'],page['eps'],page['preview'],page['tiff'] or ''])
    writer.writerows(rows)
    encoded=b'\xef\xbb\xbf'+stream.getvalue().encode('utf8')
    if index.exists() and index.read_bytes()!=encoded:raise BridgeError('MAP_SERIES_INDEX_CHANGED')
    if not index.exists():index.write_bytes(encoded)
    write_literal_workbook(files/'pages.xlsx',{'图幅':rows})
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in receipt['sources']):raise BridgeError('STALE_SOURCE')
    return finish_bundle(bridge,request_id,files,{'backend':'Original MapGIS project PS/EPS and existing Ghostscript multi-page PDF',
        'page_count':len(pages),'pages':pages,'native_project_reopen':readback,'source_preserved':True,
        'replayed':False,'reconciled_after_interruption':recovered,'mouse_or_keyboard_input':False,
        'scope':manifest['scope']})

def prepare_map_series(bridge,file,expected_sha256,parameters,request_id):
    parameters=_parameters(parameters);source,info,library,sources=_plot_inputs(bridge.root,file,expected_sha256)
    reserved={'series.pdf','series.json','pages.csv','pages.xlsx'}|{f'page-{i:03d}.'+extension for i in range(1,len(parameters['pages'])+1) for extension in ['eps','png','tif']}
    names=[Path(s['file']).name.casefold() for s in sources]
    if len(set(names))!=len(names) or set(names)&reserved:raise BridgeError('MAP_SERIES_NAME_COLLISION')
    context=json_text({'info':{k:v for k,v in info.items() if k!='native_execution'},'library':library})
    payload={'operation':'cartography.map_series','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,
        'context_sha256':sha(context.encode('utf8')),'series_protocol':2}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,'series.pdf')
    if not new:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        (stage/'series-context.json').write_text(context,encoding='utf8')
        return _advance(bridge,request_id,stage,json.loads(context),parameters)
    except Exception as error:fail_bundle(bridge,request_id,error);raise

def reconcile_map_series(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if receipt['state']=='RUNNING':
        owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(owner['pid'])
            if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
        except psutil.NoSuchProcess:pass
    context=stage/'series-context.json'
    if file_sha(context)!=receipt['payload']['context_sha256']:raise BridgeError('MAP_SERIES_RECOVERY_CONTEXT_CHANGED')
    return _advance(bridge,request_id,stage,json.loads(context.read_text(encoding='utf8')),receipt['payload']['parameters'],True)
