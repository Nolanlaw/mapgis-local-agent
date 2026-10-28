import json
import struct

import pytest

from mapgis_agent.server import build_server


def jpeg_header(path, width=400, height=300):
    path.write_bytes(b"\xff\xd8\xff\xc0" + struct.pack(">HBHH", 7, 8, height, width) + b"\xff\xd9")
    return path


def test_raster_workflow_is_registered_on_the_real_mcp_server(tmp_path):
    import asyncio
    names = {t.name for t in asyncio.run(build_server(tmp_path).list_tools())}
    assert {"mapgis_register_scan", "mapgis_prepare_georeference", "mapgis_scan_window"} <= names


def test_independent_checkpoints_expose_bad_fit(tmp_path):
    from mapgis_agent.raster import RasterWorkspace
    service = RasterWorkspace(tmp_path / "project")
    scan = service.register_scan(str(jpeg_header(tmp_path / "scan.jpg")), "scan")
    # Exact 3-point fit has zero residual even when the unused point disagrees.
    gcps = [{"pixel":[0,0],"world":[500000,3500000]},
            {"pixel":[100,0],"world":[500200,3500000]},
            {"pixel":[0,100],"world":[500000,3499800]}]
    result = service.prepare_georeference(scan["scan_id"], {"definition":"EPSG:32650","axis_order":"xy"},
        gcps, "fit", checkpoints=[{"pixel":[50,50],"world":[500120,3499900]}], max_error=5)
    assert result["residual"] < 1e-6
    assert result["checkpoints"][0]["error"] == pytest.approx(20)
    assert result["accepted"] is False
    assert result["residual_diagnostic"] is False


def test_preview_coordinates_require_source_transform(tmp_path):
    from mapgis_agent.raster import RasterWorkspace
    service = RasterWorkspace(tmp_path / "project")
    scan = service.register_scan(str(jpeg_header(tmp_path / "scan.jpg")), "scan")
    points = [{"pixel":[0,0],"world":[0,0]}, {"pixel":[10,0],"world":[10,0]},
              {"pixel":[0,10],"world":[0,-10]}]
    with pytest.raises(ValueError, match="PREVIEW_TRANSFORM_REQUIRED"):
        service.prepare_georeference(scan["scan_id"], {"definition":"EPSG:32650","axis_order":"xy"},
                                      points, "fit", pixel_frame="preview")


def test_degenerate_gcps_rejected_and_datum_not_guessed(tmp_path):
    from mapgis_agent.raster import RasterWorkspace
    service = RasterWorkspace(tmp_path / "project")
    scan = service.register_scan(str(jpeg_header(tmp_path / "scan.jpg")), "scan")
    points = [{"pixel":[x,x],"world":[500000+x,3500000+x]} for x in (0,10,20)]
    with pytest.raises(ValueError, match="EXPLICIT_CRS"):
        service.prepare_georeference(scan["scan_id"], {"definition":"UTM 50N"}, points, "badcrs")
    with pytest.raises((ValueError, RuntimeError), match="collinear|degenerate"):
        service.prepare_georeference(scan["scan_id"], {"definition":"EPSG:32650","axis_order":"xy"}, points, "degenerate")


def test_large_scan_uses_bounded_windows_and_records_full_resolution_offsets(tmp_path):
    from mapgis_agent.raster import RasterWorkspace
    service = RasterWorkspace(tmp_path / "project")
    scan = service.register_scan(str(jpeg_header(tmp_path / "scan.jpg",47958,47903)), "scan")
    assert scan["uncompressed_rgb_bytes"] == 6891996222
    plan = service.window_plan(scan["scan_id"], [20000,21000,2048,1024], 1024)
    assert plan["pixel_to_source"] == [2,0,20000,0,2,21000]
    assert plan["output_size"] == [1024,512]
    assert plan["decoder"] == "libvips-sequential"
    assert plan["max_output_bytes"] == 1572864
    with pytest.raises(ValueError, match="OUTSIDE_SCAN"):
        service.window_plan(scan["scan_id"], [47000,47000,2048,2048], 1024)
    assert service.register_scan(str(tmp_path / "scan.jpg"), "scan") == scan
    (tmp_path / "scan.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="STALE_SCAN"):
        service.window_plan(scan["scan_id"], [0,0,100,100], 100)
