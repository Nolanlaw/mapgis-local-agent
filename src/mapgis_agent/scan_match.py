"""OpenCV image correspondences in fingerprinted scan windows.

This transfers an explicit reference image frame. It supplies no datum or ground
accuracy, and its image-derived checkpoints remain conditional on the match.
"""
from __future__ import annotations

import json
import math

import cv2
import numpy as np

from .core import BridgeError,json_text,sha
from .raster import RasterWorkspace,affine_point,file_sha


def match_scan_windows(raster:RasterWorkspace,source:dict,reference:dict,parameters:dict,request_id:str):
    bridge=raster.bridge
    if set(source)!={'scan_id','window_id'} or set(reference)!={'scan_id','window_id','georeference_id'}:
        raise BridgeError('MATCH_WINDOWS')
    if set(parameters)!={'ratio_threshold','ransac_threshold_px','min_inliers'}:raise BridgeError('UNEXPECTED_PARAMETERS')
    ratio=parameters['ratio_threshold'];threshold=parameters['ransac_threshold_px'];minimum=parameters['min_inliers']
    if (type(ratio) not in (int,float) or not math.isfinite(ratio) or not .1<=ratio<1
            or type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0<threshold<=20
            or type(minimum) is not int or not 6<=minimum<=10000):raise BridgeError('MATCH_PARAMETERS')
    bridge.request_id(request_id);bridge.request_id(reference['georeference_id'])
    a,wa,ma=raster._load_window(source['scan_id'],source['window_id'])
    b,wb,mb=raster._load_window(reference['scan_id'],reference['window_id'])
    path=bridge.destination_path(raster.root/'work/georeference'/(reference['georeference_id']+'.json'))
    georef=json.loads(path.read_text(encoding='utf8')) if path.is_file() else None
    if (not georef or not georef.get('accepted') or georef['scan_id']!=reference['scan_id']
            or georef['source_sha256']!=b['source_sha256']):raise BridgeError('GEOREFERENCE_NOT_ACCEPTED')
    payload={'source':source,'reference':reference,'parameters':parameters,'source_manifest_sha256':file_sha(ma),
             'reference_manifest_sha256':file_sha(mb),'reference_georeference_sha256':file_sha(path)}
    fingerprint=sha(json_text(payload).encode())
    directory=bridge.destination_path(raster.root/'work/scan-matches'/request_id)
    output=directory/'matches.json';receipt=directory/'receipt.json'
    if receipt.is_file():
        old=json.loads(receipt.read_text(encoding='utf8'))
        if old['payload_hash']!=fingerprint:raise BridgeError('REQUEST_ID_CONFLICT')
        if file_sha(output)!=old['result_sha256']:raise BridgeError('MATCH_OUTPUT_CHANGED')
        return json.loads(output.read_text(encoding='utf8'))
    def image(window):
        image_path=bridge.owned_path(window['output'])
        im=cv2.imdecode(np.frombuffer(image_path.read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE)
        if im is None or max(im.shape)>4096:raise BridgeError('BOUNDED_SCAN_WINDOW_REQUIRED')
        return im
    ia,ib=image(wa),image(wb)
    sift=cv2.SIFT_create(nfeatures=10000)
    ka,da=sift.detectAndCompute(ia,None);kb,db=sift.detectAndCompute(ib,None)
    if da is None or db is None or min(len(ka),len(kb))<minimum:raise BridgeError('MATCH_FEATURES_INSUFFICIENT')
    pairs=cv2.BFMatcher(cv2.NORM_L2).knnMatch(da,db,k=2)
    matches=[p[0] for p in pairs if len(p)==2 and p[0].distance<ratio*p[1].distance]
    if len(matches)<minimum:raise BridgeError('MATCH_CANDIDATES_INSUFFICIENT')
    pa=np.float32([ka[m.queryIdx].pt for m in matches]);pb=np.float32([kb[m.trainIdx].pt for m in matches])
    matrix,inliers=cv2.estimateAffinePartial2D(pa,pb,method=cv2.RANSAC,ransacReprojThreshold=threshold,
                                            maxIters=10000,confidence=.999)
    if matrix is None or inliers is None:raise BridgeError('MATCH_MODEL_NOT_FOUND')
    residual=np.linalg.norm(cv2.transform(pa[:,None,:],matrix).reshape(-1,2)-pb,axis=1)
    chosen=inliers.ravel().astype(bool)&(residual<=threshold)
    # Multiple orientations of one SIFT keypoint must not become duplicate GCPs.
    unique={}
    for p,q,error in zip(pa[chosen],pb[chosen],residual[chosen]):
        key=tuple(float(v) for v in p)
        if key not in unique or error<unique[key][2]:unique[key]=(p,q,float(error))
    points=sorted(unique.values(),key=lambda v:(float(v[0][1]),float(v[0][0])))
    if len(points)<minimum:raise BridgeError('MATCH_INLIERS_INSUFFICIENT')
    hull_fraction=float(cv2.contourArea(cv2.convexHull(np.float32([p for p,_,_ in points])))/(ia.shape[0]*ia.shape[1]))
    if hull_fraction<.1:raise BridgeError('MATCH_POOR_DISTRIBUTION: inlier hull covers less than 10 percent of source window')
    controls=[];checks=[];observations=[]
    for index,(p,q,error) in enumerate(points):
        src=affine_point(wa['plan']['pixel_to_source'],[float(v) for v in p])
        ref=affine_point(wb['plan']['pixel_to_source'],[float(v) for v in q])
        world=affine_point(georef['coefficients'],list(ref))
        item={'pixel':list(src),'world':list(world)}
        (checks if index%3==0 else controls).append(item)
        observations.append({'source_pixel':list(src),'reference_pixel':list(ref),'error_reference_preview_px':error})
    result={'request_id':request_id,'source':source,'reference':reference,'crs':georef['crs'],'gcps':controls,'checkpoints':checks,
            'source_preview_to_reference_preview':matrix.tolist(),'status':'IMAGE_CORRESPONDENCES',
            'metrics':{'matches':len(matches),'inliers':len(points),'source_inlier_hull_fraction':hull_fraction,
                       'rms_reference_preview_px':float(np.sqrt(np.mean([e*e for _,_,e in points]))),
                       'max_reference_preview_px':max(e for _,_,e in points)},'correspondences':observations,
            'backend':'OpenCV SIFT/Lowe ratio/similarity RANSAC',
            'checkpoint_scope':'distinct image keypoints unused by the subsequent affine fit; selected by the same image-match model',
            'geodetic_accuracy':'not established by image matching; inherits declared reference frame',
            'source_sha256':a['source_sha256'],'reference_sha256':b['source_sha256']}
    if file_sha(ma)!=payload['source_manifest_sha256'] or file_sha(mb)!=payload['reference_manifest_sha256'] or file_sha(path)!=payload['reference_georeference_sha256']:
        raise BridgeError('STALE_DURING_SCAN_MATCH')
    directory.mkdir(parents=True)
    with output.open('x',encoding='utf8') as stream:stream.write(json_text(result))
    with receipt.open('x',encoding='utf8') as stream:stream.write(json_text({'payload_hash':fingerprint,'result_sha256':file_sha(output)}))
    return result
