import asyncio

import pytest
from pymapgis import Reader
from shapely.geometry import mapping, Polygon, LineString, Point

from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.server import build_server


CRS = {"definition":"+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs", "axis_order":"xy"}


def collection(geometries, ids=None):
    return {"type":"FeatureCollection", "features":[
        {"type":"Feature","id":f"logical-{i}", "geometry":mapping(g),
         "properties":{"ID":(ids or list(range(len(geometries))))[i],"类别":"界线"}}
        for i,g in enumerate(geometries)]}


def test_creation_uses_existing_request_ledger_and_never_overwrites_inputs(tmp_path):
    names = {t.name for t in asyncio.run(build_server(tmp_path).list_tools())}
    assert "mapgis_prepare_creation" in names
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    features = collection([LineString([(500000,3500000),(500100,3500100)])])
    template = {"crs":CRS,"map_scale":50000,"name":"boundaries.wl","style":{"linetype":1,"color":2}}
    preview = prepare_creation(bridge, features, template, "new-lines")
    assert preview["kind"] == "CREATE" and preview["state"] == "PREPARED"
    assert not (tmp_path / "deliverables/creation/new-lines/boundaries.wl").exists()
    applied = bridge.apply("new-lines")
    assert applied["state"] == "APPLIED"
    assert bridge.status("new-lines")["output_matches"]
    assert bridge.apply("new-lines")["reconciled_existing_output"]
    read = Reader(applied["output"], read_graphic_params=True)
    assert read.geom[0].hausdorff_distance(features_geometry(features,0)) < 1e-6
    assert read.line_params.color.tolist() == [2]
    assert preview["feature_mapping"] == [{"feature_id":"logical-0","physical_record_id":1,"attribute_ID":0}]
    changed = collection([LineString([(0,0),(1,1)])])
    with pytest.raises(BridgeError, match="REQUEST_ID_CONFLICT"):
        prepare_creation(bridge, changed, template, "new-lines")


def features_geometry(features, index):
    from shapely.geometry import shape
    return shape(features["features"][index]["geometry"])


def test_measurement_column_roundtrips_mixed_integer_and_decimal_values(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    features = collection([Point(1, 2), Point(3, 4)])
    for feature, value in zip(features["features"], [2, 2.5]):
        feature["properties"]["W"] = value
    preview = prepare_creation(bridge, features,
        {"name": "samples.wt", "crs": CRS, "map_scale": 1000}, "mixed-measurements")
    assert preview["state"] == "PREPARED"
    reader = Reader(bridge.apply("mixed-measurements")["output"])
    assert reader.data["W"].tolist() == [2.0, 2.5]


def test_shared_boundaries_and_holes_roundtrip(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    left = Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(4,2),(4,4),(2,4)]])
    right = Polygon([(10,0),(20,0),(20,10),(10,10)])
    result = prepare_creation(bridge, collection([left,right],[9,9]),
        {"crs":CRS,"map_scale":1000,"name":"areas.wp","style":{"fill_color":3}}, "areas")
    parsed = Reader(bridge.apply("areas")["output"],make_valid=False,read_graphic_params=True)
    assert [g.area for g in parsed.geom] == pytest.approx([96,100])
    assert parsed.geom[0].intersection(parsed.geom[1]).area == 0
    assert parsed.geom[0].boundary.intersection(parsed.geom[1].boundary).length == pytest.approx(10)
    assert result["feature_mapping"][0]["attribute_ID"] == result["feature_mapping"][1]["attribute_ID"] == 9
    assert [x["physical_record_id"] for x in result["feature_mapping"]] == [1,2]


def test_new_files_do_not_overwrite_inputs_or_accept_unsafe_names(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    original = tmp_path / "original.wt"
    original.write_bytes(b"keep")
    for name in (str(original), "../escape.wt", "file.txt"):
        with pytest.raises(BridgeError,match="NAME|EXTENSION"):
            prepare_creation(bridge,collection([Point(1,2)]),{"name":name,"crs":CRS,"map_scale":1000},"bad")
    assert original.read_bytes() == b"keep"


def test_duplicate_logical_identity_and_invalid_geometry_are_rejected(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    features = collection([Point(1,2),Point(3,4)])
    features["features"][1]["id"] = "logical-0"
    with pytest.raises(BridgeError,match="FEATURE_ID"):
        prepare_creation(bridge,features,{"name":"points.wt","crs":CRS,"map_scale":1000},"bad")
    invalid = collection([Polygon([(0,0),(10,10),(0,10),(10,0),(0,0)])])
    with pytest.raises(BridgeError,match="INVALID_GEOMETRY"):
        prepare_creation(bridge,invalid,{"name":"bad.wp","crs":CRS,"map_scale":1000},"bad2")


def test_projection_not_representable_in_6x_is_not_silently_changed(tmp_path):
    from mapgis_agent.creation import prepare_creation
    with pytest.raises(BridgeError, match="UNREPRESENTABLE_NATIVE_CRS"):
        prepare_creation(Bridge(tmp_path),collection([Point(500000,3500000)]),
            {"name":"utm.wt","crs":{"definition":"EPSG:32650","axis_order":"xy"},"map_scale":50000}, "utm")


def test_creation_rejects_silent_ellipsoid_substitution_before_ledger(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge = Bridge(tmp_path)
    with pytest.raises(BridgeError, match="UNSUPPORTED_NATIVE_CRS_ENCODING"):
        prepare_creation(bridge, collection([Point(500000, 3000000)]),
            {"name":"cgcs.wt", "crs":{"definition":"EPSG:4549", "axis_order":"xy"},
             "map_scale":1000}, "cgcs-no-substitution")
    with bridge.connect() as db:
        assert db.execute("SELECT * FROM commands WHERE id=?", ("cgcs-no-substitution",)).fetchone() is None
    assert not (tmp_path / "deliverables/creation/cgcs-no-substitution/cgcs.wt").exists()


def test_symbol_point_kind_and_rotation_survive_native_roundtrip(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge=Bridge(tmp_path)
    result=prepare_creation(bridge,collection([Point(500000,3500000)]),
        {"name":"attitude.wt","crs":CRS,"map_scale":50000,
         "style":{"symbol_no":1,"angle":37.5,"height":2,"width":2}},"symbol")
    reader=Reader(bridge.apply('symbol')['output'],read_graphic_params=True)
    assert reader.point_params.point_type.tolist()==[1]
    assert reader.point_params.symbol_no.tolist()==[1]
    assert reader.point_params.angle.tolist()==[37.5]


def test_new_symbol_boxes_cover_native_drawing_extent_and_file_union(tmp_path):
    import struct
    from pathlib import Path
    from mapgis_agent.core import read_native
    from mapgis_agent.creation import prepare_creation
    bridge=Bridge(tmp_path)
    local={'kind':'local','name':'native drawing','axis_order':'xy','unit':'unknown',
           'reference_sha256':'a'*64}
    points=collection([Point(20585000,3421000),Point(20584000,3419000)])
    prepare_creation(bridge,points,{'name':'symbols.wt','crs':local,'map_scale':50000,
        'style':{'symbol_no':252,'height':200,'width':200,'angle':45}},'boxes')
    blob,reader,sections=read_native(Path(bridge.apply('boxes')['output']))
    boxes=[struct.unpack_from('<4d',blob,sections[3][0]+i*32) for i in range(3)]
    for g,box in zip(reader.geom,boxes[1:]):
        # A 200x200 rotated symbol must not be culled by a 0.26-unit box.
        assert box[0] <= g.x-200 and box[2] >= g.x+200
        assert box[1] <= g.y-200 and box[3] >= g.y+200
    assert boxes[0]==(min(b[0] for b in boxes[1:]),min(b[1] for b in boxes[1:]),
                      max(b[2] for b in boxes[1:]),max(b[3] for b in boxes[1:]))
    assert struct.unpack_from('<4d',blob,304)==boxes[0]


def test_new_annotation_pool_preserves_gbk_markup_and_symbol_rows(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge=Bridge(tmp_path)
    features=collection([Point(500000,3500000),Point(500050,3500000)])
    features['features'][0]['text']='地层 T#-2#=d#+1#='
    features['features'][0]['style']={'height':2,'width':2,'angle':10}
    features['features'][1]['style']={'symbol_no':1,'angle':45,'height':2,'width':2}
    prepare_creation(bridge,features,{'name':'labels.wt','crs':CRS,'map_scale':50000},'labels')
    output=bridge.apply('labels')['output']
    rows=bridge.inspect(output)['rows']
    assert rows[0]['text']=='地层 T#-2#=d#+1#='
    assert rows[1]['text'] is None
    reader=Reader(output,read_graphic_params=True)
    assert reader.point_params.point_type.tolist()==[0,1]


@pytest.mark.parametrize('style',[{'color':256},{'color':1.5},{'layer':-1},{'angle':True}])
def test_creation_rejects_native_parameter_wrapping_or_coercion(tmp_path,style):
    from mapgis_agent.creation import prepare_creation
    with pytest.raises(BridgeError,match='STYLE'):
        prepare_creation(Bridge(tmp_path),collection([Point(500000,3500000)]),
            {'name':'points.wt','crs':CRS,'map_scale':50000,'style':style},'bad-style')


def test_annotation_font_bytes_are_not_advertised_as_symbol_pen_width(tmp_path):
    from mapgis_agent.creation import prepare_creation
    features=collection([Point(500000,3500000)]);features['features'][0]['text']='annotation'
    with pytest.raises(BridgeError,match='STYLE'):
        prepare_creation(Bridge(tmp_path),features,
            {'name':'points.wt','crs':CRS,'map_scale':50000,'style':{'pen_width':2.5}},'reserved-point')


def test_symbol_pen_width_uses_its_own_float_field(tmp_path):
    from mapgis_agent.creation import prepare_creation
    bridge=Bridge(tmp_path)
    prepare_creation(bridge,collection([Point(500000,3500000)]),
        {'name':'points.wt','crs':CRS,'map_scale':50000,'style':{'pen_width':2.5}},'symbol-pen')
    actual=Reader(bridge.apply('symbol-pen')['output'],read_graphic_params=True)
    assert actual.point_params['point_type'].tolist()==[1] and actual.point_params['pen_width'].tolist()==[2.5]


def test_creation_does_not_silently_drop_z_coordinates(tmp_path):
    from mapgis_agent.creation import prepare_creation
    with pytest.raises(BridgeError,match='2D'):
        prepare_creation(Bridge(tmp_path),collection([Point(500000,3500000,15)]),
            {'name':'points.wt','crs':CRS,'map_scale':50000},'z-point')


def test_new_polygon_has_native_region_walks_for_shared_arcs_and_holes(tmp_path):
    """The MapGIS renderer uses these lists; Reader reconstructs without them."""
    import struct
    from mapgis_agent.core import read_native
    from mapgis_agent.creation import prepare_creation
    bridge=Bridge(tmp_path)
    left=Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(4,2),(4,4),(2,4)]])
    right=Polygon([(10,0),(20,0),(20,10),(10,10)])
    prepare_creation(bridge,collection([left,right]),
        {'name':'walks.wp','crs':CRS,'map_scale':1000,'style':{'fill_color':3}},'walks')
    blob,reader,sections=read_native(__import__('pathlib').Path(bridge.apply('walks')['output']))
    walks=[]
    for pid in (1,2):
        count,offset=struct.unpack_from('<2i',blob,sections[8][0]+pid*40+1)
        assert count>1, 'Native region arc list is missing'
        values=struct.unpack_from(f'<{count}i',blob,sections[1][0]+offset)
        rings=[[]]
        point_count=0
        for signed in values[1:]:
            if signed==0:
                rings.append([])
                continue
            n,position=struct.unpack_from('<2i',blob,sections[0][0]+abs(signed)*57+10)
            points=[struct.unpack_from('<2d',blob,sections[1][0]+position+j*16) for j in range(n)]
            point_count+=n
            if signed<0:
                points.reverse()
            if rings[-1]:
                assert rings[-1][-1]==points[0]
            rings[-1].extend(points if not rings[-1] else points[1:])
        assert values[0]==point_count+len(rings)
        assert len(rings)==(2 if pid==1 else 1)
        from shapely.geometry import LinearRing
        assert all(r[0]==r[-1] and not LinearRing(r).is_ccw for r in rings)
        walks.append(values)
    assert {abs(a) for a in walks[0][1:] if a}&{abs(a) for a in walks[1][1:] if a}
    assert reader.polygon_params.fill_color.tolist()==[3,3]


@pytest.mark.parametrize('field',['class','field5','field6','field7'])
def test_region_index_bytes_are_not_accepted_as_user_style(tmp_path,field):
    from mapgis_agent.creation import prepare_creation
    with pytest.raises(BridgeError,match='STYLE'):
        prepare_creation(Bridge(tmp_path),collection([Polygon([(0,0),(1,0),(1,1),(0,1)])]),
            {'name':'bad.wp','crs':CRS,'map_scale':1000,'style':{field:1}},'bad-region-style')
