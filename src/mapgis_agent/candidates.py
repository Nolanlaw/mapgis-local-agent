"""Bounded scan candidates: GeoScan centerlines, GDAL pixel polygons and components.

These are reviewable observations, never inferred geological ages or accepted edits.
"""
from __future__ import annotations

import json
import math

import cv2
import numpy as np
from affine import Affine
from rasterio.features import shapes,rasterize
from shapely import coverage_is_valid, coverage_simplify, get_num_coordinates, segmentize, set_precision, unary_union
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, box, mapping, shape

from .core import BridgeError, json_text, sha
from .raster import RasterWorkspace, file_sha, affine_point
from ._vendor.geoscan_trace import zhang_suen_thin, trace_skeleton_paths, simplify_path


def _number(config, name, default, minimum=0):
    value=config.get(name,default)
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<minimum:
        raise BridgeError(f"INVALID_PARAMETER: {name}")
    return value


def _rgb(value):
    if not isinstance(value,list) or len(value)!=3 or any(type(x) is not int or not 0<=x<=255 for x in value):
        raise BridgeError("RGB_REQUIRED: three integers 0..255")
    return np.array(value,np.uint8)


def extract_candidates(raster: RasterWorkspace, scan_id: str, window_id: str,
                       config: dict, request_id: str) -> dict:
    bridge=raster.bridge
    for value in (window_id,request_id):
        bridge.request_id(value)
    scan,window,manifest=raster._load_window(scan_id,window_id)
    image_path=bridge.owned_path(window["output"])
    georef=None
    if georef_id:=config.get("georeference_id"):
        bridge.request_id(georef_id)
        georef_path=bridge.destination_path(bridge.root / "work/georeference" / f"{georef_id}.json")
        if georef_path.is_file():
            georef=json.loads(georef_path.read_text(encoding="utf-8"))
        if not georef or not georef.get("accepted") or georef["source_sha256"]!=scan["source_sha256"]:
            raise BridgeError("GEOREFERENCE_NOT_ACCEPTED: matching scan and independent checks required")
    payload={"scan_id":scan_id,"window_id":window_id,"config":config,"window_sha256":file_sha(manifest),
             "georeference":georef}
    payload_hash=sha(json_text(payload).encode())
    directory=bridge.destination_path(bridge.root / "work/candidates" / request_id)
    receipt=directory / "receipt.json"
    output=directory / "features.geojson"
    if receipt.is_file():
        previous=json.loads(receipt.read_text(encoding="utf-8"))
        if previous["payload_hash"]!=payload_hash:
            raise BridgeError("REQUEST_ID_CONFLICT")
        if not output.is_file() or file_sha(output)!=previous["features_sha256"]:
            raise BridgeError("CANDIDATE_OUTPUT_CONFLICT")
        return previous
    import pyvips
    image=pyvips.Image.new_from_file(str(image_path),access="sequential")
    width,height=window["plan"]["output_size"]
    if (image.width,image.height)!=(width,height) or max(width,height)>4096 or image.bands<3:
        raise BridgeError("BOUNDED_RGB_WINDOW_REQUIRED")
    rgb=np.frombuffer(image.extract_band(0,n=3).cast("uchar").write_to_memory(),np.uint8).reshape(height,width,3)
    mode=config.get("mode")
    role=config.get("role")
    if mode not in {"lines","areas","symbols"}:
        raise BridgeError("CANDIDATE_MODE: lines, areas or symbols")
    if mode!="areas" and role not in {"boundary","fault","contour","annotation","protected","symbol"}:
        raise BridgeError("EXPLICIT_LINE_ROLE_REQUIRED")
    transform=window["plan"]["pixel_to_source"]
    output_features=[]
    qa={"backend":"GeoScan/OpenCV/GDAL","review_status":"UNREVIEWED","mode":mode,
        "source_window":window["plan"]["source_window"],"window_size":[width,height],
        "coordinate_frame":"map" if georef else "source_pixel","automatic_geological_classification":False}

    def feature(geometry,properties):
        a,b,c,d,e,f=transform
        geometry=affine_transform(geometry,[a,b,d,e,c,f])
        if georef:
            a,b,c,d,e,f=georef["coefficients"]
            geometry=affine_transform(geometry,[a,b,d,e,c,f])
        output_features.append({"type":"Feature","id":f"{request_id}-{len(output_features)+1}",
            "geometry":mapping(geometry),"properties":{**properties,"review_status":"UNREVIEWED"}})

    if mode=="areas":
        palette=config.get("palette",[])
        if not 1<=len(palette)<=64 or len({p.get("id") for p in palette})!=len(palette) or any(not isinstance(p.get("id"),str) or not p["id"] for p in palette):
            raise BridgeError("EXPLICIT_PALETTE: 1..64 unique class IDs and RGB colors")
        tolerance=_number(config,"color_tolerance",20)
        labels=np.zeros((height,width),np.int16)
        best=np.full((height,width),np.inf,np.float32)
        for i,entry in enumerate(palette,1):
            delta=np.linalg.norm(rgb.astype(np.float32)-_rgb(entry["rgb"]).astype(np.float32),axis=2)
            chosen=(delta<best)&(delta<=tolerance)
            labels[chosen]=i
            best[chosen]=delta[chosen]
        excluded=np.zeros((height,width),bool)
        exclusion_fill=_number(config,'exclusion_fill_px',0)
        if exclusion_fill>512:raise BridgeError('EXCLUSION_FILL_WINDOW: at most 512 preview pixels')
        regions=config.get('exclude_regions',[])
        if not isinstance(regions,list) or len(regions)>1000:raise BridgeError('EXCLUSION_REGIONS')
        excluded_shapes=[]
        for region in regions:
            if not isinstance(region,dict) or set(region)!={'geometry','reason'} or not isinstance(region['reason'],str) or not region['reason']:
                raise BridgeError('EXCLUSION_REGION: geometry and explicit reason required')
            geometry=shape(region['geometry'])
            if (geometry.geom_type not in {'Polygon','MultiPolygon'} or geometry.is_empty or not geometry.is_valid
                    or not all(math.isfinite(v) for v in geometry.bounds)
                    or not box(0,0,width,height).covers(geometry)):
                raise BridgeError('EXCLUSION_REGION_BOUNDS: valid polygons in preview pixels required')
            excluded_shapes.append((mapping(geometry),1))
        if excluded_shapes:
            excluded=rasterize(excluded_shapes,out_shape=(height,width),transform=Affine.identity(),dtype='uint8').astype(bool)
            labels[excluded]=0
            qa.update(excluded_pixels=int(excluded.sum()),excluded_regions=len(regions),
                      exclusion_fill_px=exclusion_fill,exclusion_reasons=[r['reason'] for r in regions],
                      exclusion_policy='explicit occlusions filled from nearest visible palette; inferred pixels are recorded')
        seed_minimum=_number(config,"min_seed_area_px",0)
        erosion=config.get("seed_erosion_px",0)
        protected=config.get("protected_classes",[])
        if type(erosion) is not int or not 0<=erosion<=16:
            raise BridgeError("seed_erosion_px must be an integer 0..16")
        if (not isinstance(protected,list) or any(not isinstance(v,str) for v in protected)
                or not set(protected).issubset({p["id"] for p in palette})):
            raise BridgeError("protected_classes must name explicit palette classes")
        if seed_minimum or erosion:
            cleaned=np.zeros_like(labels)
            for i,entry in enumerate(palette,1):
                mask=(labels==i).astype(np.uint8)
                if entry["id"] not in protected:
                    if seed_minimum:
                        _,components,stats,_=cv2.connectedComponentsWithStats(mask,connectivity=4)
                        keep=stats[:,cv2.CC_STAT_AREA]>=seed_minimum
                        keep[0]=False
                        mask=keep[components].astype(np.uint8)
                    if erosion:
                        mask=cv2.erode(mask,np.ones((2*erosion+1,2*erosion+1),np.uint8),
                                       borderType=cv2.BORDER_REPLICATE)
                cleaned[mask>0]=i
            qa["seed_cleanup"]={"min_seed_area_px":seed_minimum,"seed_erosion_px":erosion,
                "removed_pixels":int(np.count_nonzero(labels)-np.count_nonzero(cleaned)),
                "protected_classes":sorted(set(protected)),
                "warning":"Optional seed filtering can remove small or narrow bodies; source review required"}
            labels=cleaned
        unknown=labels==0
        qa["unknown_pixels_before_fill"]=int(unknown.sum())
        fill=_number(config,"max_fill_px",0)
        if (fill or exclusion_fill) and np.any(~unknown):
            distance,nearest=cv2.distanceTransformWithLabels(unknown.astype(np.uint8),cv2.DIST_L2,5,labelType=cv2.DIST_LABEL_PIXEL)
            lookup=np.zeros(int(nearest.max())+1,np.int16)
            lookup[nearest[~unknown]]=labels[~unknown]
            allowed=unknown&((~excluded&(distance<=fill))|(excluded&(distance<=exclusion_fill)))
            labels[allowed]=lookup[nearest[allowed]]
        if regions:qa['exclusion_filled_pixels']=int(np.count_nonzero(excluded&(labels>0)))
        qa["unknown_pixels"]=int((labels==0).sum())
        qa["filled_pixels"]=qa["unknown_pixels_before_fill"]-qa["unknown_pixels"]
        qa["pixel_geometry"]="GDAL cell edges, 4-connectivity; unclassified pixels remain holes"
        minimum=_number(config,"min_area_px",9)
        polygons,classes=[],[]
        for geometry,index in shapes(labels,mask=labels>0,connectivity=4,transform=Affine.identity()):
            polygon=shape(geometry)
            if polygon.area>=minimum:
                polygons.append(polygon)
                classes.append(palette[int(index)-1]["id"])
        tolerance=_number(config,"coverage_simplify_px",0)
        if tolerance and polygons:
            pixel_grid_noded=False
            # GDAL omits collinear pixel vertices, leaving T-junctions absent
            # from the long neighbouring edge. Unit-grid densification is
            # exact for these integer, axis-aligned cell boundaries.
            if not coverage_is_valid(polygons):
                # segmentize can introduce fractional roundoff even on an
                # axis-aligned integer edge. Restore the original pixel grid.
                polygons=set_precision(segmentize(polygons,1),1)
                pixel_grid_noded=True
            if not coverage_is_valid(polygons):
                raise BridgeError("INVALID_PIXEL_COVERAGE: simplify requires matched edges and no overlaps")
            simplified=coverage_simplify(polygons,tolerance=tolerance)
            if not coverage_is_valid(simplified) or any(g.is_empty or not g.is_valid for g in simplified):
                raise BridgeError("SIMPLIFICATION_INVALID_COVERAGE")
            qa["coverage_simplification"]={"backend":"GEOS coverage_simplify","tolerance_px":tolerance,
                "vertices_before":int(sum(get_num_coordinates(polygons))),
                "vertices_after":int(sum(get_num_coordinates(simplified))),
                "max_hausdorff_px":max(a.hausdorff_distance(b) for a,b in zip(polygons,simplified)),
                "edge_matched":True,"pixel_grid_noded":pixel_grid_noded}
            polygons=simplified
        for polygon,class_id in zip(polygons,classes):
            feature(polygon,{"role":"area","class_id":class_id})
    else:
        if "rgb_min" in config or "rgb_max" in config:
            low,high=_rgb(config.get("rgb_min")),_rgb(config.get("rgb_max"))
            if np.any(low>high):
                raise BridgeError("RGB_RANGE_REVERSED")
            mask=cv2.inRange(rgb,low,high)>0
        else:
            threshold=_number(config,"gray_threshold",90)
            if threshold>255:
                raise BridgeError("gray_threshold must be 0..255")
            mask=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)<=threshold
            qa["warning"]="Dark ink may include text and filled regions; explicit review required"
        qa["mask_pixels"]=int(mask.sum())
        if mode=="lines":
            minimum=_number(config,"min_length_px",60)
            tolerance=_number(config,"simplify_px",1)
            skeleton=zhang_suen_thin(mask,max_iterations=64)
            paths=trace_skeleton_paths(skeleton)
            qa.update({"raw_paths":len(paths),"discarded_short_paths":0,"gap_closing_applied":False,
                       "trace_endpoint_policy":"REVIEW_THINNING_RETRACTION","automatic_endpoint_extension":False})
            for path in paths:
                if len(path)<2 or LineString(path).length<minimum:
                    qa["discarded_short_paths"]+=1
                    continue
                simplified=simplify_path(path,tolerance=tolerance,closed=path[0]==path[-1])
                feature(LineString([(x+0.5,y+0.5) for x,y in simplified]),{"role":role})
        else:
            minimum=_number(config,"min_area_px",9)
            maximum=_number(config,"max_area_px",width*height,minimum)
            count,_,stats,centers=cv2.connectedComponentsWithStats(mask.astype(np.uint8),connectivity=8)
            for stat,center in zip(stats[1:],centers[1:]):
                if minimum<=stat[4]<=maximum:
                    feature(Point(center[0]+0.5,center[1]+0.5),{"role":role,"pixel_area":int(stat[4])})
            qa["component_count"]=count-1
    qa["candidate_count"]=len(output_features)
    if not output_features:
        raise BridgeError("NO_CANDIDATES: inspect mask, resolution and explicit thresholds")
    result={"request_id":request_id,"payload_hash":payload_hash,"features":str(output),
        "scan_id":scan_id,"window_id":window_id,"config":config,
        "crs":georef["crs"] if georef else None,"source_sha256":scan["source_sha256"],"qa":qa}
    directory.mkdir(parents=True,exist_ok=False)
    collection={"type":"FeatureCollection","features":output_features}
    with output.open("x",encoding="utf-8") as stream:
        stream.write(json_text(collection))
    result["features_sha256"]=file_sha(output)
    with receipt.open("x",encoding="utf-8") as stream:
        stream.write(json_text(result))
    return result


def merge_candidate_tiles(raster: RasterWorkspace, scan_id: str, tiles: list[dict], request_id: str,
                          georeference_id: str | None = None, simplify_px: float = 0) -> dict:
    """Merge independently extracted area tiles in source pixels, then georeference.

    Cores explicitly own disjoint source rectangles; overlap is a QA comparison,
    never duplicate production geometry. Geological interpretation stays separate.
    """
    bridge=raster.bridge
    bridge.request_id(request_id)
    scan=raster._load(scan_id)
    if not isinstance(tiles,list) or not 2<=len(tiles)<=256:
        raise BridgeError("TILES_REQUIRED: 2..256 independently extracted windows")
    if isinstance(simplify_px,bool) or not isinstance(simplify_px,(int,float)) or not math.isfinite(simplify_px) or simplify_px<0:
        raise BridgeError("INVALID_SIMPLIFICATION")
    palette=None
    sources=[]
    for tile in tiles:
        tid=bridge.request_id(tile["request_id"])
        receipt=bridge.owned_path(bridge.root/'work/candidates'/tid/'receipt.json')
        candidate=json.loads(receipt.read_text(encoding='utf8'))
        output=bridge.owned_path(candidate['features'])
        if file_sha(output)!=candidate['features_sha256']:
            raise BridgeError("CANDIDATE_OUTPUT_CONFLICT")
        if (candidate.get('scan_id')!=scan_id or candidate['source_sha256']!=scan['source_sha256']
                or candidate['crs'] is not None or candidate['qa']['mode']!='areas'):
            raise BridgeError("SOURCE_PIXEL_AREA_TILES_REQUIRED: merge before georeferencing")
        _,source_window,_=raster._load_window(scan_id,candidate['window_id'])
        pixel_transform=source_window['plan']['pixel_to_source']
        if pixel_transform[:2]!=[1,0] or pixel_transform[3:5]!=[0,1]:
            raise BridgeError("FULL_RESOLUTION_TILE_REQUIRED: integer source pixel grid")
        colors=candidate['config']['palette']
        if palette is None:
            palette=colors
        elif json_text(palette)!=json_text(colors):
            raise BridgeError("TILE_PALETTE_MISMATCH")
        core=tile.get('core')
        if not isinstance(core,list) or len(core)!=4 or any(type(n) is not int for n in core) or min(core[:2])<0 or min(core[2:])<=0:
            raise BridgeError("CORE_RECTANGLE_REQUIRED: source pixel [left,top,width,height]")
        core_box=box(core[0],core[1],core[0]+core[2],core[1]+core[3])
        window=candidate['qa']['source_window']
        window_box=box(window[0],window[1],window[0]+window[2],window[1]+window[3])
        if not window_box.covers(core_box):
            raise BridgeError("CORE_OUTSIDE_CANDIDATE_WINDOW")
        groups={}
        for feature in json.loads(output.read_text(encoding='utf8'))['features']:
            geometry=shape(feature['geometry'])
            if not geometry.is_valid or geometry.geom_type not in {'Polygon','MultiPolygon'}:
                raise BridgeError("INVALID_AREA_TILE")
            groups.setdefault(feature['properties']['class_id'],[]).append(geometry)
        sources.append({'id':tid,'receipt_sha256':file_sha(receipt),'output_sha256':candidate['features_sha256'],
                        'core':core_box,'window':window_box,'groups':{k:unary_union(v) for k,v in groups.items()}})
    disagreement=0.0
    seam_disagreement=0.0
    for i,left in enumerate(sources):
        for right in sources[i+1:]:
            if left['core'].intersection(right['core']).area>0:
                raise BridgeError("CORE_OVERLAP: production ownership must be disjoint")
            overlap=left['window'].intersection(right['window'])
            seam=left['core'].boundary.intersection(right['core'].boundary)
            seam_band=seam.buffer(.5,cap_style='flat').intersection(overlap) if seam.length else None
            for key in set(left['groups'])|set(right['groups']):
                a=left['groups'].get(key,box(0,0,0,0)).intersection(overlap)
                b=right['groups'].get(key,box(0,0,0,0)).intersection(overlap)
                difference=a.symmetric_difference(b)
                disagreement+=difference.area
                if seam_band is not None:
                    seam_disagreement+=difference.intersection(seam_band).area
    cores=unary_union([s['core'] for s in sources])
    if cores.area!=cores.envelope.area:
        raise BridgeError("CORE_GAP: cores must cover one complete rectangle")
    merged={}
    for source in sources:
        for key,geometry in source['groups'].items():
            clipped=geometry.intersection(source['core'])
            if not clipped.is_empty:
                merged.setdefault(key,[]).append(clipped)
    geometries,classes=[],[]
    for key,parts in sorted(merged.items()):
        geometry=unary_union(parts)
        for part in ([geometry] if geometry.geom_type=='Polygon' else geometry.geoms):
            if part.geom_type=='Polygon' and part.area>0:
                geometries.append(part)
                classes.append(key)
    if not geometries:
        raise BridgeError("NO_CANDIDATES")
    if not coverage_is_valid(geometries):
        geometries=set_precision(segmentize(geometries,1),1)
    if not coverage_is_valid(geometries):
        raise BridgeError("INVALID_MERGED_COVERAGE")
    before=geometries
    if simplify_px:
        geometries=coverage_simplify(geometries,simplify_px)
    if not coverage_is_valid(geometries) or any(g.is_empty or not g.is_valid for g in geometries):
        raise BridgeError("INVALID_MERGED_COVERAGE")
    georef=None
    if georeference_id:
        bridge.request_id(georeference_id)
        georef=json.loads(bridge.owned_path(bridge.root/'work/georeference'/f'{georeference_id}.json').read_text(encoding='utf8'))
        if not georef['accepted'] or georef['source_sha256']!=scan['source_sha256']:
            raise BridgeError("GEOREFERENCE_NOT_ACCEPTED")
    inputs=[{'request_id':s['id'],'receipt_sha256':s['receipt_sha256'],'features_sha256':s['output_sha256'],
             'core':list(s['core'].bounds)} for s in sources]
    payload_hash=sha(json_text({'inputs':inputs,'georeference':georef,'simplify_px':simplify_px}).encode())
    directory=bridge.destination_path(bridge.root/'work/candidates'/request_id)
    receipt=directory/'receipt.json'
    output=directory/'features.geojson'
    if receipt.exists():
        previous=json.loads(receipt.read_text(encoding='utf8'))
        if previous['payload_hash']!=payload_hash or not output.is_file() or file_sha(output)!=previous['features_sha256']:
            raise BridgeError("CANDIDATE_OUTPUT_CONFLICT")
        return previous
    features=[]
    for i,(geometry,key) in enumerate(zip(geometries,classes),1):
        if georef:
            a,b,c,d,e,f=georef['coefficients']
            geometry=affine_transform(geometry,[a,b,d,e,c,f])
        features.append({'type':'Feature','id':f'{request_id}-{i}','geometry':mapping(geometry),
                         'properties':{'role':'area','class_id':key,'review_status':'UNREVIEWED'}})
    result={'request_id':request_id,'payload_hash':payload_hash,'scan_id':scan_id,'inputs':inputs,
            'source_sha256':scan['source_sha256'],'features':str(output),'crs':georef['crs'] if georef else None,
            'palette':palette,'qa':{'backend':'GEOS tile core clipping, class union and coverage validation',
                'review_status':'UNREVIEWED','tile_count':len(tiles),'edge_matched':True,
                'overlap_disagreement_px2':disagreement,'overlap_metric':'sum of per-class symmetric differences; class swaps count twice',
                'core_seam_disagreement_px2':seam_disagreement,'core_seam_band_width_px':1,
                'core_bounds_source_pixel':list(cores.bounds),'simplify_px':simplify_px,
                'max_hausdorff_px':max(a.hausdorff_distance(b) for a,b in zip(before,geometries)),
                'candidate_count':len(features),'coordinate_frame':'map' if georef else 'source_pixel'}}
    directory.mkdir(parents=True,exist_ok=False)
    with output.open('x',encoding='utf8') as stream:
        stream.write(json_text({'type':'FeatureCollection','features':features}))
    result['features_sha256']=file_sha(output)
    with receipt.open('x',encoding='utf8') as stream:
        stream.write(json_text(result))
    return result
