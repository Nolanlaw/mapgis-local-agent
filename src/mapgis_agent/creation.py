"""Create new native vectors with mapgis2shp; never rebuild an existing file."""
from __future__ import annotations

import json
import math
from numbers import Integral, Real
from pathlib import Path
import struct
import tempfile

import geopandas as gpd
from pymapgis import Reader, Writer
from shapely.geometry import shape, LinearRing

from .core import Bridge, BridgeError, json_text, sha, read_native, annotation_text
from .raster import explicit_crs, crs_metadata


PARAMS = {
    "POINT":{"point_type","symbol_no","height","width","spacing","angle","color","layer","font_zh","font_en","font_style","pen_width","radius_mm","border_color","filled","start_angle","end_angle"},
    "LINE":{"linetype","aux_linetype","aux_flag","color","width","x_coef","y_coef","aux_color","layer","transparent"},
    "POLYGON":{"fill_color","pattern","pattern_height","pattern_width","pattern_color","layer"},
}
TYPES = {".wt":"POINT", ".wl":"LINE", ".wp":"POLYGON"}
REGION_INDEX_FIELDS = {"class","field5","field6","field7"}


def _replace_sections(blob: bytes, sections: list, replacements: dict) -> bytes:
    chunks={i:blob[start:start+size] for i,(start,size) in enumerate(sections) if size}
    chunks.update(replacements)
    table=struct.unpack_from("<i",blob,12)[0]
    header=bytearray(blob[:table+16*10])
    body=bytearray()
    for i in range(16):
        chunk=chunks.get(i)
        struct.pack_into("<2i",header,table+i*10,len(header)+len(body) if chunk else 0,len(chunk) if chunk else 0)
        if chunk:
            body.extend(chunk)
    return bytes(header+body)


def _new_region_arc_index(blob: bytes, sections: list, geometries: list, factor: float) -> bytes:
    """Complete the native renderer's region walks in newly encoded WP only.

    The pinned upstream writer supplies shared arcs and left/right ownership,
    but omits the per-region walk. Native 6.7 stores two i32 values @1/@5 in
    each 40-byte region record: list length and offset in coordinate section.
    The list is [sum(arc vertex counts)+ring count, signed arcs, 0, next ring].
    Every ring is walked clockwise. Original native files and an independent
    native topology rebuild anchor this layout; existing files are never rebuilt.
    """
    coords=bytearray(blob[sections[1][0]:sum(sections[1])])
    params=bytearray(blob[sections[8][0]:sum(sections[8])])
    edges,arc_sizes={},{}
    for aid in range(1,sections[0][1]//57):
        count,offset=struct.unpack_from("<2i",blob,sections[0][0]+aid*57+10)
        points=[struct.unpack_from("<2d",coords,offset+i*16) for i in range(count)]
        arc_sizes[aid]=count
        owners=set(struct.unpack_from('<2i',blob,sections[3][0]+aid*24+8))-{0}
        for a,b in zip(points,points[1:]):
            if a==b:
                continue
            for owner in owners:
                if (owner,a,b) in edges or (owner,b,a) in edges:
                    raise BridgeError("NATIVE_REGION_INDEX: ambiguous duplicate arc segment within one region")
                edges[owner,a,b]=aid
                edges[owner,b,a]=-aid
    for pid,geometry in enumerate(geometries,1):
        parts=list(geometry.geoms) if geometry.geom_type=="MultiPolygon" else [geometry]
        rings=[]
        for polygon in parts:
            for ring in (polygon.exterior,*polygon.interiors):
                points=[(float(x)*factor,float(y)*factor) for x,y in ring.coords]
                if LinearRing(points).is_ccw:
                    points.reverse()
                walk=[]
                for a,b in zip(points,points[1:]):
                    if a==b:
                        continue
                    signed=edges.get((pid,a,b))
                    if signed is None:
                        raise BridgeError("NATIVE_REGION_INDEX: source ring does not match encoded arcs")
                    if not walk or walk[-1]!=signed:
                        walk.append(signed)
                if len(walk)>1 and walk[0]==walk[-1]:
                    walk.pop()
                if not walk:
                    raise BridgeError("NATIVE_REGION_INDEX: empty ring")
                rings.append(walk)
        values=[sum(arc_sizes[abs(a)] for ring in rings for a in ring)+len(rings)]
        for i,ring in enumerate(rings):
            if i:
                values.append(0)
            values.extend(ring)
        struct.pack_into("<2i",params,pid*40+1,len(values),len(coords))
        coords.extend(struct.pack(f"<{len(values)}i",*values))
    return _replace_sections(blob,sections,{1:coords,8:params})


def _new_annotation_pool(blob: bytes, sections: list, texts: list[str | None]) -> bytes:
    """Supplement the upstream writer's empty text pool, only for new files.

    Native WT records use u16 byte length @1, i32 pool offset @3 and GBK text.
    The observed native pool starts with two null bytes; text has no terminator.
    """
    chunks={i:bytearray(blob[start:start+size]) for i,(start,size) in enumerate(sections) if size}
    pool=bytearray(b"\0\0")
    for index,text in enumerate(texts,1):
        if text is None:
            continue
        try:
            encoded=text.encode("gbk",errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError("NATIVE_TEXT_GBK_REQUIRED") from exc
        if not encoded or b"\0" in encoded or len(encoded)>65535:
            raise BridgeError("NATIVE_TEXT: non-empty GBK text, no nulls, at most 65535 bytes")
        struct.pack_into("<Hi",chunks[0],index*93+1,len(encoded),len(pool))
        pool.extend(encoded)
    chunks[1]=pool
    return _replace_sections(blob,sections,chunks)


def _new_point_boxes(blob: bytes, sections: list, texts: list, crs, scale: float) -> bytes:
    """Supply conservative boxes in the native storage coordinate frame.

    Upstream uses a fixed degrees/mm factor and a zero file box. Native 6.7
    culls local drawing symbols with those boxes even when parameters are valid.
    Only newly generated WT is changed; MapGIS may refine font/symbol bounds.
    """
    boxes=[]
    for index,text in enumerate(texts,1):
        pos=sections[0][0]+index*93
        x,y=struct.unpack_from('<2d',blob,pos+7)
        height,width=struct.unpack_from('<2f',blob,pos+(33 if text is not None else 37))
        radius=math.hypot(height,width*max(1,len(text or '')))
        if blob[pos+31] in (2,3):radius=struct.unpack_from('<d',blob,pos+33)[0]
        rx=ry=radius
        if crs is not None and crs.is_geographic:
            # Symbol dimensions are drawing millimetres. Geographic coordinates
            # stay in degrees; use the declared ellipsoid rather than a fixed
            # factor measured on a different sheet. Deliberately overbound.
            geod=crs.get_geod()
            distance=radius*scale/1000*1.1
            east,_,_=geod.fwd(x,y,90,distance)
            west,_,_=geod.fwd(x,y,-90,distance)
            _,north,_=geod.fwd(x,y,0,distance)
            _,south,_=geod.fwd(x,y,180,distance)
            rx=max(abs(east-x),abs(west-x))
            ry=max(abs(north-y),abs(south-y))
            if abs(y)+ry>=89 or rx>180:
                rx=360
        boxes.append((x-rx,y-ry,x+rx,y+ry))
    whole=(min(b[0] for b in boxes),min(b[1] for b in boxes),
           max(b[2] for b in boxes),max(b[3] for b in boxes))
    result=bytearray(_replace_sections(blob,sections,{3:b''.join(struct.pack('<4d',*b) for b in [whole,*boxes])}))
    # The native SaveAs result mirrors the union in the common file header.
    # Anchor-only extents clip symbols at the extrema in the file renderer.
    struct.pack_into('<4d',result,304,*whole)
    return bytes(result)


def prepare_creation(bridge: Bridge, features: dict, template: dict, request_id: str,
                     sources: list[dict] | None = None) -> dict:
    request_id = bridge.request_id(request_id)
    name = template.get("name", "")
    if not name or Path(name).name != name or any(c in name for c in '/\\:<>"|?*') or name.rstrip(". ") != name:
        raise BridgeError("INVALID_NAME: provide a filename, not a path")
    kind = TYPES.get(Path(name).suffix.lower())
    if kind is None:
        raise BridgeError("INVALID_EXTENSION: WT/WL/WP required")
    specification = template.get('crs', {})
    mode = specification.get('crs_delivery_mode')
    if mode not in (None, 'dual_sidecar'):
        raise BridgeError('CRS_DELIVERY_MODE: expected dual_sidecar')
    modern = mode == 'dual_sidecar'
    output = bridge.destination_path(bridge.root / "deliverables/creation" / request_id / name)
    payload = {"format_version":5,"kind":"CREATE", "features":features,"template":template}
    if modern:
        payload['operation'] = 'crs.modern_create'
    if sources is not None:
        sources=[{'file':str(Path(item['file']).resolve(strict=True)),'sha256':item['sha256']} for item in sources]
        if not sources or any(sha(Path(item['file']).read_bytes())!=item['sha256'] for item in sources):
            raise BridgeError('STALE_SOURCE: native operation input changed')
        payload['sources']=sources
    encoded = json_text(payload).encode("utf-8")
    payload_hash = sha(encoded)
    with bridge.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise BridgeError("REQUEST_ID_CONFLICT")
            return bridge._receipt(existing)
    declared = explicit_crs(specification)
    if modern and (declared is None or not (declared.is_geographic or declared.is_projected)
                   or len(declared.axis_info) != 2):
        raise BridgeError('CRS_SIDECAR_2D_EARTH_CRS_REQUIRED')
    crs = None if modern else declared
    projection = crs.to_dict() if crs else {}
    unsupported_tm = projection.get("proj") == "tmerc" and any((
        projection.get("k",projection.get("k_0",1)) != 1,
        projection.get("lat_0",0) != 0,projection.get("x_0",500000) != 500000,projection.get("y_0",0) != 0))
    if crs and crs.is_projected and (projection.get("proj") not in {"tmerc","aea","lcc"} or unsupported_tm):
        raise BridgeError("UNREPRESENTABLE_NATIVE_CRS: this file writer cannot preserve the specified projection; use a native projection workflow or an explicitly transformed branch")
    scale = template.get("map_scale")
    if isinstance(scale,bool) or not isinstance(scale,(int,float)) or not math.isfinite(scale) or scale <= 0:
        raise BridgeError("EXPLICIT_SCALE: positive map_scale denominator required")
    if features.get("type") != "FeatureCollection" or not features.get("features"):
        raise BridgeError("A non-empty GeoJSON FeatureCollection is required")
    rows, geometries, mapping, identities, texts, styles, attributes = [], [], [], set(), [], [], []
    for index, feature in enumerate(features["features"],1):
        identity = feature.get("id")
        if not isinstance(identity,str) or not identity or identity in identities:
            raise BridgeError("FEATURE_ID: every feature needs a unique stable string ID")
        identities.add(identity)
        geometry = shape(feature["geometry"])
        if geometry.has_z or getattr(geometry,"has_m",False):
            raise BridgeError("NATIVE_2D_REQUIRED: explicit projection of Z/M before creating 6.x vectors")
        expected = {"POINT":{"Point"},"LINE":{"LineString"},"POLYGON":{"Polygon","MultiPolygon"}}[kind]
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in expected:
            raise BridgeError(f"INVALID_GEOMETRY: {identity}; explicit repair/explode before preparation")
        if not all(math.isfinite(n) for n in geometry.bounds):
            raise BridgeError("INVALID_GEOMETRY: non-finite coordinates")
        properties = feature.get("properties",{}).copy()
        if any(key in PARAMS[kind] or key == "geometry" or (kind=="POLYGON" and key in REGION_INDEX_FIELDS)
               or (kind=="POINT" and key=="pen_width") for key in properties):
            raise BridgeError("Attribute name conflicts with a native graphic parameter; put it in style")
        for key in properties:
            if not isinstance(key,str) or len(key.encode("gbk",errors="strict")) > 19:
                raise BridgeError("FIELD_NAME: must fit the native GBK descriptor without truncation")
        style = {**template.get("style",{}),**feature.get("style",{})}
        if isinstance(style.get('filled'),bool):style['filled']=int(style['filled'])
        if set(style)-PARAMS[kind]:
            raise BridgeError("UNKNOWN_STYLE_FIELD: " + ",".join(sorted(set(style)-PARAMS[kind])))
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in style.values()):
            raise BridgeError("STYLE_VALUE: finite native numeric parameters required")
        text=feature.get("text")
        if text is not None and (kind!="POINT" or not isinstance(text,str)):
            raise BridgeError("NATIVE_TEXT: text belongs to a point feature")
        if kind == "POINT":
            style.setdefault("point_type",0 if text is not None else 1)
            if style["point_type"] not in (0,1,2,3) or (text is not None)!=(style["point_type"]==0):
                raise BridgeError("POINT_KIND: text requires0, symbol1, circle2 or arc3")
            from .point_primitives import validate_style
            validate_style(style,crs)
        if style and kind == "LINE":
            style.setdefault("linetype",1)
        from .point_primitives import FIELDS
        encoded_style={k:v for k,v in style.items() if k not in FIELDS}
        if kind=='POINT' and style['point_type'] in (2,3):encoded_style={'point_type':1}
        rows.append({**properties,**encoded_style})
        styles.append(style)
        attributes.append(properties)
        texts.append(text)
        geometries.append(geometry)
        mapping.append({"feature_id":identity,"physical_record_id":index,"attribute_ID":properties.get("ID",index)})
    table = gpd.GeoDataFrame(rows,geometry=geometries,crs=crs)
    work = bridge.destination_path(bridge.root / "work/creation" / request_id)
    work.mkdir(parents=True,exist_ok=True)
    manifest = work / "input.json"
    if manifest.exists() and manifest.read_bytes() != encoded:
        raise BridgeError("INPUT_CONFLICT: previous generation intent differs")
    if not manifest.exists():
        with manifest.open("xb") as stream:
            stream.write(encoded)
    with tempfile.TemporaryDirectory(prefix="encode-",dir=work) as temporary:
        path = Path(temporary) / name
        writer=Writer(table,make_valid=False,map_scale=scale,snap_tolerance=0)
        writer.to_file(path)
        if kind=="POLYGON":
            raw,_,sections=read_native(path)
            path.write_bytes(_new_region_arc_index(raw,sections,geometries,writer._scale_factor()))
        if crs is None:
            # Upstream stamps geographic/degree at 91/92 even without a CRS.
            # Native 6.7 user drawing files use 0/0 and scale 0. Ordinates stay
            # unscaled; physical ground units and datum remain in the receipt.
            raw=bytearray(path.read_bytes())
            raw[91:93]=bytes([0,0])
            if modern:
                raw[109:111] = b'\0\0'
            struct.pack_into('<d',raw,143,0.0)
            path.write_bytes(raw)
        if any(text is not None for text in texts):
            raw,_,sections=read_native(path)
            path.write_bytes(_new_annotation_pool(raw,sections,texts))
        if kind=="POINT":
            raw,_,sections=read_native(path)
            from .point_primitives import encode
            path.write_bytes(encode(raw,sections,styles));raw,_,sections=read_native(path)
            path.write_bytes(_new_point_boxes(raw,sections,texts,crs,scale))
        blob, parsed, _ = read_native(path)
        if crs is not None and (not parsed.crs or not parsed.crs.equals(crs,ignore_axis_order=True)):
            raise BridgeError("UNSUPPORTED_NATIVE_CRS_ENCODING: native readback changes the declared datum, ellipsoid or projection; no coordinate reference substitution is allowed")
        if len(parsed.geom) != len(geometries):
            raise BridgeError("NATIVE_READBACK_COUNT_MISMATCH")
        for source, actual in zip(geometries,parsed.geom):
            if not source.equals_exact(actual, tolerance=1e-7, normalize=True) and source.hausdorff_distance(actual)>1e-7:
                raise BridgeError("NATIVE_READBACK_GEOMETRY_MISMATCH")
        parameters={"POINT":parsed.point_params,"LINE":parsed.line_params,"POLYGON":parsed.polygon_params}[kind]
        for i,(style,properties) in enumerate(zip(styles,attributes)):
            for key,value in style.items():
                actual=parameters[key].iloc[i] if key in parameters else None
                equal=value==actual if isinstance(actual,Integral) else actual is not None and math.isfinite(actual) and math.isclose(value,float(actual),rel_tol=2e-7,abs_tol=1e-6)
                if not equal:
                    raise BridgeError(f"STYLE_READBACK_MISMATCH: {mapping[i]['feature_id']}.{key}; native storage would coerce the value")
            for key,value in properties.items():
                actual=parsed.data[key].iloc[i] if key in parsed.data else None
                if isinstance(value,int) and not isinstance(value,bool):
                    # A mixed numeric column is stored as floating point. Accept
                    # integer inputs only when native readback is exactly equal;
                    # int(actual) also avoids NumPy coercing a large input integer.
                    equal=(isinstance(actual,Integral) and int(actual)==value) or (
                        isinstance(actual,Real) and math.isfinite(actual)
                        and float(actual).is_integer() and int(actual)==value)
                elif isinstance(value,float):
                    equal=actual is not None and math.isfinite(value) and math.isfinite(actual) and math.isclose(value,float(actual),rel_tol=2e-7,abs_tol=1e-6)
                else:
                    equal=isinstance(value,str) and actual==value
                if not equal:
                    raise BridgeError(f"ATTRIBUTE_READBACK_MISMATCH: {mapping[i]['feature_id']}.{key}; use a representable native scalar")
        if kind=="POINT":
            _,_,sections=read_native(path)
            if [annotation_text(blob,sections,i) for i in range(1,len(texts)+1)]!=texts:
                raise BridgeError("NATIVE_TEXT_READBACK_MISMATCH")
    receipt = {"id":request_id,"kind":"CREATE","source":str(manifest),"source_sha256":payload_hash,
               "candidate_sha256":sha(blob),"output":str(output),"feature_count":len(mapping),
               "feature_mapping":mapping,"crs":crs_metadata(specification,declared),"earth_crs_known":declared is not None,
               "map_scale":scale,"coordinate_frame":"map","native_type":kind,
               "native_coordinate_representation":"user drawing coordinates; ground reference in receipt" if crs is None else "native earth projection",
               "geometry_readback":"VERIFIED","native_reopen":"NOT_VERIFIED",
               "native_region_walks":"encoded" if kind=="POLYGON" else None,
               "native_point_boxes":"encoded" if kind=="POINT" else None,
               "input_preserved":True,"changes":[],"changed_bytes":len(blob),
               "qa":template.get("qa",{}),"provenance":template.get("provenance",{})}
    if sources is not None:
        if any(sha(Path(item['file']).read_bytes())!=item['sha256'] for item in sources):raise BridgeError('STALE_SOURCE: native input changed while preparing')
        receipt['sources']=sources
    if modern:
        from .artifacts import begin_bundle, fail_bundle, finish_bundle
        from .crs_contract import make_crs_sidecar, sidecar_path
        contract = make_crs_sidecar(name, blob, declared)
        bound_sources = [{'file': str(manifest), 'sha256': payload_hash}, *(sources or [])]
        stage, previous, fresh = begin_bundle(bridge, payload, request_id, bound_sources, name)
        if not fresh:
            return previous
        try:
            files = stage / 'files'
            files.mkdir()
            vector = files / name
            vector.write_bytes(blob)
            sidecar = sidecar_path(vector)
            sidecar.write_text(json_text(contract), encoding='utf-8')
            read_native(vector)
            metadata = {key: value for key, value in receipt.items()
                        if key not in {'id', 'kind', 'source', 'source_sha256', 'output', 'sources'}}
            metadata.update(crs_delivery_mode=mode, crs_sidecar=sidecar.name,
                            native_coordinate_representation='unscaled user drawing; geodetic reference in hash-bound WKT2',
                            sidecar_sha256=sha(sidecar.read_bytes()))
            return finish_bundle(bridge, request_id, files, metadata)
        except Exception as exc:
            fail_bundle(bridge, request_id, exc)
            raise
    with bridge.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM commands WHERE id=?",(request_id,)).fetchone()
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise BridgeError("REQUEST_ID_CONFLICT")
            return bridge._receipt(existing)
        db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",(request_id,payload_hash,"PREPARED",
            str(manifest),encoded,blob,"[]",json_text(receipt)))
    return {**receipt,"state":"PREPARED"}


def prepare_modern_crs_creation(bridge, features, template, request_id, sources=None):
    """Prepare an atomic WT/WL/WP and CRS sidecar using the shared creator."""
    return prepare_creation(bridge, features, {**template, 'crs': {
        **template.get('crs', {}), 'crs_delivery_mode': 'dual_sidecar'}}, request_id, sources)
