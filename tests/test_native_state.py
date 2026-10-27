import io
from pathlib import Path

import geopandas as gpd
import pytest
from pymapgis import Writer
from pymapgis import reader as native
from shapely.geometry import Point, LineString

from mapgis_agent.core import Bridge, native_ids, read_native, sha


@pytest.mark.parametrize('suffix,geometries', [
    ('.WT', [Point(0, 0), Point(10, 0), Point(20, 0)]),
    ('.WL', [LineString([(x, 0), (x + 1, 1)]) for x in (0, 10, 20)]),
])
def test_deleted_slots_keep_native_physical_identity(tmp_path, suffix, geometries):
    source = tmp_path / ('original' + suffix)
    Writer(gpd.GeoDataFrame({'ID': [9, 9, 9], '名称': ['甲', '乙', '丙']}, geometry=geometries)).to_file(source)
    blob, reader, entries = read_native(source)
    changed = bytearray(blob)
    # Physical slot 2 is deleted by native format flags; slot 3 must stay 3.
    size = 93 if suffix == '.WT' else 57
    changed[entries[0][0] + 2 * size] = 0
    stream = io.BytesIO(blob)
    native._read_attribute_header(stream, entries[native._ATTR_HEADER_INDEX[reader.shapeType]][0])
    _, length, _ = native._read_attribute_header(io.BytesIO(blob), entries[native._ATTR_HEADER_INDEX[reader.shapeType]][0])
    changed[stream.tell() + 2 * length] = 0
    source.write_bytes(changed)
    _, parsed, _ = read_native(source)
    assert native_ids(parsed) == [1, 3]
    assert [g.wkt for g in parsed.geom] == [geometries[0].wkt, geometries[2].wkt]
    bridge = Bridge(tmp_path / 'project')
    work = Path(bridge.copy(str(source), 'copy')['path'])
    assert [row['record_id'] for row in bridge.inspect(str(work))['rows']] == [1, 3]
    bridge.prepare(str(work), sha(work.read_bytes()), [3], 'edit-third', attributes={'名称': '丁'})
    _, result, _ = read_native(Path(bridge.apply('edit-third')['output']))
    assert result.data['名称'].tolist() == ['甲', '丁']
