"""Calibrate an installed MapGIS color table through its native RGB TIFF output.

Prepare creates only new vectors. The agent must create/bind a native project,
set the stated page and export TIFF with Sky before read can sample its pixels.
No proprietary color or symbol library is bundled, changed or decoded here.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from mapgis_agent.core import Bridge
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha


def prepare(root: Path, library: Path, request_id: str, count: int = 1024) -> dict:
    if type(count) is not int or not 1<=count<=1024:
        raise ValueError('count must be 1..1024')
    fingerprint=file_sha(library)
    features={'type':'FeatureCollection','features':[]}
    from shapely.geometry import box,mapping
    for number in range(count):
        x,y=(number%32)*4,(number//32)*4
        features['features'].append({'type':'Feature','id':f'color-{number}',
            'geometry':mapping(box(x,y,x+3.5,y+3.5)),
            'properties':{'ID':number+1,'色号':number},'style':{'fill_color':number}})
    bridge=Bridge(root)
    prepare_creation(bridge,features,{'name':'palette.wp','map_scale':1,
        'crs':{'kind':'local','name':'Procedural native palette swatches','unit':'millimetre',
               'axis_order':'xy','reference_sha256':fingerprint},
        'provenance':{'library_sha256':fingerprint,'procedural_geometry':True}},request_id)
    result=bridge.apply(request_id)
    return {'file':result['output'],'sha256':file_sha(Path(result['output'])),'count':count,
            'library_sha256':fingerprint,'state':'REQUIRES_NATIVE_PROJECT_AND_TIFF',
            'page':{'width_mm':150,'height_mm':150,'dx':10,'dy':10,'sx':1,'sy':1,'rotation':0},
            'export':{'dpi':300,'mode':'RGB'},'preview_request':request_id}


def read(tiff: Path, library: Path, count: int = 1024, dpi: int = 300) -> dict:
    import numpy as np
    import rasterio
    if type(count) is not int or not 1<=count<=1024 or type(dpi) is not int or not 72<=dpi<=600:
        raise ValueError('Invalid count or DPI')
    with rasterio.open(tiff) as source:
        expected=int(150*dpi/25.4)
        if source.count!=3 or max(source.width,source.height)>4096 or max(abs(source.width-expected),abs(source.height-expected))>1:
            raise ValueError('Expected a 150 x 150 mm native RGB TIFF at the stated DPI')
        data=source.read()
        colors=[]
        for number in range(count):
            x=int((10+(number%32)*4+1.75)*source.width/150)
            y=source.height-1-int((10+(number//32)*4+1.75)*source.height/150)
            patch=data[:,y-2:y+3,x-2:x+3]
            if patch.shape!=(3,5,5) or np.any(np.ptp(patch,axis=(1,2))>2):
                raise ValueError(f'Nonuniform or misplaced swatch: {number}')
            colors.append({'number':number,'rgb':np.median(patch,axis=(1,2)).astype(int).tolist()})
    return {'state':'EXTRACTED_REQUIRES_NATIVE_BINDING','library_sha256':file_sha(library),
            'native_tiff_sha256':file_sha(tiff),'dpi':dpi,'count':count,'colors':colors,
            'scope':'Native RGB output samples. Validate project/dependencies/page with NativeSession and independent UI anchors before use.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','read'])
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--library',type=Path,required=True)
    parser.add_argument('--request-id',default='native-palette')
    parser.add_argument('--count',type=int,default=1024)
    parser.add_argument('--tiff',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='prepare':
        result=prepare(args.root,args.library,args.request_id,args.count)
    else:
        if args.tiff is None:parser.error('read requires --tiff')
        result=read(args.tiff,args.library,args.count)
    if args.output:
        with args.output.open('x',encoding='utf8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
        print(json.dumps({'output':str(args.output),'state':result['state'],'count':result['count']},ensure_ascii=False))
    else:
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
