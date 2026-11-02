"""Scan provenance, bounded libvips windows and explicit GCP coordinate frames."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import struct

from pyproj import CRS

from .core import Bridge, BridgeError, json_text, sha
from ._vendor.stratigraphic_amenity.georef.transform import GroundControlPoint, fit_affine
from ._vendor.stratigraphic_amenity.georef.errors import AffineFitError


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def affine_point(coefficients: list[float], point: list[float]) -> tuple[float, float]:
    a, b, c, d, e, f = coefficients
    x, y = point
    return a*x+b*y+c, d*x+e*y+f


def explicit_crs(value: dict) -> CRS | None:
    # A named native grid is useful even when the old file has no datum metadata.
    if value.get("kind") == "local":
        reference = value.get("reference_sha256", "")
        if (value.get("axis_order") != "xy" or not value.get("name")
                or value.get("unit") not in {"metre", "millimetre", "unknown"}
                or len(reference) != 64 or any(c not in "0123456789abcdef" for c in reference)):
            raise BridgeError("EXPLICIT_LOCAL_FRAME: name, xy axes, unit and reference_sha256 required")
        return None
    definition = value.get("definition", "")
    if value.get("axis_order") != "xy" or not isinstance(definition, str) or not (
        definition.upper().startswith(("EPSG:", "PROJCRS[", "GEOGCRS[", "PROJCS[", "GEOGCS["))
        or "+proj=" in definition and ("+datum=" in definition or "+ellps=" in definition)
    ):
        raise BridgeError("EXPLICIT_CRS: supply EPSG/WKT or full PROJ definition with datum/ellipsoid and axis_order=xy; no guessed UTM datum")
    return CRS.from_user_input(definition)


def crs_metadata(value: dict, resolved: CRS | None) -> dict:
    return dict(value) if resolved is None else {"definition":resolved.to_wkt(),"axis_order":"xy"}


def local_crs_matches(crs: CRS, frame: dict) -> bool:
    """Match an embedded engineering grid by its declared name, units and axes.

    WKT1 LOCAL_CS may omit its datum and use Easting/Northing axis names. It
    cannot be compared to a newly invented local datum or reprojected as Earth.
    """
    axes=crs.axis_info;factor={'metre':1.,'millimetre':.001}.get(frame.get('unit'))
    return (crs.is_engineering and crs.name==frame.get('name') and factor is not None
        and len(axes)==2 and [a.direction for a in axes]==['east','north']
        and all(math.isclose(a.unit_conversion_factor,factor,rel_tol=1e-12,abs_tol=0.) for a in axes))


def image_size(path: Path) -> tuple[int, int]:
    # JPEG SOF dimensions require no image allocation, including the 2.3Gpx scans.
    with path.open("rb") as stream:
        if stream.read(2) == b"\xff\xd8":
            for _ in range(4096):
                byte = stream.read(1)
                if not byte:
                    break
                if byte != b"\xff":
                    continue
                while byte == b"\xff":
                    byte = stream.read(1)
                if not byte:
                    break
                marker = byte[0]
                if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                    continue
                length = int.from_bytes(stream.read(2), "big")
                if length < 2:
                    break
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                    _, height, width = struct.unpack(">BHH", stream.read(5))
                    if width and height:
                        return width, height
                    break
                stream.seek(length-2, 1)
            raise BridgeError("INVALID_JPEG_HEADER")
    try:
        import pyvips
    except ImportError as exc:
        raise BridgeError("RASTER_DEPENDENCY_REQUIRED: install the pinned raster extra (pyvips[binary]); no full-image fallback") from exc
    image = pyvips.Image.new_from_file(str(path), access="sequential")
    return image.width, image.height


class RasterWorkspace:
    def __init__(self, root: str | Path):
        self.bridge = Bridge(root)
        self.root = self.bridge.root

    def _load(self, scan_id: str) -> dict:
        self.bridge.request_id(scan_id)
        manifest = self.bridge.destination_path(self.root / "work/scans" / scan_id / "scan.json")
        scan = json.loads(manifest.read_text(encoding="utf-8"))
        if file_sha(Path(scan["source"])) != scan["source_sha256"]:
            raise BridgeError("STALE_SCAN: original bytes changed")
        return scan

    def _load_window(self, scan_id: str, window_id: str) -> tuple[dict, dict, Path]:
        self.bridge.request_id(window_id)
        scan=self._load(scan_id)
        path=self.bridge.owned_path(self.root / "work/scans" / scan_id / "windows" / window_id / "window.json")
        window=json.loads(path.read_text(encoding="utf-8"))
        image=self.bridge.owned_path(window["output"])
        if window["plan"]["source_sha256"]!=scan["source_sha256"] or file_sha(image)!=window["output_sha256"]:
            raise BridgeError("STALE_WINDOW: original/window bytes changed")
        return scan,window,path

    def register_scan(self, source: str, request_id: str) -> dict:
        self.bridge.request_id(request_id)
        source_path = Path(source).resolve(strict=True)
        manifest = self.bridge.destination_path(self.root / "work/scans" / request_id / "scan.json")
        if manifest.exists():
            scan = self._load(request_id)
            if Path(scan["source"]) != source_path:
                raise BridgeError("REQUEST_ID_CONFLICT")
            return scan
        width, height = image_size(source_path)
        scan = {"scan_id":request_id, "source":str(source_path), "source_sha256":file_sha(source_path),
                "source_bytes":source_path.stat().st_size, "width":width, "height":height,
                "uncompressed_rgb_bytes":width*height*3,
                "pixel_frame":"source", "pixel_convention":"continuous coordinates from upper-left pixel edge; x right, y down",
                "map_crs":None, "full_image_decoded":False}
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("x", encoding="utf-8") as stream:
            stream.write(json_text(scan))
        return scan

    def window_plan(self, scan_id: str, window: list[int], max_dimension: int = 2048) -> dict:
        scan = self._load(scan_id)
        if len(window) != 4 or any(type(n) is not int for n in window):
            raise BridgeError("Window is [left, top, width, height] in full-resolution source pixels")
        x, y, width, height = window
        if min(x,y) < 0 or min(width,height) <= 0 or x+width > scan["width"] or y+height > scan["height"]:
            raise BridgeError("OUTSIDE_SCAN")
        if type(max_dimension) is not int or not 1 <= max_dimension <= 4096:
            raise BridgeError("max_dimension must be 1..4096")
        scale = min(1.0, max_dimension/max(width,height))
        ow, oh = max(1,round(width*scale)), max(1,round(height*scale))
        return {"scan_id":scan_id, "source_sha256":scan["source_sha256"], "source_window":window,
                "output_size":[ow,oh], "pixel_to_source":[width/ow,0,x,0,height/oh,y],
                "pixel_convention":scan["pixel_convention"], "decoder":"libvips-sequential",
                "max_output_bytes":ow*oh*3, "full_image_array":False}

    def render_window(self, scan_id: str, window: list[int], request_id: str, max_dimension: int = 2048) -> dict:
        self.bridge.request_id(request_id)
        plan = self.window_plan(scan_id, window, max_dimension)
        directory = self.bridge.destination_path(self.root / "work/scans" / scan_id / "windows" / request_id)
        receipt = directory / "window.json"
        output = directory / "window.png"
        if receipt.exists():
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            if previous["plan"] != plan or not output.is_file() or file_sha(output) != previous["output_sha256"]:
                raise BridgeError("OUTPUT_CONFLICT: reconcile window before replay")
            return previous
        import pyvips
        pyvips.cache_set_max(0)
        pyvips.cache_set_max_mem(64*1024*1024)
        scan = self._load(scan_id)
        # JPEG shrink-on-load and sequential scanlines avoid whole-image arrays.
        x, y, width, height = window
        image = pyvips.Image.new_from_file(scan["source"], access="sequential")
        image = image.crop(x,y,width,height)
        ow, oh = plan["output_size"]
        image = image.resize(ow/width, vscale=oh/height)
        if image.width != ow or image.height != oh:
            raise BridgeError("WINDOW_DIMENSION_MISMATCH")
        directory.mkdir(parents=True, exist_ok=False)
        image.pngsave(str(output))
        if image_size(output) != (ow,oh) or file_sha(Path(scan["source"])) != scan["source_sha256"]:
            raise BridgeError("RASTER_READBACK_FAILED")
        result = {"plan":plan,"output":str(output),"output_sha256":file_sha(output)}
        with receipt.open("x", encoding="utf-8") as stream:
            stream.write(json_text(result))
        return result


    def prepare_georeference(self, scan_id: str, crs: dict, gcps: list[dict], request_id: str,
                             checkpoints: list[dict] | None = None, max_error: float = 1.0,
                             pixel_frame: str = "source", pixel_to_source: list[float] | None = None) -> dict:
        self.bridge.request_id(request_id)
        scan = self._load(scan_id)
        resolved_crs = explicit_crs(crs)
        if pixel_frame != "source" and pixel_to_source is None:
            raise BridgeError("PREVIEW_TRANSFORM_REQUIRED")
        if pixel_frame == "source" and pixel_to_source is not None:
            raise BridgeError("Source coordinates cannot carry a second preview transform")
        if pixel_to_source is not None and (len(pixel_to_source) != 6 or not all(math.isfinite(x) for x in pixel_to_source)):
            raise BridgeError("Invalid pixel_to_source affine")
        if len(gcps) < 3 or not math.isfinite(max_error) or max_error <= 0:
            raise BridgeError("At least 3 distributed GCPs and positive max_error are required")
        def point(value):
            pixel = affine_point(pixel_to_source, value["pixel"]) if pixel_to_source else value["pixel"]
            if len(pixel) != 2 or len(value["world"]) != 2 or not all(math.isfinite(n) for n in (*pixel,*value["world"])):
                raise BridgeError("GCP coordinates must be finite 2D numbers")
            if not (0 <= pixel[0] <= scan["width"] and 0 <= pixel[1] <= scan["height"]):
                raise BridgeError("GCP_OUTSIDE_SCAN")
            return GroundControlPoint(*pixel,*value["world"])
        training = [point(p) for p in gcps]
        checks = [point(p) for p in checkpoints or []]
        if set((p.pixel_x,p.pixel_y) for p in training) & set((p.pixel_x,p.pixel_y) for p in checks):
            raise BridgeError("CHECKPOINT_NOT_INDEPENDENT: check points must be unused in fitting")
        try:
            affine = fit_affine(training)
            affine.solve(*affine.apply(0,0))  # Reject a singular map transform.
        except AffineFitError as exc:
            raise BridgeError(str(exc)) from exc
        errors = [{"pixel":[p.pixel_x,p.pixel_y], "world":[p.world_x,p.world_y],
                   "error":math.dist(affine.apply(p.pixel_x,p.pixel_y),(p.world_x,p.world_y))} for p in checks]
        result = {"request_id":request_id,"scan_id":scan_id,"source_sha256":scan["source_sha256"],
                  "crs":crs_metadata(crs,resolved_crs),"earth_crs_known":resolved_crs is not None,
                  "units":resolved_crs.axis_info[0].unit_name if resolved_crs else crs["unit"], "model":"affine", "pixel_frame":"source",
                  "pixel_convention":scan["pixel_convention"], "coefficients":list(affine.coefficients),
                  "residual":affine.residual,"residual_diagnostic":len(training)>3,
                  "gcps":[asdict(p) for p in training],"checkpoints":errors,"max_error":max_error,
                  "accepted":bool(checks) and affine.residual<=max_error and all(p["error"]<=max_error for p in errors),
                  "native_calibrated_raster":"NOT_VERIFIED"}
        target = self.bridge.destination_path(self.root / "work/georeference" / f"{request_id}.json")
        encoded = json_text(result)
        if target.exists():
            if target.read_text(encoding="utf-8") != encoded:
                raise BridgeError("REQUEST_ID_CONFLICT")
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open("x",encoding="utf-8") as stream:
                stream.write(encoded)
        return result


def write_georeferenced_window(raster: RasterWorkspace, scan_id: str, window_id: str,
                               georeference_id: str, request_id: str) -> dict:
    """Write bounded RGB GeoTIFF with 4 affine-consistent corner GCPs for MapGIS."""
    import numpy as np
    import pyvips
    import rasterio
    from rasterio.control import GroundControlPoint

    bridge=raster.bridge
    for value in (georeference_id,request_id):
        bridge.request_id(value)
    scan,window,manifest=raster._load_window(scan_id,window_id)
    fit_path=bridge.owned_path(raster.root / "work/georeference" / f"{georeference_id}.json")
    fit=json.loads(fit_path.read_text(encoding="utf-8"))
    if not fit.get("accepted") or fit["source_sha256"]!=scan["source_sha256"]:
        raise BridgeError("GEOREFERENCE_NOT_ACCEPTED")
    payload={"format_version":2,"scan_id":scan_id,"window_id":window_id,"fit_sha256":file_sha(fit_path),"window_sha256":file_sha(manifest)}
    payload_hash=sha(json_text(payload).encode())
    directory=bridge.destination_path(raster.root / "work/georaster" / request_id)
    output,receipt=directory / "raster.tif",directory / "receipt.json"
    native_gcp=directory / "raster.gcp"
    if receipt.is_file():
        previous=json.loads(receipt.read_text(encoding="utf-8"))
        if (previous["payload_hash"]!=payload_hash or not output.is_file() or file_sha(output)!=previous["output_sha256"]
                or not native_gcp.is_file() or file_sha(native_gcp)!=previous.get("native_gcp_sha256")):
            raise BridgeError("GEOREFERENCE_OUTPUT_CONFLICT")
        return previous
    image=pyvips.Image.new_from_file(window["output"],access="sequential").extract_band(0,n=3).cast("uchar")
    if max(image.width,image.height)>4096:
        raise BridgeError("BOUNDED_WINDOW_REQUIRED")
    gcps=[]
    for col,row in ((0,0),(image.width,0),(image.width,image.height),(0,image.height)):
        source=affine_point(window["plan"]["pixel_to_source"],[col,row])
        x,y=affine_point(fit["coefficients"],source)
        gcps.append(GroundControlPoint(row=row,col=col,x=x,y=y))
    resolved=explicit_crs(fit["crs"])
    if resolved:
        out_crs=rasterio.crs.CRS.from_wkt(resolved.to_wkt())
    else:
        name=fit["crs"]["name"].replace('"','""')
        unit=fit["crs"]["unit"]
        factor=0.001 if unit=="millimetre" else 1
        out_crs=rasterio.crs.CRS.from_wkt(f'LOCAL_CS["{name}",LOCAL_DATUM["Unspecified native grid",32767],UNIT["{unit}",{factor}],AXIS["X",EAST],AXIS["Y",NORTH]]')
    array=np.frombuffer(image.write_to_memory(),np.uint8).reshape(image.height,image.width,3)
    directory.mkdir(parents=True,exist_ok=False)
    with rasterio.open(output,"w",driver="GTiff",width=image.width,height=image.height,count=3,
                       dtype="uint8",compress="lzw",gcps=gcps,crs=out_crs) as destination:
        destination.write(array.transpose(2,0,1))
        # Keep provenance in the JSON receipt: MapGIS 6.7 warns on GDAL tag 42112.
    with rasterio.open(output) as check:
        actual,actual_crs=check.gcps
        if len(actual)!=4 or any(math.dist((a.x,a.y),(b.x,b.y))>1e-7 for a,b in zip(actual,gcps)) or check.shape!=(image.height,image.width):
            raise BridgeError("GEOTIFF_GCP_READBACK_MISMATCH")
    raster._load_window(scan_id,window_id)
    # The observed 6.7 TIFF converter discards TIFF GCPs. Its own GCP import
    # preserves these coordinates on normal close/reopen; never infer native
    # registration from successful TIFF conversion alone.
    rows=["控制点号\t图像坐标x\t图像坐标y\t理论坐标x\t理论坐标y\t残差"]
    rows.extend(f"{i},\t{p.col:.9f},\t{p.row:.9f},\t{p.x:.9f},\t{p.y:.9f},\t0.000000000" for i,p in enumerate(gcps,1))
    native_gcp.write_bytes(("\r\n".join(rows)+"\r\n").encode("gbk"))
    result={"request_id":request_id,"output":str(output),"output_sha256":file_sha(output),
            "native_gcp_file":str(native_gcp),"native_gcp_sha256":file_sha(native_gcp),
            "native_georeference_requires_gcp_import":True,
            "payload_hash":payload_hash,"crs":fit["crs"],"source_sha256":scan["source_sha256"],
            "gcps":[{"pixel":[p.col,p.row],"world":[p.x,p.y]} for p in gcps],
            "model":"affine-consistent GCPs; source pixels preserved; no raster warp",
            "native_msi_conversion":"NOT_VERIFIED"}
    with receipt.open("x",encoding="utf-8") as stream:
        stream.write(json_text(result))
    return result
