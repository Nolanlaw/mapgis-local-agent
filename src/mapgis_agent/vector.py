"""Deterministic vector tasks using GEOS/PROJ, always on a new native branch."""
from __future__ import annotations

import copy
import math

from pyproj.transformer import TransformerGroup
from shapely import get_parts, polygonize_full, unary_union, set_precision, STRtree, line_merge, reverse, segmentize
from shapely.geometry import mapping, shape, MultiLineString
from shapely.ops import transform, split, substring
from shapely.affinity import affine_transform

from ._vector_ops import apply_operator
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
    source = apply_operator(operation_id, source, geometries, parameters, qa, crs)
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
