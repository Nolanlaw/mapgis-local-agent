"""Deterministic vector tasks using GEOS/PROJ, always on a new native branch."""
from __future__ import annotations

import copy
import math

from pyproj.transformer import TransformerGroup
from shapely import get_parts, polygonize_full, unary_union, set_precision, STRtree, line_merge, reverse, segmentize
from shapely.geometry import mapping, shape, MultiLineString
from shapely.ops import transform, split, substring
from shapely.affinity import affine_transform

from .core import Bridge, BridgeError, json_text, sha
from .creation import prepare_creation
from .raster import explicit_crs


OPERATIONS = {"topology.polygonize","overlay.clip","overlay.difference","geometry.buffer",
              "geometry.simplify","geometry.dissolve","projection.transform","style.classify",
              "topology.node","topology.trim_dangles","topology.snap_grid","correction.affine",
              "geometry.replace","line.split","polygon.split","line.merge",
              "line.reverse","line.offset","line.substring","geometry.densify"}


def prepare_vector_operation(bridge: Bridge, operation_id: str, features: dict, template: dict,
                             parameters: dict, request_id: str, sources: list[dict] | None = None) -> dict:
    if operation_id not in OPERATIONS:
        raise BridgeError("UNKNOWN_VECTOR_OPERATION")
    crs = explicit_crs(template.get("crs",{}))
    collection = copy.deepcopy(features)
    if collection.get("type") != "FeatureCollection" or not collection.get("features"):
        raise BridgeError("Non-empty FeatureCollection required")
    source = collection["features"]
    if len({f.get("id") for f in source}) != len(source) or any(not isinstance(f.get("id"),str) for f in source):
        raise BridgeError("FEATURE_ID: unique stable string IDs required")
    geometries = [shape(feature["geometry"]) for feature in source]
    if any(g.is_empty or not g.is_valid or not all(math.isfinite(n) for n in g.bounds) for g in geometries):
        raise BridgeError("INVALID_GEOMETRY: repair explicitly before operation")
    qa = {**template.get("qa",{}),"source_feature_ids":[f["id"] for f in source],"source_count":len(source),
          "units":crs.axis_info[0].unit_name if crs else template["crs"]["unit"],"backend":"GEOS/PROJ; native UI execution is a separate path"}
    distance_ops = {"geometry.buffer", "geometry.simplify", "line.offset", "line.substring", "geometry.densify"}
    if operation_id in distance_ops and crs is None and qa["units"] == "unknown":
        raise BridgeError("KNOWN_DISTANCE_UNIT_REQUIRED: local grid unit is unknown")
    if operation_id in distance_ops | {"topology.polygonize","topology.trim_dangles"} and crs and not crs.is_projected:
        raise BridgeError("PROJECTED_UNITS_REQUIRED: angular coordinates cannot define planar distance/tolerance")
    if operation_id == 'geometry.replace':
        updates = parameters.get('updates')
        if not isinstance(updates, list) or not updates or not all(isinstance(u, dict) for u in updates):
            raise BridgeError('EDIT_UPDATES_REQUIRED')
        by_id = {f['id']: i for i, f in enumerate(source)}
        edited = []
        for update in updates:
            key = update.get('feature_id')
            if not isinstance(key, str) or key not in by_id:
                raise BridgeError('EDIT_UNKNOWN_FEATURE')
            if key in edited:
                raise BridgeError('EDIT_DUPLICATE_FEATURE')
            original = geometries[by_id[key]]
            try:
                replacement = shape(update['geometry'])
                family = lambda g: g.geom_type.removeprefix('Multi')
                if (replacement.is_empty or not replacement.is_valid or replacement.has_z
                        or getattr(replacement, 'has_m', False) or family(replacement) != family(original)
                        or not all(math.isfinite(n) for n in replacement.bounds)):
                    raise ValueError('invalid 2D geometry or changed native family')
            except (KeyError, TypeError, ValueError) as exc:
                raise BridgeError('EDIT_INVALID_GEOMETRY') from exc
            source[by_id[key]]['geometry'] = mapping(replacement)
            edited.append(key)
        qa.update(edited_feature_ids=edited, attribute_policy='original attributes, text and style retained; other selected geometry unchanged')
    elif operation_id in {'line.split', 'polygon.split'}:
        required_type = 'LineString' if operation_id == 'line.split' else 'Polygon'
        if any(g.geom_type not in {required_type, 'Multi' + required_type} for g in geometries):
            raise BridgeError('SPLIT_GEOMETRY_TYPE')
        try:
            cutter = shape(parameters['splitter'])
            allowed = {'Point', 'MultiPoint', 'LineString', 'MultiLineString'} if required_type == 'LineString' else {'LineString'}
            if cutter.is_empty or not cutter.is_valid or cutter.has_z or cutter.geom_type not in allowed:
                raise ValueError('unsupported splitter')
            parts = [list(split(g, cutter).geoms) for g in geometries]
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeError('SPLIT_INVALID_SPLITTER: no implicit snap or overlapping linework') from exc
        if not any(len(items) > 1 for items in parts):
            raise BridgeError('SPLIT_DID_NOT_CUT: splitter must intersect exactly; snap explicitly first')
        source = [{**f, 'id': f['id'] + ':split-' + str(i), 'geometry': mapping(g)}
                  for f, items in zip(source, parts) for i, g in enumerate(items, 1)]
        qa.update(split_parts=[len(items) for items in parts], snapping_applied=False,
                  attribute_policy='each result retains its source attributes, text and style')
    elif operation_id == 'line.merge':
        if len(source) < 2 or any(g.geom_type != 'LineString' for g in geometries):
            raise BridgeError('MERGE_LINES_REQUIRED')
        directed = parameters.get('directed', True)
        if type(directed) is not bool:
            raise BridgeError('MERGE_DIRECTION_REQUIRED')
        selected = parameters.get('attribute_source_id')
        if selected is None:
            metadata = [json_text({k: f.get(k) for k in ('properties', 'style', 'text')}) for f in source]
            if len(set(metadata)) != 1:
                raise BridgeError('MERGE_METADATA_CONFLICT: choose attribute_source_id explicitly')
            chosen = source[0]
        else:
            chosen = next((f for f in source if f['id'] == selected), None)
            if chosen is None:
                raise BridgeError('EDIT_UNKNOWN_FEATURE')
        merged = line_merge(MultiLineString(geometries), directed=directed)
        if merged.is_empty or merged.geom_type != 'LineString':
            raise BridgeError('MERGE_NOT_CONTIGUOUS: endpoints and direction must agree; no implicit snap')
        qa.update(merged_source_ids=[f['id'] for f in source], attribute_source_id=chosen['id'], directed=directed,
                  attribute_policy='all metadata from the explicitly selected source, or identical input metadata')
        source = [{**chosen, 'id': 'merge:' + chosen['id'], 'geometry': mapping(merged)}]
    elif operation_id in {'line.reverse', 'line.offset', 'line.substring', 'geometry.densify'}:
        if operation_id != 'geometry.densify' and any(g.geom_type != 'LineString' for g in geometries):
            raise BridgeError('EDIT_LINES_REQUIRED')
        def number(name):
            value = parameters.get(name)
            if type(value) not in (float, int) or not math.isfinite(value):
                raise BridgeError('EDIT_FINITE_PARAMETER: ' + name)
            return value
        if operation_id == 'line.reverse':
            transformed = [reverse(g) for g in geometries]
        elif operation_id == 'line.offset':
            distance = number('distance')
            segments = parameters.get('quad_segs', 8)
            join = parameters.get('join_style', 'round')
            mitre = parameters.get('mitre_limit', 5.0)
            if (distance == 0 or type(segments) is not int or not 4 <= segments <= 128
                    or join not in {'round', 'bevel', 'mitre'} or type(mitre) not in (int, float)
                    or not math.isfinite(mitre) or mitre <= 0):
                raise BridgeError('OFFSET_PARAMETERS')
            transformed = [g.offset_curve(distance, quad_segs=segments, join_style=join, mitre_limit=mitre) for g in geometries]
            qa.update(distance=distance, side='left' if distance > 0 else 'right', quad_segs=segments, join_style=join, mitre_limit=mitre)
        elif operation_id == 'line.substring':
            start, end = number('start'), number('end')
            normalized = parameters.get('normalized', False)
            if type(normalized) is not bool or not 0 <= start < end or any(end > (1 if normalized else g.length) for g in geometries):
                raise BridgeError('SUBSTRING_RANGE: 0 <= start < end <= length or normalized 1; no clamping')
            transformed = [substring(g, start, end, normalized=normalized) for g in geometries]
            qa.update(start=start, end=end, normalized=normalized)
        else:
            maximum = number('max_segment_length')
            if maximum <= 0:
                raise BridgeError('DENSIFY_POSITIVE_LENGTH')
            transformed = [segmentize(g, maximum) for g in geometries]
            qa['max_segment_length'] = maximum
        if any(g.is_empty or not g.is_valid for g in transformed):
            raise BridgeError('EDIT_COLLAPSED_GEOMETRY')
        source = [{**f, 'geometry': mapping(g)} for f, g in zip(source, transformed)]
        qa['attribute_policy'] = 'original source attributes, text and style retained'
    elif operation_id=='topology.trim_dangles':
        if set(parameters)!={'max_length'} or type(parameters['max_length']) not in (int,float) or not math.isfinite(parameters['max_length']) or parameters['max_length']<=0:
            raise BridgeError('TRIM_LENGTH: explicit positive maximum in source coordinate units required')
        roles=[f.get('properties',{}).get('role') for f in source]
        if any(role not in {'boundary','fault','contour','annotation','protected','dyke'} for role in roles):raise BridgeError('EXPLICIT_LINE_ROLE_REQUIRED')
        indexes=[i for i,role in enumerate(roles) if role=='boundary']
        if not indexes or any(g.geom_type not in {'LineString','MultiLineString'} for g in geometries):raise BridgeError('TRIM_LINES_REQUIRED')
        noded=unary_union([geometries[i] for i in indexes])
        branches=list(get_parts(line_merge(noded)));degree={}
        for g in branches:
            for p in (g.coords[0],g.coords[-1]):degree[p]=degree.get(p,0)+1
        removed_indexes={i for i,g in enumerate(branches) if g.length<=parameters['max_length'] and (degree[g.coords[0]]==1 or degree[g.coords[-1]]==1)}
        removed=[branches[i] for i in sorted(removed_indexes)]
        retained=unary_union([g for i,g in enumerate(branches) if i not in removed_indexes])
        tree=STRtree([geometries[i] for i in indexes]);output=[]
        for segment in get_parts(noded):
            if not retained.covers(segment):continue
            for local_index in tree.query(segment,predicate='covered_by'):
                original=source[indexes[local_index]]
                output.append({**original,'id':str(len(output)+1)+':'+original['id'],'geometry':mapping(segment)})
        output.extend(f for f,role in zip(source,roles) if role!='boundary')
        qa.update(noded=True,trimmed_branches=len(removed),trimmed_length=sum(g.length for g in removed),
            max_length=parameters['max_length'],trim_policy='one pass over complete degree-2-merged leaf branches; no recursive erosion or snapping',
            preserved_nonboundary_ids=[f['id'] for f,role in zip(source,roles) if role!='boundary'],
            attribute_policy='each retained noded segment keeps every covering source record; protected roles remain unchanged')
        source=output
    elif operation_id=='topology.node':
        if any(g.geom_type not in {'LineString','MultiLineString'} for g in geometries):raise BridgeError('NODE_LINES_REQUIRED')
        tree=STRtree(geometries);output=[]
        for segment in get_parts(unary_union(geometries)):
            for index in tree.query(segment,predicate='covered_by'):
                f=source[index]
                output.append({**f,'id':str(len(output)+1)+':'+f['id'],'geometry':mapping(segment)})
        qa.update(noded=True,attribute_policy='each segment retains every covering source record; overlaps retain distinct provenance')
        source=output
    elif operation_id=='topology.snap_grid':
        grid=parameters.get('grid_size')
        if type(grid) not in (int,float) or not math.isfinite(grid) or grid<=0:raise BridgeError('SNAP_GRID_SIZE')
        if crs and crs.is_geographic:raise BridgeError('PROJECTED_UNITS_REQUIRED')
        snapped=[set_precision(g,grid_size=grid,mode='valid_output') for g in geometries]
        if any(g.is_empty or not g.is_valid or g.geom_type!=original.geom_type for g,original in zip(snapped,geometries)):
            raise BridgeError('SNAP_GRID_COLLAPSED_GEOMETRY')
        qa.update(grid_size=grid,max_hausdorff_distance=max(a.hausdorff_distance(b) for a,b in zip(geometries,snapped)),
            precision_policy='explicit zero-origin shared lattice in declared source units')
        source=[{**f,'geometry':mapping(g)} for f,g in zip(source,snapped)]
    elif operation_id=='correction.affine':
        matrix=parameters.get('matrix')
        if (not isinstance(matrix,list) or len(matrix)!=6 or any(type(v) not in (int,float) or not math.isfinite(v) for v in matrix)
                or abs(matrix[0]*matrix[3]-matrix[1]*matrix[2])<1e-15):raise BridgeError('AFFINE_MATRIX: nonsingular [a,b,d,e,xoff,yoff] required')
        source=[{**f,'geometry':mapping(affine_transform(g,matrix))} for f,g in zip(source,geometries)]
        qa.update(affine_matrix=matrix,coordinate_policy='explicit planar affine in declared input/output frame; no earth datum inferred')
    elif operation_id == "topology.polygonize":
        roles = [f.get("properties",{}).get("role") for f in source]
        if any(role not in {"boundary","fault","contour","annotation","protected","dyke"} for role in roles):
            raise BridgeError("EXPLICIT_LINE_ROLE_REQUIRED")
        boundaries = [g for g,role in zip(geometries,roles) if role == "boundary"]
        if not boundaries or any(g.geom_type not in {"LineString","MultiLineString"} for g in boundaries):
            raise BridgeError("Polygonize requires boundary linework")
        noded = unary_union(boundaries)
        polygons, cuts, dangles, invalid = polygonize_full(get_parts(noded))
        qa.update({"cuts":len(cuts.geoms),"dangles":len(dangles.geoms),"invalid_rings":len(invalid.geoms),
                   "preserved_nonboundary_ids":[f["id"] for f,role in zip(source,roles) if role != "boundary"],
                   "excluded_features":[f for f,role in zip(source,roles) if role != "boundary"],
                   "node_snapping_applied":False,"input_lines_deleted":False})
        if invalid.geoms or not polygons.geoms:
            raise BridgeError("TOPOLOGY_INVALID: no valid closed areas; inspect linework")
        source = [{"type":"Feature","id":f"area-{index}","geometry":mapping(g),
                   "properties":{"ID":index}} for index,g in enumerate(polygons.geoms,1)]
    elif operation_id.startswith("overlay."):
        mask = shape(parameters["mask"])
        if mask.is_empty or not mask.is_valid:
            raise BridgeError("INVALID_MASK")
        operation = "intersection" if operation_id == "overlay.clip" else "difference"
        output=[];contacts=[]
        def same_family(result,family):
            if result.is_empty:return []
            if result.geom_type==family:return [result]
            if hasattr(result,'geoms'):return [part for g in result.geoms for part in same_family(g,family)]
            return []
        for f,g in zip(source,geometries):
            result = getattr(g,operation)(mask)
            if not result.is_empty:
                parts=same_family(result,g.geom_type.removeprefix('Multi'))
                if parts:output.append({**f,"geometry":mapping(unary_union(parts))})
                else:contacts.append(f['id'])
        source=output
        qa.update({"mask_crs":"same explicitly declared input CRS", "attributes_preserved":True,
            "lower_dimension_contacts":contacts,"dimension_policy":"retain input geometry family; boundary-only lower-dimensional contacts omitted"})
    elif operation_id == "geometry.buffer":
        distance = parameters.get("distance")
        if isinstance(distance,bool) or not isinstance(distance,(float,int)) or not math.isfinite(distance) or distance<=0:
            raise BridgeError("BUFFER_DISTANCE: finite positive map units required")
        segments = parameters.get("quad_segs",16)
        if type(segments) is not int or not 4 <= segments <= 128:
            raise BridgeError("quad_segs must be 4..128")
        source=[{**f,"geometry":mapping(g.buffer(distance,quad_segs=segments))} for f,g in zip(source,geometries)]
        qa.update({"distance":distance,"quad_segs":segments,"analytic_area_tolerance":"polygonal arc approximation"})
    elif operation_id == "geometry.simplify":
        tolerance=parameters.get("tolerance")
        if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<=0:
            raise BridgeError("Finite positive tolerance required")
        simplified=[g.simplify(tolerance,preserve_topology=True) for g in geometries]
        if any(g.is_empty or not g.is_valid for g in simplified):
            raise BridgeError("SIMPLIFICATION_COLLAPSED_GEOMETRY")
        qa["max_hausdorff_distance"] = max(a.hausdorff_distance(b) for a,b in zip(geometries,simplified))
        source=[{**f,"geometry":mapping(g)} for f,g in zip(source,simplified)]
    elif operation_id == "geometry.dissolve":
        field=parameters.get("field")
        if 'grid_size' in parameters:
            grid=parameters['grid_size']
            if type(grid) not in (int,float) or not math.isfinite(grid) or grid<=0:raise BridgeError('SNAP_GRID_SIZE')
            if crs and crs.is_geographic:raise BridgeError('PROJECTED_UNITS_REQUIRED')
            snapped=[set_precision(g,grid,mode='valid_output') for g in geometries]
            if any(g.is_empty or not g.is_valid for g in snapped):raise BridgeError('SNAP_GRID_COLLAPSED_GEOMETRY')
            qa.update(grid_size=grid,max_hausdorff_distance=max(a.hausdorff_distance(b) for a,b in zip(geometries,snapped)))
            geometries=snapped
        groups={}
        for f,g in zip(source,geometries):
            if field not in f.get("properties",{}):
                raise BridgeError("DISSOLVE_FIELD_MISSING")
            key=json_text(f["properties"][field])
            groups.setdefault(key,[]).append((f,g))
        source=[{"type":"Feature","id":f"dissolve-{index}",
                 "geometry":mapping(unary_union([g for _,g in group])),
                 "properties":{field:group[0][0]["properties"][field]},
                 } for index,group in enumerate(groups.values(),1)]
        qa["attribute_policy"]="group field retained; other attributes require an explicit aggregation and are omitted"
    elif operation_id == "projection.transform":
        original = explicit_crs(parameters.get("source_crs",{}))
        if original is None or crs is None:
            raise BridgeError("EARTH_CRS_REQUIRED: a local frame has no known datum or earth projection")
        group = TransformerGroup(original,crs,always_xy=True,allow_ballpark=False)
        if not group.transformers or not group.best_available:
            raise BridgeError("PRECISE_TRANSFORM_UNAVAILABLE: missing grids or only ballpark operation; source unchanged")
        transformer=group.transformers[0]
        same_datum = original.geodetic_crs.equals(crs.geodetic_crs,ignore_axis_order=True)
        if transformer.accuracy<0 and not same_datum and not parameters.get("allow_unknown_accuracy",False):
            raise BridgeError("TRANSFORM_ACCURACY_UNKNOWN: explicit tolerance decision required")
        source=[{**f,"geometry":mapping(transform(lambda x,y,z=None: transformer.transform(x,y,errcheck=True),g))}
                for f,g in zip(source,geometries)]
        qa.update({"coordinates_transformed":True,"always_xy":True,"transform_description":transformer.description,
                   "transform_accuracy_m":transformer.accuracy,"same_geodetic_crs":same_datum,
                   "source_crs":original.to_wkt(),"ballpark":False})
    elif operation_id == "style.classify":
        field=parameters.get("field")
        styles=parameters.get("mapping",{})
        missing=sorted({str(f.get("properties",{}).get(field)) for f in source
                        if str(f.get("properties",{}).get(field)) not in styles})
        if missing:
            raise BridgeError("UNMAPPED_CLASSES: " + ",".join(missing))
        source=[{**f,"style":{**f.get("style",{}),**styles[str(f["properties"][field])]}} for f in source]
        qa.update({"classified_count":len(source),"geometry_unchanged":True})
    if not source:
        raise BridgeError("EMPTY_RESULT: no native file created")
    expanded=[]
    for f in source:
        geometry=shape(f['geometry'])
        if geometry.geom_type in {'MultiLineString','MultiPoint'}:
            expanded.extend({**f,'id':f['id']+':part-'+str(i),'geometry':mapping(g)} for i,g in enumerate(geometry.geoms,1))
        else:expanded.append(f)
    source=expanded
    collection["features"]=source
    qa["output_count"]=len(source)
    provenance={"operation_id":operation_id,"source_sha256":sha(json_text(features).encode()),"parameters":parameters}
    return prepare_creation(bridge,collection,{**template,"provenance":provenance,"qa":qa},request_id,sources)
