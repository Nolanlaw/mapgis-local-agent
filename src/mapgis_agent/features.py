"""Bridge saved native geometry into the existing GEOS/PROJ tools."""
from __future__ import annotations

from pathlib import Path

from shapely.geometry import mapping

from .core import Bridge, BridgeError, annotation_text, native_ids, read_native, scalar, sha
from .creation import PARAMS, TYPES
from .vector import prepare_vector_operation
from .comparison import _extract_style


def _native_collection(file: str, expected_sha256: str, record_ids: list[int] | None,
                       offset: int = 0, limit: int | None = None):
    path = Path(file).resolve(strict=True)
    blob, reader, sections = read_native(path)
    if sha(blob) != expected_sha256:
        raise BridgeError("STALE_SOURCE: inspect the current native file first")
    ids = native_ids(reader)
    if record_ids is not None and (
            not record_ids or any(type(rid) is not int for rid in record_ids)
            or len(record_ids) != len(set(record_ids)) or not set(record_ids).issubset(ids)):
        raise BridgeError("RECORD_IDS: unique active physical record IDs required")
    selected = set(ids if record_ids is None else record_ids)
    indices = [i for i, rid in enumerate(ids) if rid in selected]
    selected_count = len(indices)
    indices = indices[offset:] if limit is None else indices[offset:offset + limit]
    features = []
    for index in indices:
        rid = ids[index]
        feature = {"type": "Feature", "id": str(rid), "geometry": mapping(reader.geom[index]),
                   "properties": {str(k): scalar(v) for k, v in reader.data.iloc[index].items()},
                   "style": {k:v for k,v in _extract_style(reader,index).items() if v is not None}}
        if reader.shapeType == "POINT":
            text = annotation_text(blob, sections, rid)
            if text is not None:
                feature["text"] = text
        features.append(feature)
    source = {"path": str(path), "sha256": sha(blob), "native_type": reader.shapeType,
              "physical_record_ids": [int(f["id"]) for f in features],
              "selected_count": selected_count,
              "crs_wkt": reader.crs.to_wkt() if reader.crs else None,
              "coordinate_units": "REQUIRE_EXPLICIT_SOURCE_CONTEXT",
              "identity": "physical slots; attribute ID may repeat"}
    if reader.shapeType=='POINT' and reader.point_params['point_type'].isin([2,3]).any():
        source['geometry_semantics']='WT circle/arc records use Point centres plus exact radius_mm/angle style parameters; a centre is not the curve footprint'
    return {"type": "FeatureCollection", "features": features}, source


def read_features(file: str, expected_sha256: str, record_ids: list[int] | None = None,
                  offset: int = 0, limit: int = 100) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise BridgeError("PAGING: offset >= 0 and limit 1..100 must be integers")
    collection, source = _native_collection(file, expected_sha256, record_ids, offset, limit)
    count = source["selected_count"]
    next_offset = offset + len(collection["features"])
    return {"source": source, "collection": collection, "count": count,
            "returned": len(collection["features"]),
            "next_offset": next_offset if next_offset < count else None,
            "native_reopen": "NOT_VERIFIED_BY_FILE_READ"}


def prepare_file_operation(bridge: Bridge, operation_id: str, file: str, expected_sha256: str,
                           template: dict, parameters: dict, request_id: str,
                           record_ids: list[int] | None = None) -> dict:
    collection, source = _native_collection(file, expected_sha256, record_ids)
    if any(f['style'].get('point_type') in (2,3) for f in collection['features']):
        raise BridgeError('PARAMETRIC_GEOMETRY: this GEOS operation would use only WT circle/arc centres; use native subset/transform or an explicit curve conversion')
    parameters = parameters.copy()
    roles = parameters.pop("role_by_record", {})
    allowed_roles = {"boundary", "fault", "contour", "annotation", "protected", "dyke"}
    if (not isinstance(roles, dict) or any(k not in {f["id"] for f in collection["features"]}
                                         or v not in allowed_roles for k, v in roles.items())):
        raise BridgeError("SOURCE_ROLES: explicit selected physical IDs and supported line roles required")
    output_kind = TYPES.get(Path(template.get("name", "")).suffix.lower())
    for feature in collection["features"]:
        if feature["id"] in roles:
            feature["properties"]["role"] = roles[feature["id"]]
        # A buffer, for example, needs area styles rather than point parameters.
        feature["style"] = ({k: v for k, v in feature["style"].items() if k in PARAMS[output_kind]}
                            if output_kind == source["native_type"] else {})
        if output_kind != "POINT":
            feature.pop("text", None)
    template = {**template, "qa": {**template.get("qa", {}), "native_source": source,
                                  "native_auxiliary_sections_copied": False,
                                  "result_kind": "new derived file; source stays unchanged"}}
    return prepare_vector_operation(bridge, operation_id, collection, template, parameters, request_id,
        [{'file':source['path'],'sha256':source['sha256']}])
