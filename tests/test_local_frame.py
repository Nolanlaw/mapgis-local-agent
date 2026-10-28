import pytest
import struct
from pathlib import Path
from pymapgis import Reader
from shapely.geometry import Point

from mapgis_agent.core import Bridge
from mapgis_agent.creation import prepare_creation
from test_georeference import jpeg_header
from test_native_creation import collection

LOCAL={"kind":"local","name":"course-native-grid","axis_order":"xy","unit":"unknown",
       "reference_sha256":"a"*64}

def test_explicit_unknown_local_frame_is_preserved_without_guessed_earth_crs(tmp_path):
    from mapgis_agent.raster import RasterWorkspace
    service=RasterWorkspace(tmp_path)
    service.register_scan(str(jpeg_header(tmp_path / "scan.jpg")),"scan")
    points=[{"pixel":[0,0],"world":[20584000,3422000]},
            {"pixel":[100,0],"world":[20584100,3422000]},
            {"pixel":[0,100],"world":[20584000,3421900]}]
    result=service.prepare_georeference("scan",LOCAL,points,"fit",
        checkpoints=[{"pixel":[80,80],"world":[20584080,3421920]}])
    assert result["accepted"]
    assert result["crs"]==LOCAL
    assert result["earth_crs_known"] is False
    bridge=Bridge(tmp_path)
    receipt=prepare_creation(bridge,collection([Point(20584000,3422000)]),
        {"name":"local.wt","crs":LOCAL,"map_scale":50000},"local")
    output=bridge.apply("local")["output"]
    read=Reader(output)
    assert read.crs in (None,"")
    assert read.geom[0].x==pytest.approx(20584000)
    assert receipt["crs"]==LOCAL
    blob=Path(output).read_bytes()
    assert blob[91:93]==bytes([0,0])  # Native user drawing frame, not geographic degrees.
    assert struct.unpack_from('<d',blob,143)[0]==0

def test_local_frame_cannot_silently_enter_earth_projection_or_metric_buffer(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    template={"name":"local.wp","crs":LOCAL,"map_scale":50000}
    features=collection([Point(1,2)])
    with pytest.raises(ValueError,match="EARTH_CRS_REQUIRED"):
        prepare_vector_operation(Bridge(tmp_path),"projection.transform",features,template,
                                 {"source_crs":LOCAL},"bad")
    with pytest.raises(ValueError,match="KNOWN_DISTANCE_UNIT_REQUIRED"):
        prepare_vector_operation(Bridge(tmp_path),"geometry.buffer",features,template,{"distance":5},"bad2")
