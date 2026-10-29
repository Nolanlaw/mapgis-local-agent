"""Exchange MapGIS vectors with standard GIS formats and SQL query attributes.

Provides read-only SQL query over native attribute tables and verified vector
export/import bridging GeoJSON, GPKG and ESRI Shapefile using GeoPandas/Pyogrio.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import mapping

from .core import Bridge, BridgeError, annotation_text, json_text, native_ids, read_native, scalar, sha
from .creation import PARAMS, TYPES, prepare_creation
from .raster import explicit_crs


def _safe_dbf_field_name(name: str) -> str:
    """Truncate field name to fit DBF 10-byte limit without breaking UTF-8 characters."""
    encoded = str(name).encode("utf-8")
    if len(encoded) <= 10:
        return str(name)
    return encoded[:10].decode("utf-8", errors="ignore")


def _safe_scalar(value: Any) -> Any:
    """Normalize pandas / numpy / float / datetime values to JSON-safe primitives."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    val = scalar(value)
    if isinstance(val, float) and (math.isnan(val) or not math.isfinite(val)):
        return None
    return val


def query_attributes(file: str, expected_sha256: str, sql: str, limit: int = 1000) -> dict:
    """Query native file attributes using an in-memory SQLite table named features.

    Enforces a strict read-only authorizer rejecting writes, ATTACH, PRAGMA,
    extension loading, and external mutations.
    """
    path = Path(file).resolve(strict=True)
    blob, reader, _ = read_native(path)
    actual_sha = sha(blob)
    if actual_sha != expected_sha256:
        raise BridgeError("STALE_SOURCE: inspect the current native file first")

    if type(limit) is not int or not 0<=limit<=100000:
        raise BridgeError("LIMIT: integer in 0..100000 required")

    df = reader.data.copy()
    if any(str(col).lower() == "__record_id" for col in df.columns):
        raise BridgeError("RESERVED_COLUMN: attribute name '__record_id' is reserved")

    ids = native_ids(reader)
    df.insert(0, "__record_id", ids)

    conn = sqlite3.connect(":memory:")
    try:
        df.to_sql("features", conn, index=False)

        allowed_actions = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }

        def authorizer(action, a1, a2, db_name, trigger_name):
            if action not in allowed_actions:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and a2 and a2.lower() in ("load_extension", "enable_load_extension"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        steps=0
        def progress():
            nonlocal steps
            steps+=1
            return int(steps>2000)
        conn.set_progress_handler(progress,1000)

        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limit + 1)
            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]
            columns = [col[0] for col in cursor.description] if cursor.description else []
            converted_rows = [[_safe_scalar(v) for v in row] for row in rows]
        except (sqlite3.Error, sqlite3.DatabaseError) as exc:
            raise BridgeError(f"SQL_QUERY_FAILED: {exc}") from exc
    finally:
        conn.close()

    return {
        "source_sha256": actual_sha,
        "columns": columns,
        "rows": converted_rows,
        "row_count": len(converted_rows),
        "limit": limit,
        "truncated": bool(truncated),
    }


def export_vector(file: str, expected_sha256: str, output_directory: str, driver: str = "GeoJSON") -> dict:
    """Export native vector to GeoJSON, GPKG, or ESRI Shapefile in a new empty directory."""
    path = Path(file).resolve(strict=True)
    blob, reader, sections = read_native(path)
    actual_sha = sha(blob)
    if actual_sha != expected_sha256:
        raise BridgeError("STALE_SOURCE: inspect the current native file first")

    out_dir = Path(output_directory).resolve()
    if out_dir.exists():
        raise BridgeError(f"DESTINATION_EXISTS: output_directory '{out_dir}' already exists; export requires newly created empty directory")

    driver_clean = driver.strip().lower()
    if driver_clean in ("geojson", "json"):
        driver_name = "GeoJSON"
        ext = ".geojson"
    elif driver_clean in ("gpkg", "geopackage"):
        driver_name = "GPKG"
        ext = ".gpkg"
    elif driver_clean in ("esri shapefile", "shapefile", "shp"):
        driver_name = "ESRI Shapefile"
        ext = ".shp"
    else:
        raise BridgeError(f"UNSUPPORTED_DRIVER: driver '{driver}' is not supported")

    ids = native_ids(reader)
    geometries = list(reader.geom)
    data = reader.data.copy()
    crs = reader.crs or None
    shape_type = reader.shapeType
    params = {"POINT": reader.point_params, "LINE": reader.line_params, "POLYGON": reader.polygon_params}[shape_type]

    texts = [annotation_text(blob, sections, rid) for rid in ids] if shape_type == "POINT" else [None] * len(ids)

    rec_col = "mgis_rec"
    while rec_col.casefold() in {str(c).casefold() for c in data.columns}:rec_col='_'+rec_col

    data_export = data.copy()
    data_export[rec_col] = ids

    if driver_name == "ESRI Shapefile":
        for col in data_export.columns:
            for val in data_export[col]:
                if isinstance(val, str) and len(val.encode("utf-8")) > 254:
                    raise BridgeError(f"SHAPEFILE_STRING_TRUNCATION: string in field '{col}' exceeds 254 bytes")
                if isinstance(val, int) and abs(val) >= 10**18:
                    raise BridgeError(f"SHAPEFILE_NUMERIC_TRUNCATION: integer in field '{col}' exceeds 18 digits")

        field_names = list(data_export.columns)
        truncated_names = [_safe_dbf_field_name(c) for c in field_names]
        if len(truncated_names) != len(set(truncated_names)):
            raise BridgeError("SHAPEFILE_FIELD_COLLISION: field name truncation collision in Shapefile export")

        field_map = {col: _safe_dbf_field_name(col) for col in field_names}
        data_export = data_export.rename(columns=field_map)
    else:
        field_map = {col: col for col in data_export.columns}

    gdf = gpd.GeoDataFrame(data_export, geometry=geometries, crs=crs)

    out_dir.mkdir(parents=True, exist_ok=False)
    stem = path.stem
    out_file = out_dir / f"{stem}{ext}"
    gdf.to_file(out_file, driver=driver_name)
    if driver_name=='GeoJSON' and crs is not None:
        # GeoJSON 2008 supports named CRSs. GDAL omits unrecognised custom
        # projected CRSs unless they are supplied explicitly; RFC7946 is WGS84
        # only and must not silently relabel native coordinates as degrees.
        document=json.loads(out_file.read_text(encoding='utf8'))
        document['crs']={'type':'name','properties':{'name':crs.to_wkt()}}
        out_file.write_text(json_text(document),encoding='utf8')

    # Standard format readback verification
    readback = gpd.read_file(out_file)
    if len(readback) != len(gdf):
        raise BridgeError(f"READBACK_VERIFICATION_FAILED: expected {len(gdf)} features, read back {len(readback)}")
    for idx, (g1, g2) in enumerate(zip(gdf.geometry, readback.geometry)):
        if not g1.equals_exact(g2, 1e-6) and g1.hausdorff_distance(g2) >= 1e-6:
            raise BridgeError(f"READBACK_VERIFICATION_FAILED: geometry mismatch at index {idx}")
    for exp_col in field_map.values():
        if exp_col not in readback.columns:
            raise BridgeError(f"READBACK_VERIFICATION_FAILED: attribute column '{exp_col}' missing in readback")
        for a,b in zip(gdf[exp_col],readback[exp_col]):
            a,b=_safe_scalar(a),_safe_scalar(b)
            equal=(a==b or isinstance(a,float) and isinstance(b,(float,int)) and math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12))
            if not equal:raise BridgeError(f'READBACK_ATTRIBUTE_LOSS: {exp_col}')
    if crs is not None and (readback.crs is None or not crs.equals(readback.crs)):
        raise BridgeError('READBACK_CRS_LOSS')

    file_hashes = {
        f.name: sha(f.read_bytes())
        for f in sorted(out_dir.iterdir())
        if f.is_file() and not f.name.endswith(".manifest.json")
    }

    features_meta = []
    for i, rid in enumerate(ids):
        feat_style = {}
        if params is not None:
            for k, v in params.iloc[i].items():
                val = _safe_scalar(v)
                if val is not None and isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                    feat_style[str(k)] = val
        features_meta.append({
            "physical_record_id": rid,
            "attribute_ID": _safe_scalar(data.iloc[i].get("ID")),
            "style": feat_style,
            "text": texts[i],
            "original_attributes": {str(k): _safe_scalar(v) for k, v in data.iloc[i].items()},
        })

    manifest = {
        "source_file": str(path),
        "source_sha256": actual_sha,
        "driver": driver_name,
        "format_variant": "GeoJSON 2008 named CRS" if driver_name=='GeoJSON' else driver_name,
        "native_type": shape_type,
        "crs": crs.to_wkt() if crs else None,
        "physical_id_column": rec_col,
        "field_schema": {col: str(dtype) for col, dtype in data.dtypes.items()},
        "field_map": field_map,
        "file_hashes": file_hashes,
        "features": features_meta,
    }
    manifest_file = out_dir / f"{stem}.manifest.json"
    manifest_file.write_text(json_text(manifest), encoding="utf-8")

    return {
        "status": "SUCCESS",
        "driver": driver_name,
        "output_directory": str(out_dir),
        "exported_file": str(out_file),
        "manifest_file": str(manifest_file),
        "source_sha256": actual_sha,
        "feature_count": len(ids),
        "file_hashes": file_hashes,
    }


def prepare_import(bridge: Bridge, file: str, expected_sha256: str, template: dict, request_id: str, layer: str | None = None) -> dict:
    """Prepare MapGIS native creation from single GeoJSON or GPKG vector file."""
    path = Path(file).resolve(strict=True)
    if not path.is_file():
        raise BridgeError(f"FILE_NOT_FOUND: '{path}' is not a regular file")
    blob = path.read_bytes()
    actual_sha = sha(blob)
    if actual_sha != expected_sha256:
        raise BridgeError("STALE_SOURCE: input file changed or hash mismatch")

    name = template.get("name", "")
    if not name or Path(name).name != name or any(c in name for c in '/\\:<>"|?*') or name.rstrip(". ") != name:
        raise BridgeError("INVALID_NAME: provide a filename, not a path")
    kind = TYPES.get(Path(name).suffix.lower())
    if kind is None:
        raise BridgeError("INVALID_EXTENSION: WT/WL/WP required")

    crs = explicit_crs(template.get("crs", {}))
    scale = template.get("map_scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise BridgeError("EXPLICIT_SCALE: positive map_scale denominator required")

    suffix = path.suffix.lower()
    if suffix in (".geojson", ".json"):
        format_type = "GeoJSON"
    elif suffix == ".gpkg":
        format_type = "GPKG"
    else:
        raise BridgeError(f"UNSUPPORTED_IMPORT_FORMAT: only GeoJSON and GPKG are supported, got '{suffix}'")

    if format_type == "GPKG":
        layers = pyogrio.list_layers(path)
        layer_names = [row[0] for row in layers]
        if not layer_names:
            raise BridgeError("EMPTY_GPKG: no layers found in GPKG")
        if layer is None:
            if len(layer_names) > 1:
                raise BridgeError(f"AMBIGUOUS_GPKG_LAYERS: multiple layers found in GPKG ({layer_names}); specify layer explicitly")
            layer = layer_names[0]
        else:
            if layer not in layer_names:
                raise BridgeError(f"LAYER_NOT_FOUND: layer '{layer}' not found in GPKG layers {layer_names}")

    raw_feature_ids: list[str|None] = []
    if format_type == "GeoJSON":
        try:
            raw_data = json.loads(blob.decode("utf-8"))
            if raw_data.get("type") == "FeatureCollection":
                for f in raw_data.get("features", []):
                    fid = f.get("id")
                    raw_feature_ids.append(None if fid is None else str(fid))
        except Exception as exc:
            raise BridgeError(f"INVALID_GEOJSON: {exc}") from exc
        defined=[f for f in raw_feature_ids if f is not None]
        if len(defined) != len(set(defined)):
            raise BridgeError("CONFLICTING_IDENTITIES: duplicate feature ids found in GeoJSON")

    gdf = gpd.read_file(path, layer=layer)
    if len(gdf) == 0:
        raise BridgeError("EMPTY_DATASET: no features found in input file")

    feature_ids: list[str] = []
    for idx, row in gdf.iterrows():
        if idx < len(raw_feature_ids) and raw_feature_ids[idx] is not None:
            fid = raw_feature_ids[idx]
        elif "id" in row and row["id"] is not None and not pd.isna(row["id"]):
            fid = str(row["id"])
        else:
            fid = f"import-{idx + 1}"
        feature_ids.append(fid)
    if len(feature_ids) != len(set(feature_ids)):
        raise BridgeError("CONFLICTING_IDENTITIES: duplicate feature IDs found")

    expected_types = {
        "POINT": {"Point"},
        "LINE": {"LineString"},
        "POLYGON": {"Polygon", "MultiPolygon"},
    }[kind]

    for idx, g in enumerate(gdf.geometry):
        if g is None or g.is_empty:
            raise BridgeError(f"INVALID_GEOMETRY: feature {feature_ids[idx]} has empty geometry")
        if not g.is_valid:
            raise BridgeError(f"INVALID_GEOMETRY: feature {feature_ids[idx]} has invalid geometry")
        if g.has_z or getattr(g, "has_m", False):
            raise BridgeError("NATIVE_2D_REQUIRED: explicit projection of Z/M before creating 6.x vectors")
        if not all(math.isfinite(c) for c in g.bounds):
            raise BridgeError("INVALID_GEOMETRY: non-finite coordinates")
        if g.geom_type not in expected_types:
            raise BridgeError(f"INVALID_GEOMETRY: feature {feature_ids[idx]} has type {g.geom_type}, expected {expected_types}")

    sidecar_path = None
    if template.get("sidecar"):
        sidecar_path = Path(template["sidecar"])
    elif template.get("manifest"):
        sidecar_path = Path(template["manifest"])
    else:
        adjacent = path.parent / f"{path.stem}.manifest.json"
        if adjacent.is_file():
            sidecar_path = adjacent

    sidecar_features = None
    sidecar_field_map = None
    physical_id_col = None

    if sidecar_path is not None:
        resolved_sidecar = sidecar_path.resolve()
        if not resolved_sidecar.is_file():
            raise BridgeError("SIDECAR_NOT_REGULAR_FILE: sidecar must be a regular file")
        if not resolved_sidecar.is_relative_to(bridge.root.resolve()):
            raise BridgeError("SIDECAR_NOT_PROJECT_LOCAL: sidecar must be within bridge.root")

        try:
            manifest_data = json.loads(resolved_sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BridgeError(f"INVALID_SIDECAR: {exc}") from exc

        file_hashes = manifest_data.get("file_hashes", {})
        if manifest_data.get('native_type')!=kind:raise BridgeError('SIDECAR_NATIVE_TYPE')
        for fname in file_hashes:
            if Path(fname).name != fname or any(c in fname for c in '/\\:<>"|?*'):
                raise BridgeError(f"SIDECAR_INVALID_FILENAME: invalid referenced filename '{fname}'")
        if file_hashes.get(path.name)!=actual_sha:
            raise BridgeError('SIDECAR_HASH_VERIFICATION_FAILED: sidecar must hash this exact input')
        for fname, expected_hash in file_hashes.items():
            ref_file = resolved_sidecar.parent / fname
            if not ref_file.is_file():
                raise BridgeError(f"SIDECAR_HASH_VERIFICATION_FAILED: referenced file '{fname}' not found")
            if sha(ref_file.read_bytes()) != expected_hash:
                raise BridgeError(f"SIDECAR_HASH_VERIFICATION_FAILED: hash mismatch for referenced file '{fname}'")

        sidecar_features = manifest_data.get("features")
        sidecar_field_map = manifest_data.get("field_map", {})
        physical_id_col = manifest_data.get("physical_id_column")
        if (not isinstance(sidecar_features,list) or len(sidecar_features)!=len(gdf) or not physical_id_col
                or physical_id_col not in gdf.columns or gdf[physical_id_col].duplicated().any()
                or set(gdf[physical_id_col])!={f['physical_record_id'] for f in sidecar_features}):
            raise BridgeError('SIDECAR_IDENTITIES_DIFFER')

    source_crs=gdf.crs
    if sidecar_path is not None and manifest_data.get('crs') is None:
        source_crs=None  # Verified local-coordinate export; GeoJSON drivers otherwise assume WGS84.
    if (source_crs is None)!=(crs is None) or source_crs is not None and not source_crs.equals(crs):
        raise BridgeError('IMPORT_CRS_MISMATCH: explicitly reproject before importing')

    reverse_map = {v: k for k, v in sidecar_field_map.items()} if sidecar_field_map else {}
    features_list = []

    for idx, row in gdf.iterrows():
        fid = feature_ids[idx]
        geom = row.geometry

        sc_feat = None
        if sidecar_features is not None:
            if physical_id_col and physical_id_col in row and not pd.isna(row[physical_id_col]):
                rid = row[physical_id_col]
                for sf in sidecar_features:
                    if sf.get("physical_record_id") == rid:
                        sc_feat = sf
                        break
            if sc_feat is None:raise BridgeError('SIDECAR_IDENTITIES_DIFFER')

        properties = {}
        for col in gdf.columns:
            if col == "geometry" or col == "id":
                continue
            if physical_id_col and col == physical_id_col:
                if sc_feat and physical_id_col not in sc_feat.get("original_attributes", {}):
                    continue
            val = _safe_scalar(row[col])
            orig_col_name = reverse_map.get(col, col)
            properties[orig_col_name] = val
        if sc_feat is not None:
            properties=dict(sc_feat['original_attributes'])

        feature_dict: dict[str, Any] = {
            "type": "Feature",
            "id": fid,
            "geometry": mapping(geom),
            "properties": properties,
        }

        if sc_feat is not None:
            if sc_feat.get("style"):
                feature_dict["style"] = {
                    k: v for k, v in sc_feat["style"].items()
                    if k in PARAMS[kind] and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                }
            if kind == "POINT" and sc_feat.get("text") is not None:
                feature_dict["text"] = sc_feat["text"]
        else:
            if template.get("style"):
                feature_dict["style"] = {
                    k: v for k, v in template["style"].items()
                    if k in PARAMS[kind] and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                }

        features_list.append(feature_dict)

    features_collection = {
        "type": "FeatureCollection",
        "features": features_list,
    }

    provenance=dict(template.get('provenance',{}))|{'operation':'vector.import','input_file':str(path),'input_sha256':actual_sha,
        'sidecar_sha256':sha(resolved_sidecar.read_bytes()) if sidecar_path is not None else None,'backend':'GeoPandas/Pyogrio'}
    return prepare_creation(bridge, features_collection, template|{'provenance':provenance}, request_id)


def prepare_export(bridge:Bridge,file:str,expected_sha256:str,driver:str,request_id:str)->dict:
    from .artifacts import begin_bundle,finish_bundle,fail_bundle
    path=Path(file).resolve(strict=True)
    names={'geojson':'.geojson','json':'.geojson','gpkg':'.gpkg','geopackage':'.gpkg',
           'esri shapefile':'.shp','shapefile':'.shp','shp':'.shp'}
    extension=names.get(driver.strip().lower())
    if extension is None:raise BridgeError('UNSUPPORTED_DRIVER')
    payload={'operation':'vector.export','source':str(path),'expected_sha256':expected_sha256,'driver':driver}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],path.stem+extension)
    if not new:return receipt
    try:
        result=export_vector(str(path),expected_sha256,str(stage/'files'),driver)
        return finish_bundle(bridge,request_id,stage/'files',{'backend':'GeoPandas/Pyogrio','driver':result['driver'],
            'feature_count':result['feature_count'],'source_preserved':True})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
