"""Deterministic vector tasks using GEOS/PROJ, always on a new native branch."""
from __future__ import annotations

import copy
import math

from pyproj.transformer import TransformerGroup
from shapely import get_parts, polygonize_full, unary_union
from shapely.geometry import mapping, shape
from shapely.ops import transform

from .core import Bridge, BridgeError, json_text, sha
from .creation import prepare_creation
from .raster import explicit_crs


OPERATIONS = {"topology.polygonize","overlay.clip","overlay.difference","geometry.buffer",
              "geometry.simplify","geometry.dissolve","projection.transform","style.classify"}


def prepare_vector_operation(bridge: Bridge, operation_id: str, features: dict, template: dict,
                             parameters: dict, request_id: str) -> dict:
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
    qa = {"source_feature_ids":[f["id"] for f in source],"source_count":len(source),
          "units":crs.axis_info[0].unit_name if crs else template["crs"]["unit"],"backend":"GEOS/PROJ; native UI execution is a separate path"}
    if operation_id in {"geometry.buffer","geometry.simplify"} and crs is None and qa["units"] == "unknown":
        raise BridgeError("KNOWN_DISTANCE_UNIT_REQUIRED: local grid unit is unknown")
    if operation_id in {"geometry.buffer","geometry.simplify","topology.polygonize"} and crs and not crs.is_projected:
        raise BridgeError("PROJECTED_UNITS_REQUIRED: angular coordinates cannot define planar distance/tolerance")
    if operation_id == "topology.polygonize":
        roles = [f.get("properties",{}).get("role") for f in source]
        if any(role not in {"boundary","fault","contour","annotation","protected"} for role in roles):
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
        output=[]
        for f,g in zip(source,geometries):
            result = getattr(g,operation)(mask)
            if not result.is_empty:
                output.append({**f,"geometry":mapping(result)})
        source=output
        qa.update({"mask_crs":"same explicitly declared input CRS", "attributes_preserved":True})
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
    collection["features"]=source
    qa["output_count"]=len(source)
    provenance={"operation_id":operation_id,"source_sha256":sha(json_text(features).encode()),"parameters":parameters}
    return prepare_creation(bridge,collection,{**template,"provenance":provenance,"qa":qa},request_id)
