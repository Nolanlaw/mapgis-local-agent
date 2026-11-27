from __future__ import annotations

if __name__ == '__main__':
    # The module entry, source launcher and installed command share the fd guard.
    from . import _mcp_main

    _mcp_main()
    raise SystemExit

import os
# Each idle Windows BLAS pool otherwise commits about 0.6 GiB; NumPy and SciPy
# each own one. Keep this tool process small and respect explicit caller choices.
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
import argparse
from functools import cache
from importlib.util import find_spec
from ipaddress import IPv6Address, ip_address
from pathlib import Path
import re
from typing import Annotated, Collection, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictInt, StrictFloat, StrictBool

from .core import Bridge, BridgeError
from .deletion import prepare_deletion
from .environment import capabilities
from .native_state import live_state
from .operations import add_operation_schema, find_operations, describe_operation
from .native_help import search_native_help
from .raster import RasterWorkspace, write_georeferenced_window
from .creation import prepare_creation
from .vector import prepare_vector_operation
from .features import read_features, prepare_file_operation
from .comparison import compare_native_files
from .native_backend import native_file_info, prepare_native_operation, reconcile_native_request
from .workspace import NativeWorkspaces
from .exchange import query_attributes,prepare_export,prepare_import,vector_source_info
from .terrain import analyze_terrain,prepare_terrain
from .network import analyze_network,prepare_network
from .native_raster import prepare_native_raster,prepare_raster_warp,raster_source_info
from .artifacts import reconcile_bundle
from .native_project import native_project_info,prepare_native_project,prepare_project_plot,prepare_project_edit
from .cartography import projection_points,prepare_map_frame
from .derived import prepare_annotations,prepare_seams,prepare_attribute_report
from .attributes import prepare_attribute_schema
from .multimedia import MultimediaParameters, prepare_multimedia
from .sdk_catalog import sdk_describe
from .sdk_flow import prepare_sdk_flow, sdk_policy
from .private_control import private_control
from .topology import read_wp_arcs
from .symbol_library import system_library_info,prepare_system_library
from .project_workspace import ProjectWorkspaces
from .spatial import spatial_query,polygon_overlay
from .table_join import JoinTable,JoinParameters,prepare_table_join
from .database_sources import DatabaseSource
from .external_database import DatabaseJoinParameters,WritebackParameters,database_info,prepare_database_join,prepare_database_writeback
from .tin_flight import TinFlightParameters
from .scene_lines import LineOverlayInput
from .scene_annotations import AnnotationOverlayInput
from .scene_regions import RegionOverlayInput
from .scene_flight import FlightParameters
from .spatial_summary import SpatialSource,SummaryParameters,prepare_spatial_summary
from .raster_processing import ImageParameters,prepare_raster_processing
from .raster_classification import ClassificationParameters,prepare_raster_classification
from .raster_crop import prepare_raster_crop
from .raster_vectorization import RasterVectorParameters,prepare_raster_vectorization
from .raster_fusion import FusionParameters,prepare_raster_fusion
from .geochemistry_statistics import BackgroundParameters,prepare_statistics
from .assay_qa import AssayQAParameters,prepare_assay_qa
from .geochemistry_combination import ElementAnomaly,CombinationParameters,prepare_combination
from .sample_numbering import SoilGridParameters,SedimentNumberingParameters,prepare_soil_grid,prepare_sediment_numbering
from .raster_processing import RasterReference
from .native_msd import MsiSource,MsdParameters,msd_source_info,native_msd_info,prepare_native_msd,prepare_msd_edit
from pydantic import StrictInt
from .cad_exchange import DxfSource,DxfExportParameters,DxfImportParameters,dxf_source_info,prepare_dxf_export,prepare_dxf_import
from .native_com import prepare_native_subset
from .native_project_parameters import native_project_parameters_info,prepare_project_parameters
from .native_legend import native_legend_info,prepare_native_legend
from .volume import (VolumeImportParameters,VolumeGridParameters,VolumeSliceParameters,
    volume_source_info,read_volume_samples,prepare_volume_import,prepare_volume_grid,prepare_volume_slice,reconcile_volume)
from .volume_plot import VolumePlotParameters,prepare_volume_plot,reconcile_volume_plot


def _requires_opencv(*args, **kwargs):
    raise BridgeError('PROVIDER_UNAVAILABLE: this operation requires OpenCV from the full or scan-cv profile')


if find_spec('cv2') is not None:
    from .candidates import extract_candidates, merge_candidate_tiles
    from .grid_analysis import SurfaceParameters, prepare_grid_analysis
    from .hydrology import HydrologyParameters, prepare_hydrology_isolated
    from .native_plot import prepare_native_plot
    from .native_scene import SceneParameters, prepare_native_scene
    from .scene_texture import TextureInput
    from .seed_following import SeedTraceParameters, seed_trace
    from .tin_edit import TinEditParameters
    from .tin_lines import TinLineOverlayInput
    from .tin_profile import ProfileParameters
    from .tin_scene import TinSceneParameters, prepare_native_tin_scene
    from .visibility import ViewshedParameters, LineOfSightParameters, prepare_viewshed, line_of_sight
else:
    # Core shares retain tool names and discovery. These modules import cv2,
    # directly or through grid_analysis, so only the full share has their schemas.
    SurfaceParameters = HydrologyParameters = SceneParameters = TextureInput = dict
    SeedTraceParameters = ProfileParameters = TinSceneParameters = dict
    TinEditParameters = TinLineOverlayInput = dict
    ViewshedParameters = LineOfSightParameters = dict
    extract_candidates = merge_candidate_tiles = prepare_grid_analysis = _requires_opencv
    prepare_hydrology_isolated = prepare_native_plot = prepare_native_scene = _requires_opencv
    seed_trace = prepare_native_tin_scene = prepare_viewshed = line_of_sight = _requires_opencv


def _register_legacy_tools(server, bridge, raster, workspaces, projects):
    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_volume_source_info(file:str,expected_sha256:str,native_reopen:StrictBool=False,statistics_policy:Literal['strict','report_discrepancies']='strict',encoding_policy:Literal['linear_only','stored','physical']='linear_only')->dict:
        """Inspect MapGIS6.7 count-first XYZV .3bn or linear-axis 3DEM .3dm. Checks dimensions, directory/data offsets, finite scalar values and layer statistics before native access. Returns [nx,ny,nz], XYZ bounds, physical zyx order and hash-bound frame when present; absent CRS/Z direction/value units remain unknown. native_reopen requests a fresh original analysis-program header read on a task copy. It checks all layer dimensions/ranges/means; it is not a claim that every scalar was read through a native numeric API. Sources stay unchanged. Statistics policy defaults strict. Explicit report_discrepancies is3DM-only: keep every finite saved voxel, report saved global/per-layer statistics separately from independently recomputed values, and retain strict byte/dimension/axis/finite guards. Original application header readback verifies declared headers, not voxel-statistic agreement; discrepancy status stays explicit. Source coordinate/value units and CRS remain unspecified without bound metadata. Encoding defaults linear_only, preserving prior strict behaviour. Explicit encoding_policy=stored permits exact saved jointlog10Z/value flag1,1 data; physical individually reconstructs float64 10**savedfloat32logZ/value and recomputes physical statistics, with stored metrics separate. Log0 represents physical1, not missing. Overflow/positive-underflow and unverified mixedflags fail. Unknown CRS/units and prelog quantization remain explicit. This is a read-only policy; production grid/slice/plot still reject log profiles unless an explicit separate transformation is implemented. Native readback verifies stored headers only."""
        return volume_source_info(bridge.root,file,expected_sha256,native_reopen,statistics_policy,encoding_policy)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_read_volume(file:str,expected_sha256:str,offset:Annotated[StrictInt,Field(ge=0)]=0,limit:Annotated[StrictInt,Field(ge=1,le=1000)]=1000,statistics_policy:Literal['strict','report_discrepancies']='strict',encoding_policy:Literal['linear_only','stored','physical']='linear_only')->dict:
        """Read a bounded physical window of XYZV observations or 3DEM nodes. Returns exact saved XYZ/value, zero-based flat index and model ijk indices; model order is x-fastest, then y, then z. Finite zero remains valid. No interpolation, coordinate inference or source mutation. Use volume_source_info for file/frame/native-readback information. Statistics policy defaults strict. Explicit report_discrepancies is3DM-only: keep every finite saved voxel, report saved global/per-layer statistics separately from independently recomputed values, and retain strict byte/dimension/axis/finite guards. Original application header readback verifies declared headers, not voxel-statistic agreement; discrepancy status stays explicit. Source coordinate/value units and CRS remain unspecified without bound metadata. Encoding defaults linear_only, preserving prior strict behaviour. Explicit encoding_policy=stored permits exact saved jointlog10Z/value flag1,1 data; physical individually reconstructs float64 10**savedfloat32logZ/value and recomputes physical statistics, with stored metrics separate. Log0 represents physical1, not missing. Overflow/positive-underflow and unverified mixedflags fail. Unknown CRS/units and prelog quantization remain explicit. This is a read-only policy; production grid/slice/plot still reject log profiles unless an explicit separate transformation is implemented. Native readback verifies stored headers only."""
        return read_volume_samples(file,expected_sha256,offset,limit,statistics_policy,encoding_policy)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_volume_import(file:str,expected_sha256:str,parameters:VolumeImportParameters,template:dict,request_id:str)->dict:
        """Prepare a NEW MapGIS6.7 XYZV 3BN from ASCII/UTF8 DAT: n followed by exactly n x y z value records. parameters requires frame={crs:explicit projected/local metre XY,z_axis:elevation_up|depth_down,value_name,value_unit}, max_coordinate_error_m and max_value_error for float32 storage. Preserves record order, finite zero and all four columns; rejects missing/nonfinite records and unapproved rounding. template={name:'samples.3bn'}. Compatible writer and independent saved-data read always run. Optional parameters.native_reopen=true additionally requires the project-approved original-program header route; default false records NOT_VERIFIED_FOR_THIS_OUTPUT. apply_patch publishes the guarded bundle; get_request/reconcile_artifact do not replay incomplete production. Original input remains unchanged."""
        return prepare_volume_import(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_volume_grid(file:str,expected_sha256:str,parameters:VolumeGridParameters,template:dict,request_id:str)->dict:
        """Prepare a NEW finite scalar 3D MapGIS3DEM .3dm from 3BN XYZV, with explicit bounds=[xmin,ymin,zmin,xmax,ymax,zmax], shape=[nx,ny,nz], power1..16, neighbors1..256, frame and float32 error budgets. nx/ny2..32767, nz2..256, <=2000000 nodes. idw_3d uses exact Euclidean nearest-neighbor IDW throughout the full box and requires vertical_extrapolation=reject (vertical clamp is inapplicable). borehole_linear_idw requires contiguous unique XY groups with >=2 increasing Z samples; interpolates each hole in Z then XY IDW; vertical_extrapolation=reject|clamp is explicit. Algorithms are assistant NumPy/SciPy implementations. Scalar zero is valid; no masked/log-axis profile. Compatible save and independent saved-voxel checks always run. Optional parameters.native_reopen=true requires the project-approved original application route and checks layer headers; default false records NOT_VERIFIED_FOR_THIS_OUTPUT. It is an XYZV field, with no inferred fault, geological solid or resource classification. template={name:'field.3dm'}. apply/get/reconcile use the existing guarded artifact ledger."""
        return prepare_volume_grid(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_volume_slice(file:str,expected_sha256:str,parameters:VolumeSliceParameters,template:dict,request_id:str)->dict:
        """Prepare an X/Y/Z scalar section of a finite saved 3DEM model. parameters={frame:matching explicit metre XYZ/value frame,axis:x|y|z,position_m,method:linear|nearest,max_value_error}. Requires an in-bounds world coordinate; nearest ties choose the lower coordinate and report the actual sampled position. template={name:'section.grd'}. Produces numeric Surfer GRD, full XYZV CSV and bound axis/CRS metadata. X fixed: columns=Y,rows=Z; Y fixed: columns=X,rows=Z; Z fixed: columns=X,rows=Y. Rows increase in the declared Z sense, including depth_down. NumPy interpolation is checked independently; the saved GRD is reopened in another original MapGIS DTM process and every cell is compared. CSV uses those saved float32 values. apply/get/reconcile use the artifact ledger; source volume remains unchanged."""
        return prepare_volume_slice(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_volume_plot(file:str,expected_sha256:str,parameters:VolumePlotParameters,template:dict,request_id:str)->dict:
        """Prepare a NEW original MapGIS colour3D view from a finite3DM.

        Requires the already approved native_volume_private_ui and legacy-host
        opt-ins. Only new owned original processes on a non-input private desktop
        operate on task copies, using normal automatic-palette/full3D/SaveAs
        commands. No existing app binding, keyboard/mouse, injection, installation
        or licence change. view=full|stereo_x|stereo_y|stereo_z|flat_x|flat_y|flat_z|
        block_1|block_2|block_3|scalar_height_z|multilayer. Scalar-height uses a Z plane and
        drawing height represents source scalar, not elevation/depth. Other
        parameters are frame,max_coordinate_error_m?,
        planes?:{x|y|z:{position_m,max_error_m}}. full uses no planes; stereo/flat
        requires exactly its axis; block_1 uses X/Z, block_2 uses Y/Z and
        block_3 uses X/Y/Z. No unused plane is accepted.
        multilayer requires layers:[{position_m,max_error_m},...] with2..100
        strictly increasing source-Z positions, no planes parameter, and distinct
        actual grid-node slices. It adds each verified position through the
        original Add Layer control and reads every list value before OK/drawing.
        The original app sorts native Z upward; layer_draw_order reports that
        order separately from caller order, including depth_down reversal.
        All section modes require a full warm-up drawing before their commands enable.
        Its0.1m float32 slider and grid-node sampling can limit reachable planes.
        Tool chooses the closest reachable source-node plane and enforces the
        caller's explicit max_error_m; actual source position/index, control tick
        and displayed position are retained. No implicit plane interpolation.
        frame is the existing explicit metre XY/elevation_up|depth_down/value-name/
        unit contract. elevation_up is copied byte-for-byte. depth_down requires
        an explicit coordinate-error budget; a task copy reverses Z layers and
        maps native_z=-depth, retaining every scalar without interpolation.
        Original native Z labels are therefore negative depth, with the mapping
        saved beside the result. template={name:'view.wp'} produces editable
        sibling view.wt/view.wl/view.wp and a provenance sidecar. These are a
        native2D projection with an automatic numeric colour legend, not an
        interactive3D solid or source-map XY coordinates. Source frame is kept
        separate from the output drawing frame. Fresh native processes reopen
        all three layers; independent geometry/count validation precedes prepare.
        Some degenerate grids trigger a legacy contour loop; bounded failure
        ends only our Job and retains diagnostics/candidates. Completed SaveAs
        sets can be adopted after parent loss without rerunning original drawing.
        Use get_request/reconcile_artifact before retrying, then apply_patch.
        Compose a portable MPJ and actual EPS/PDF/TIFF through existing tools.
        """
        return prepare_volume_plot(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_native_legend_info(file:str,expected_sha256:str)->dict:
        """Read a saved CLN with the installed mapleg60 SDK in a private process, returning ordered point/line/region names, descriptions, signed class codes and supported native styles. The original CLN remains unchanged. This reads catalog items; CLN alone does not specify the generated page layout or the system symbol library. Use prepare_native_legend for a new edited catalog and editable layout. No mouse, registry changes or licence bypass."""
        return native_legend_info(bridge.root,file,expected_sha256)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_native_legend(items:list[dict],parameters:dict,request_id:str)->dict:
        """Create a NEW original MapGIS CLN plus editable WT/WL/WP through mapleg60 and the native SDK exporter. items=[{kind:'point'|'line'|'region',name:GBK <=90 bytes,description?:GBK <=127 bytes,code:signed32,style?:native fields}]. point style: symbol_no,height,width,angle,pen_width,color,layer; line: linetype,aux_linetype,color,width,x_coef,y_coef,aux_color,layer; region: fill_color,pattern,pattern_height,pattern_width,pattern_color,layer. Numeric parameters are finite, dimensions positive except line width/pen_width >=0. parameters={name:'new.cln',title:nonempty GBK <=127 bytes,bounds:[xmin,ymin,xmax,ymax],columns:1..20}; bounds are native drawing millimetres, map_scale=1, requiring an explicit transform before mixing with ground-coordinate layers. Names are drawn in point/line/region groups; descriptions remain CLN metadata. A bound legend JSON retains layout and output hashes; symbol/color numbers resolve in the chosen project system library. The original COM Add/Type adapters have observed defects, so this uses original exported C APIs and normal style conversion. Native save, fresh process reopen, real vector counts and exact labels are checked. apply/get/cancel/reconcile_artifact use the atomic ledger; completed native generation is not replayed after parent loss. No desktop input."""
        return prepare_native_legend(bridge,items,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_native_project_parameters_info(file:str,expected_sha256:str)->dict:
        """Read all 29 saved native MPJ MapParam fields through the installed SDK in a private process, on copied sibling dependencies. Returns original SDK field descriptions, exact enum codes, scalar values and native receipt. No source registry, GUI or layer mutation. Requires portable sibling references; clone an older project with prepare_project_edit first if needed. Angular fields follow raw native SDK encodings, including signed DDDMMSS.SSSS where documented, with no guessed conversion. The field catalog describes metadata, not verified coordinate transformations or an EPSG/ground-accuracy claim."""
        return native_project_parameters_info(bridge.root,file,expected_sha256)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_project_parameters(file:str,expected_sha256:str,changes:dict,template:dict,request_id:str)->dict:
        """Assign native MPJ map metadata into a NEW portable project, preserving every layer byte, state, display limit, native bounds, layout and sibling system library. Inspect native_project_parameters_info first for all names, encodings and enums. changes is a nonempty subset of the 29 fields; type/levelType/TICtype codes0..4, projection0..20, ellipsoid0..10, linear/angular units0..16. Other values are finite; rate/vRate/mapw/maph >=0; infoRatex/y initial values0 or1. template={name:'new.mpj'} is lowercase ASCII. No layer coordinates or layer CRS are transformed; geodetic reprojection uses the separate explicit projection tools. Normal SDK save is followed by a different SDK read process and independent mapprj60 relocation/reopen with the candidate directory hidden. apply/get/cancel/reconcile_artifact use the atomic ledger; completed native edits are not replayed after parent loss."""
        return prepare_project_parameters(bridge,file,expected_sha256,changes,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_native_subset(file:str,expected_sha256:str,record_ids:list[StrictInt],template:dict,request_id:str,transform:dict|None=None,target:dict|None=None)->dict:
        """Save selected active physical IDs from a WT/WL/WP to a NEW native file without desktop input. record_ids is ordered and unique; attribute ID may repeat. template={name:'selection.wt|wl|wp'} uses lowercase ASCII and the source extension. Installed 6.7 SDK CopyList copies native geometry/topology and returns an explicit source-to-output physical-ID map. Preserves primary attribute records byte-for-byte to avoid an observed legacy SDK attribute-reader incompatibility, then BAS60 saves and a fresh private process reopens. Checks each geometry, annotation, graphic style and the entire saved map-parameter block. WP holes/shared boundaries and known primary/arc/node attributes are copied with verified physical mappings; unknown nonempty auxiliary tables are rejected. Optional target={file,expected_sha256} appends into a saved same-kind target, producing a NEW merged file; every existing active target ID is retained and each new physical ID is returned. Both source hashes remain guarded through publication. Exact saved coordinate parameters and raw attribute schemas must agree; there is no implicit transform, snapping, deduplication or cross-file topology joining. Use transform separately before appending. Append handles annotation/symbol/circle/arc WT, WL, and verified WP layouts with known auxiliary tables, including deleted slots, retained unowned arcs and an empty active target. Existing target arc/node IDs and inactive walk offsets are retained or relocated explicitly; embedded resources and ambiguous coincident unowned arcs require further mapping. Selection IDs can come from workspace/project-workspace state, but no external GUI unsaved memory is mutated. Existing legacy-host opt-in and installed exact-build SDK required. apply/get/cancel/reconcile_artifact use the existing atomic ledger; source files are unchanged. Optional transform={translation:[dx,dy],scale:[sx,sy],rotation_degrees:angle,order:['scale','rotate','translate'],coordinate_frame:'source'} applies original SDK Transform to the copied selection, about coordinate origin0. Any permutation of these three operations is explicit; nonzero negative scales reflect. Translation is in saved source XY units and is converted to stored drawing coordinates. This is a planar graphical change, with original coordinate metadata retained; no CRS reprojection. All native graphic parameters/text are preserved (point chgParam=0). Expanded region attribute records are retained from source bytes after the legacy transform, followed by BAS60 save and independent geometry, topology, style, attribute and fresh-process checks."""
        return prepare_native_subset(bridge,file,expected_sha256,record_ids,template,request_id,transform,target)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_dxf_source_info(file:str,expected_sha256:str|None=None,layout:str='Model')->dict:
        """Inspect DXF modelspace with ezdxf1.4.5, returning exact file/witness members, header units, layers, entity counts, guarded native snapshots and unsupported types. Does not repair files, load external XREF/image resources or convert DWG. Pass the complete members map into prepare_dxf_import; paperspace layouts are listed but not imported."""
        return dxf_source_info(file,expected_sha256,layout)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_dxf_export(files:list[DxfSource],parameters:DxfExportParameters,request_id:str)->dict:
        """Create a new R2013 DXF from saved native WT/WL/WP. files=[{file,expected_sha256,layer?:unique CAD layer}]. parameters={name:'map.dxf',crs:explicit common projected/local native frame,units:metre|millimetre|centimetre|foot|inch|unitless,colors?:{'native color id':[R,G,B]},font?:'simsun.ttf'}. Known native CRS must match the declaration. Converts known linear units explicitly; unitless/known mixing fails. Native annotations become UTF-8 TEXT, points become POINT, lines LWPOLYLINE, polygon rings/holes solid HATCH. Exact coordinates/text are independently reopened before preparation. Attributes/styles/physical identities travel in a complete-DXF-hash-bound sidecar plus guarded original native layer snapshots, which retain opaque bytes. Actual CAD visual losses are reported: proprietary symbols, patterns, widths and native composite fonts/anchors are not reproduced by guessing. Default unmapped colors use CAD black/white. Atomic apply/get/cancel/reconcile_artifact; source files stay unchanged. This is DXF interchange, not DWG or native appearance equivalence."""
        return prepare_dxf_export(bridge,[v.model_dump() for v in files],parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_dxf_import(file:str,expected_sha256:str,expected_members:dict[str,str],parameters:DxfImportParameters,template:dict,request_id:str)->dict:
        """Create a new native WT/WL/WP from explicitly selected DXF root-entity layers in one named layout and one geometry kind. parameters={kind:point|line|polygon,layers:['CAD layer'],source_units:unit name matching the header,curve_tolerance?:positive source-unit flattening tolerance (analytic arc sagitta; bulge/HATCH Bezier conversion has a separate error),restore_bound_metadata?:true}. template={name:matching .wt/.wl/.wp,crs:explicit source coordinate frame,map_scale:positive}. Coordinates are scaled between known units, never reprojected by guessing. POINT/TEXT/MTEXT, LINE/2D polylines, ARC/CIRCLE/ELLIPSE/SPLINE with explicit approximation, and normal HATCH nested rings are supported; nonzero Z, mesh, unsupported selected entities or ambiguous unit contexts fail. Other supported geometry kinds in selected layers are counted and excluded explicitly. With an unchanged bound DXF and guarded snapshots, restore one complete original native layer byte-identically after DXF geometry/text checks, retaining original physical IDs/scale/styles/opaque fields. Source units/frame must match; changing scale/styles needs explicit standard conversion. A modified CAD drawing needs restore_bound_metadata=false, retains standard geometry/text plus CAD handle/layer/type, and reports visual limitations. Source/member hashes guard atomic publication. Native resave/reopen is a separate existing operation; no desktop input. New explicit parameters.layout selects an existing layout (default Model); expand_blocks=true recursively composes block base-point/insertion/scaling/rotation matrices on copied leaves and resolves every MINSERT row/column. Root layers select INSERTs; child layer0 inherits the insert layer, other child layers remain. Output mappings retain original root and leaf handles plus a stable virtual path. max_expanded_entities1..200000(default200000),max_block_depth1..32(default16). Paper drawings require paper_coordinate_units_confirmed=true for the declared source_units and an explicit separate local output frame; do not infer paper drawing units from plot setup or Earth coordinates from paper geometry. Curve tolerance is after block placement in source drawing units. Unsupported selected VIEWPORT/XCLIP/XREF, recursive graphs, attached/definition attribute text and nonuniform/sheared/mirrored block text fail explicitly. These scopes remain pending; unchanged default modelspace requests keep their old fingerprint/protocol2."""
        return prepare_dxf_import(bridge,file,expected_sha256,expected_members,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_seed_trace(scan_id:str,window_id:str,expected_window_sha256:str,parameters:SeedTraceParameters,decisions:list[str]|None=None,expected_graph_sha256:str|None=None,stop_after_segments:StrictInt|None=None)->dict:
        """Follow a scan centerline from a seed without desktop input, reusing the pinned MIT GeoScan thinning/graph core. Seed and bridge coordinates are WINDOW pixel centers (x+0.5,y+0.5). Require explicit RGB low/high triples or gray_threshold, role, optional Euclidean center-to-background min/max halfwidth_px and snap_radius_px. No automatic gap closing; bridges=[{start,end,reason}] can explicitly join degree-one endpoints within max_gap_px. exclude_regions=[{geometry:pixel Polygon/MultiPolygon,reason}] removes known text/protected areas. At crossings or an interior seed's two directions, return PAUSED_BRANCH and stable choice ids. Repeat with decisions=[id,...] and the matching expected_graph_sha256 to continue. stop_after_segments rewinds to an earlier segment prefix; adjust subsequent branch decisions to choose another route. This is a deterministic saved-image read; no native edit or geological interpretation occurs. Returns unsimplified source-pixel GeoJSON, or map coordinates only with a matching accepted georeference_id. Feed an explicitly accepted feature into prepare_creation. Bounded RGB window at most4194304pixels; 1000000skeleton pixels,2000segments,20000vertices; strokes ending at a window edge require another observation window."""
        return seed_trace(raster,scan_id,window_id,expected_window_sha256,parameters,decisions,expected_graph_sha256,stop_after_segments)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_msd_source_info(file:str,expected_sha256:str|None=None)->dict:
        """Inspect a saved native MSD 6.0 image collection index without opening it in CMsiDB. Returns ordered one-based image references and a complete members map keyed by resolved absolute path, including bound MSI/MSD .json witnesses. Missing images, unsupported layouts or changed witnesses fail explicitly. Native MSD does not encode a CRS identifier; known CRS witnesses remain separate. Pass source_sha256 and the complete members map into native_msd_info or prepare_msd_edit. Does not control a GUI or a multi-scale database."""
        return msd_source_info(file,expected_sha256)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_native_msd_info(file:str,expected_sha256:str,expected_members:dict[str,str],query_bounds:list[float]|None=None)->dict:
        """Reopen an MSD image collection through the installed exact-build CMsiDB in a private native process. Native close may save, so only an owned rebased copy is opened. Guards the original index, MSI files and bound witnesses before/after. Returns per-image native extents, their checked union and project_layers usable by prepare_native_project. Optional query_bounds=[xmin,ymin,xmax,ymax] selects one-based image positions by boundary-inclusive envelope intersection, not footprint or pixel content. Coordinates are unchanged native units; no CRS is guessed. Input membership hashes must be the complete msd_source_info members map."""
        return native_msd_info(bridge.root,file,expected_sha256,expected_members,query_bounds)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_native_msd(images:list[MsiSource],parameters:MsdParameters,request_id:str)->dict:
        """Create a NEW portable native MSD image collection through CMsiDB Create/Add/Save, followed by independent native reopen. images=[{file:MSI,expected_sha256,expected_witness_sha256?:hash of existing .msi.json}]; the witness hash is mandatory when a witness exists. Empty collections are supported. Each nonempty member needs at least three saved native GCPs. parameters={name:lowercase ASCII .msd filename,coordinate_frame:'native'} explicitly declares common physical coordinates; known differing CRS witnesses are rejected, unknown frames are not inferred. Publishes relative ASCII member names plus unchanged MSI/witness copies as one atomic bundle. apply/get/cancel/reconcile_artifact use the request ledger; no mouse. This covers saved image collections, not every remote database or scale switching module."""
        return prepare_native_msd(bridge,[v.model_dump() for v in images],parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_msd_edit(file:str,expected_sha256:str,expected_members:dict[str,str],remove_indexes:list[StrictInt],add_images:list[MsiSource],parameters:MsdParameters,request_id:str)->dict:
        """Branch a saved MSD with native positional deletion and image insertion. Pass the full msd_source_info members map. remove_indexes are unique existing ONE-based image positions; surviving members keep their order, additions append in supplied order. add_images and parameters follow prepare_native_msd. Returns old-to-new position mapping, checked native extents, save/reopen receipts and a portable atomic bundle. Removing all images is supported. The original index and all dependencies remain unchanged; source hashes guard publication. Agent buffers/unsaved external GUI state are separate. Parent-loss reconciliation adopts only an actually completed native child and never replays edits."""
        return prepare_msd_edit(bridge,file,expected_sha256,expected_members,remove_indexes,[v.model_dump() for v in add_images],parameters,request_id)

    @server.tool()
    def mapgis_spatial_query(left:dict,right:dict,parameters:dict,offset:int=0,limit:int=1000)->dict:
        """Query physical-ID pairs between two saved native files using GEOS STRtree. left/right={file,expected_sha256}. parameters={predicate:intersects|within|contains|covers|covered_by|contains_properly|touches|overlaps|crosses|equals|disjoint|dwithin|nearest,crs?:explicit local frame,include_attributes?:false,distance?:for dwithin,max_distance?:for nearest}. Predicates evaluate left geometry against right. Native Earth CRSs must match; unreferenced files require the same explicitly declared local frame. Distance operations require projected or known local linear units. nearest returns all ties in physical-ID order. Pages contain up to 1000 pairs; truncated total_pairs=null, next_offset continues deterministically. Attributes are nested by side so duplicate ID fields remain separate. Source hashes are checked before/after. To bind query results into project_workspace.select, pass layer index, returned physical IDs and expected_checkpoint_sha256=sources.left/right.sha256. No file mutation or global GUI selection."""
        return spatial_query(left,right,parameters,offset,limit)

    @server.tool()
    def mapgis_polygon_overlay(left:dict,right:dict,parameters:dict)->dict:
        """Overlay two saved WP datasets through GeoPandas/GEOS. left/right={file,expected_sha256}; parameters={operation:intersection|union|symmetric_difference|difference|identity,crs?:explicit local frame,fields?:[{side:left|right,field:source column,target:new column,missing?:explicit value for no owner,null_value?:explicit value for source null}],max_features?:100000}. Matching native Earth CRS or explicit shared local frame required. Returns valid polygon-family GeoJSON with stable output IDs and src_left/src_right physical IDs (0 explicitly means no owner). Only mapped attributes are copied; names/collisions/missing/null rules stay explicit. Graphic parameters come from the left owner, otherwise the right. No implicit geometry repair; boundary-only line/point contacts are outside this area operation. Empty results are valid. Feed a non-empty collection into prepare_creation with the same CRS/scale, then native resave/plot when required; native field types can be set through attribute_schema. Coordinates and reported areas are planar source units. Inputs stay unchanged."""
        return polygon_overlay(left,right,parameters)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_spatial_summary(left:SpatialSource,right:SpatialSource,parameters:SummaryParameters,request_id:str)->dict:
        """Spatial left join/zone statistics into a NEW native WT/WL/WP, preserving all physical IDs, original attributes and non-attribute bytes. left/right={file,expected_sha256}; predicate evaluates left geometry against right, e.g. covers for zones including boundary points or contains for strict interiors. Matching native CRS or explicit shared local frame required. match_policy='unique' rejects any right record belonging to multiple left records; 'all' explicitly counts it in each. columns=[{name,operation:count|sum|mean|min|max|median|stddev|single|concat_unique,field?:right attribute,type?:int|double|string,length?:GBK bytes,empty_value?:explicit fallback,ddof?:0|1}]. count without field counts all matched records (empty=0); a named field counts non-null values. Numeric fields must be finite; nulls default error, skip is explicit. stddev defaults sample ddof1, undersampled/empty groups require a typed empty_value. single requires exactly one match; concat_unique sorts distinct strings joined by semicolons and requires length. New field names cannot overwrite existing columns. All left records remain, including unmatched ones; source files are guarded through atomic publication. Bundle includes CSV and physical match/provenance sidecar. Exact native save/reopen is a separate native operation. At most 2000000 assignments/pairs; no desktop input. apply/get/cancel/reconcile_artifact use the ledger. For polygon area weights explicitly set weighting=intersection_area. Geographic CRSs are rejected; projected or declared local planar coordinates required. Boundary-only contacts have zero weight and do not count toward ownership. Additional operations: weighted_mean(field)=sum(value*overlap_area)/sum(overlap_area), removing skipped nulls from both numerator and denominator; prorated_sum(field)=sum(value*overlap_area/source_polygon_area), assuming uniform source density; coverage_area(no field)=union of overlaps, counting overlapping source coverage once. Weights/allocations remain per source feature; overlapping target zones each receive their own allocation. Coverage units are source coordinate units squared; no ground-accuracy claim. Empty weighted groups require typed empty_value; empty coverage is0. Sidecar records intersection areas, source fractions, excluded contacts and formula/units."""
        return prepare_spatial_summary(bridge,left.model_dump(),right.model_dump(),parameters,request_id)

    @server.tool()
    def mapgis_project_workspace(action:str,workspace_id:str,parameters:dict,expected_revision:int|None=None,request_id:str|None=None)->dict:
        """Own a native multi-layer MPJ context without mouse/GUI input. open={file:owned MPJ,expected_sha256}; status={}. Identity binds full path plus hash, so identical names/hashes in different folders remain separate. select={mode:replace|add|remove|intersect|xor,layers:[{index:one-based layer,record_ids:[physical IDs],expected_checkpoint_sha256?:query source hash}]}; empty replace clears selection. set_layer={index,state?:0..3,display_limits?:[min,max]} uses native drawing scales, with 0/0 unlimited. set_environment={translation?:[x,y],scale?:[sx,sy],rotation_degrees?:number,paper_mm?:[w,h]} retains unspecified header fields and participates in dirty/save/discard/recover. edit={index,operation,parameters,use_selection?:bool} reuses native buffer operations; editable/current layers require state 2/3. use_selection binds point/line updates to selected IDs; feature.delete uses empty parameters to delete that layer's selection. Arc/node operations use explicit topology IDs. feature.append takes parameters={file:incoming saved native file,expected_sha256:incoming hash,record_ids:incoming physical IDs}; it copies into the target layer checkpoint, retaining existing dirty edits and target identities. Source IDs are explicit, so use_selection does not apply. The owned native buffer is restarted on adoption; revision/request/dirty state commit together, recover never repeats completed CopyList. New entities follow appended drawing order. save={} publishes one portable MPJ set while retaining item settings, then binds clean buffers to verified members. close={} refuses dirty work; {discard:true} restores the last published project on recover={}. Mutations use expected_revision and stable request_id (close/recover require revision); pending edits/saves reconcile through recover without replay. Current checkpoints represent unsaved agent buffers, independently of an external editor. WT/WL/WP buffers and saved MSI backgrounds are supported; other native layer kinds remain explicit."""
        return projects.execute(action,workspace_id,parameters,expected_revision,request_id)

    @server.tool()
    def mapgis_prepare_project_plot(file:str,expected_sha256:str,view:dict,request_id:str,system_library:dict|None=None)->dict:
        """Render a portable saved MPJ's ordered vector and MSI background layers through native MapGIS into EPS, with an independent PNG preview. view={native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}. Project dependencies must be siblings (prepare_native_project makes such a bundle). Freezes project/member hashes, checks actual drawn layer counts and EPS page/visible output, then prepares an atomic bundle for apply/get/cancel. No mouse or external editor mutation."""
        return prepare_project_plot(bridge,file,expected_sha256,view,request_id,system_library)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_map_series(file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Prepare ordered native MPJ views as one multi-page PDF with editable portable MPJ/dependencies, per-page native EPS, PDF previews, JSON views and XLSX/CSV page indexes. parameters={pages:[{name:unique nonempty label<=128chars,view:{native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}}],dpi?:300,include_tiff?:false};1..100 explicit ordered pages, integer dpi72..600, optional TIFF at that dpi, <=100M pixels per page. Names label the index, not automatic page annotations. Existing original MapGIS project renderer draws each unchanged source project view; existing Ghostscript assembles EPS pages without JPEG filtering or image downsampling. Independently interpret every PDF page to verify count, order, physical size and marks against its native EPS; keep source hashes guarded through apply. Output is series.pdf plus sibling editable source, page-NNN.eps/png and optional tif, series.json/pages.xlsx/pages.csv. Views are recorded in the manifest, not a proprietary native multi-page layout object. No georeferencing is inferred from a printed page. Recovery adopts finished child plots/combined PDF, resumes only unissued views and can repeat format conversion without repeating completed native rendering. apply/get/cancel/reconcile_artifact use the atomic ledger. No desktop input."""
        from .map_series import prepare_map_series
        return prepare_map_series(bridge,file,expected_sha256,parameters,request_id)

    @server.tool()
    def mapgis_system_library_info(folder:str,expected_manifest_sha256:str|None=None,kind:str|None=None,ids:list[int]|None=None)->dict:
        """Read a saved MapGIS 6.x Slib folder without touching installed settings. Returns a fingerprint of every .lib member and native symbol/line/pattern counts. Optionally query kind=symbol|line|pattern|color and 1..100 distinct native ids. Glyphs use IDs starting at 1; color IDs start at 0 and return kcmy=[black,cyan,magenta,yellow], opaque spot/reserved bytes and record SHA. Color queries require the verified PCOLOR 3 Pcolor.lib. Font editing and arbitrary native arc authoring remain unsupported. Fingerprint is separate from library.json's file SHA."""
        return system_library_info(folder,expected_manifest_sha256,kind,ids)

    @server.tool()
    def mapgis_prepare_system_library(folder:str,expected_manifest_sha256:str,changes:list[dict],request_id:str)->dict:
        """Prepare a saved system-library copy; installed settings stay unchanged. changes:kind=symbol|pattern|line|color,action=append|replace (replace requires positive id). Symbol/pattern primitives: polyline|polygon|polygon_outline with parts:[[[x,y],...]], or arc|circle|circle_filled|circle_filled_outline with center:[x,y],radius>0,color?,width_index0..15. Arc requires0<=start_angle<end_angle<=360 degrees counterclockwise from east. Symbol circles use color for fill/outline; combine filled and outline primitives for different colors. Pattern filled circles also accept fill_color and centers0..1. Polygon rings close; coordinates/radii use normalized library units. Lines use dash_fraction:(0,1]. Colors use integer kcmy:[K,C,M,Y]0..255; append assigns next process-color ID below16384 with zero extras, replacement preserves extras; color0 reserved. Original payloads/trailer preserved. Unknown primitives, spot/font/full line-variant authoring unsupported. Apply/reconcile use atomic ledger. Bind returned folder and manifest_sha256 via system_library for isolated plots or portable MPJs."""
        return prepare_system_library(bridge,folder,expected_manifest_sha256,changes,request_id)

    @server.tool()
    def mapgis_projection_points(points:list[list[float]],source_crs:dict,target_crs:dict,allow_unknown_accuracy:bool=False)->dict:
        """Transform explicit XY pairs using PROJ with no ballpark operation. Require Earth CRS definitions and xy axes. Missing best grids fail; unknown accuracy across datums needs explicit opt-in. Returns actual operation/accuracy metadata, independently of native file CRS encoding limits."""
        return projection_points(points,source_crs,target_crs,allow_unknown_accuracy)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_borehole_trajectory(survey:Annotated[list[dict],Field(min_length=2,max_length=10000)],collar:dict,coordinate_frame:dict,
        query_mds:Annotated[list[StrictFloat],Field(max_length=100000)]|None=None,course_length:Annotated[StrictFloat,Field(gt=0)]=30.)->dict:
        """Compute drillhole positions using pinned unmodified wellpathpy0.5.2 minimum curvature and SciPy spherical direction interpolation. survey=[{md,inclination,azimuth}], MD starts0 and strictly increases, inclination degrees0<=inc<180 from vertical down, azimuth0<=azi<360 clockwise from grid north. collar={easting,northing,elevation}; coordinate_frame={crs:explicit projected metre CRS or referenced local metre grid,vertical_unit:'metre',vertical_datum:nonempty name,azimuth_reference:'grid_north',inclination_reference:'vertical_down'}. No implicit unit/datum/magnetic conversion. query_mds retains order/duplicates, cannot extrapolate; follows curved arcs rather than linear station chords. Reject near-antipodal directions because the path is not unique/stable. course_length in metres normalizes dogleg degrees, default30. Returns actual survey/query XYZ, MD/TVD, original-station resampling discrepancy and conventions; elevation is upward. These are model calculations from supplied survey, not observed intermediate positions or a native3D-file edit. Native editable output uses prepare_borehole_chart."""
        from .borehole import borehole_trajectory
        return borehole_trajectory(survey,collar,coordinate_frame,query_mds=query_mds,course_length=course_length)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_apparent_dip(dip_direction:Annotated[StrictFloat,Field(ge=0,lt=360)],dip:Annotated[StrictFloat,Field(ge=0,le=90)],
        section_azimuth:Annotated[StrictFloat,Field(ge=0,lt=360)])->dict:
        """Project an explicitly supplied true dip into an oriented vertical section. All directions are degrees clockwise from the same grid north. dip_direction is down-dip direction, not strike. Signed apparent dip is positive downward along increasing section distance. Horizontal bedding returns0; vertical bedding parallel/coincident with a vertical section has no unique intersection and is explicitly rejected. No magnetic conversion, inferred attitude or geological correlation."""
        from .borehole import apparent_dip
        return apparent_dip(dip_direction,dip,section_azimuth)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_read_borehole_workbook(file:str,expected_sha256:str,tables:dict,coordinate_frame:dict,sort_by_depth:StrictBool=False)->dict:
        """Read hash-bound XLSX collar, survey and interval tables using existing table reader. tables has collars/surveys/intervals, each={sheet:exact name,columns:{model_field:exact workbook header}}. Collar fields hole_id/easting/northing/elevation; survey hole_id/md/inclination/azimuth; interval hole_id/top/base/unit with optional description. Header is row1; identifiers must be exact strings, values numeric literal measurements, no unverified formula caches. coordinate_frame follows borehole_trajectory's explicit metre/grid-north/down convention. sort_by_depth must explicitly be true to sort stations and interval tops; original worksheet row lineage retained. Reject duplicate collars, unknown IDs, overlapping/out-of-range intervals; unlogged gaps stay missing. At most100 collars per batch. Source remains unchanged. For chart generation pass {workbook:{file,expected_sha256,tables},coordinate_frame,sort_by_depth} so the original workbook is re-read and guarded through publication."""
        from .borehole_tables import read_borehole_workbook
        return read_borehole_workbook(file,expected_sha256,tables,coordinate_frame,sort_by_depth=sort_by_depth)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_borehole_chart(data:dict,parameters:dict,template:dict,request_id:str)->dict:
        """Prepare editable native WT labels/station markers, WL axes/traces, WP interval blocks/ribbons plus source/result XLSX and bound JSON. data={coordinate_frame,holes:[{id,collar,survey,intervals:[{top,base,unit,description?}]}]} or {workbook:{file,expected_sha256,tables},coordinate_frame,sort_by_depth?:false}; workbook mappings follow read_borehole_workbook and are re-read. template={name:lowercase ASCII '.wp' basename}, sibling suffixes contain the other outputs. parameters={mode:'columns'|'section',title,vertical_scale:positive denominator,column_width_mm:1..100 graphic track width,font_mm:1..12,tick_interval_metres:positive,lithology_styles:{unit:native region style},font_zh?:1,font_en?:0}. columns adds column_spacing_mm large enough for separate labels, plots measured depth downward with common scale; horizontal spacing is layout only. section adds horizontal_scale,reference_elevation,sample_step_md,section:{start:[E,N],end:[E,N],max_crossline_distance:nonnegative}; projects minimum-curvature samples onto that declared straight vertical plane, elevation upward, explicit VE=horizontal_scale/vertical_scale, rejects sampled offsets beyond tolerance. Track/ribbon width is graphic, not drilled diameter. Source survey stations have separate native point markers; model curves are interpolation, no cross-hole stratigraphic connections. Unlogged intervals remain blank and labelled, overlaps are rejected; explicit unit/style mapping, black text. Native draw plane uses millimetres and retains original XYZ/MD/CRS/datum in result tables/JSON. Native source encoder plus original BAS60 save/fresh reopen verifies all three files. apply/get/cancel/reconcile_artifact use the atomic ledger; recovery adopts existing native results and starts only not-yet-issued stages, never replays a completed native edit. Existing authorized isolated legacy host required. No desktop input or native3D-file claim."""
        from .borehole_charts import prepare_borehole_chart
        return prepare_borehole_chart(bridge,data,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_map_sheets(action:Literal['index','decode','cover'],parameters:dict,geographic_crs:dict)->dict:
        """Calculate GB/T13989-2012 geographic sheet numbers/bounds, without writing files or selecting a datum. Explicit2D geographic CRS with degree units and longitude/latitude xy axes required. Domestic unmerged envelope72<=longitude<138,0<=latitude<56 only; no high-latitude merged sheets, global hemisphere prefixes, old numbering or rectangular engineering scheme. action=index parameters={points:[[lon,lat],...],scale:'1m'|'500k'|'250k'|'100k'|'50k'|'25k'|'10k'|'5k'|'2k'|'1k'|'500'}, up to5000 points; scale denominator aliases accepted. decode parameters={codes:[...]}, at most5000, validates scale/row/column/domain.1:2000 uses576 rows and columns,1:1000/500 use four-digit row/column. Point cells include south/west and exclude north/east; southwest corner returns its own sheet. cover parameters={geometry:valid2D Polygon|MultiPolygon GeoJSON,scale,include_boundary_touch?:false}, at most100000 vertices and5000 bounding candidates; holes retained, default positive intersection area, explicit true includes boundary-only touches. Returns geographic_bounds and map_scale for composing with mapgis_prepare_map_frame border/grid/labels and normal native save/project/output. No coordinate conversion or surveyed accuracy is inferred. Uses a pinned MIT ovobj-converter kernel with corrected2k table, strict code validation and exact canonical-float boundary comparisons; license/patch retained."""
        from .map_sheets import map_sheets
        return map_sheets(action,parameters,geographic_crs)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_fit_control_transform(model:Literal['translation2d','similarity2d','affine2d','translation3d','similarity3d'],controls:Annotated[list[dict],Field(min_length=1,max_length=1000)],coordinate_frame:dict,max_fit_error:Annotated[StrictFloat,Field(gt=0)],
        checkpoints:Annotated[list[dict],Field(max_length=1000)]|None=None,max_checkpoint_error:Annotated[StrictFloat,Field(gt=0)]|None=None,points:Annotated[list[list[StrictFloat]],Field(max_length=100000)]|None=None,direction:Literal['forward','inverse']='forward')->dict:
        """Fit explicit paired Cartesian controls, without writing files. model=translation2d(2 parameters), similarity2d(4), affine2d(6), translation3d(3), similarity3d(exact7DOF). controls=[{source:[x,y],target:[x,y]}] or explicit XYZ triples; minimum counts1/2/3/1/3, max1000. coordinate_frame={kind:planar(2D)|cartesian(3D)|geocentric(3D metres),unit:metre|millimetre|centimetre|foot|inch|unknown,axis_order:xy|xyz,source_name?:label,target_name?:label} declares common units; geographic degrees are rejected. max_fit_error is a positive tolerance in those units. Optional checkpoints are genuinely withheld source/target pairs, never reused fitting positions; max_checkpoint_error requires actual checks. Repeated or rank-deficient controls and singular/collapsed results fail. NumPy and the existing affine fit are centered/normalized;3D uses SciPy Kabsch with positive uniform scale and a proper rotation. Returns homogeneous column-vector matrix/inverse, parameters, residuals, degrees of freedom, independent checks and roundtrip error; points (max100000) use direction=forward|inverse.2D affine_matrix follows Shapely[a,b,d,e,xoff,yoff].2D translation/similarity also returns native_subset_transform for existing planar source-unit native copy; native_subset_transforms is the ordered sequence, one call for translation/similarity or two SVD-decomposed native calls for full affine including shear and reflection; this preserves existing metadata/styles and is not a datum change. Metre similarity models export a PROJ pipeline,3D with exact equations and runtime-verified Euler convention; rotation_vector_radians is axis-angle, not PROJ Euler rx/ry/rz. Fitting residual alone is not ground accuracy.3D math does not establish native3D file editing, a MapGIS coefficient-file writer, kinematic transforms or global extrapolation accuracy."""
        from .control_transform import fit_control_transform
        return fit_control_transform(model,controls,coordinate_frame,max_fit_error,checkpoints=checkpoints,max_checkpoint_error=max_checkpoint_error,points=points,direction=direction)

    @server.tool()
    def mapgis_prepare_map_frame(part:str,parameters:dict,template:dict,request_id:str)->dict:
        """Create editable frame components. Geographic parts border/grid (WL) or labels (WT): parameters={geographic_bounds:[west,south,east,north],grid_step_degrees,subdivisions?:32,geographic_crs?:EPSG4326 explicit dict}; densifies before PROJ and checks native CRS encoding. Fixed page parts page.title/page.north_label/page.scale_labels (WT) or page.north_arrow/page.scale_bar (WL): common parameters={view:exact native plotting view,anchor_mm:[x,y] from paper bottom-left,element_id:stable name,font_mm?:3.5,line_width_mm?:.2,project_environment?:identity}; requires identity global project transform. page.title adds text; north parts require length_mm,angle_degrees clockwise from page-up,direction_kind:'declared' (no true-north inference); scale parts require distance_metres,metres_per_native_unit,segments?:4,tick_mm?:2, all planar units explicitly declared. Page template={name,crs:{kind:'local',name:'native plot frame',unit:'unknown',axis_order:'xy',reference_sha256:source frame hash},map_scale:1,style?:{color,layer,font_zh,font_en,font_style}}; line style may instead use linetype. The same aspect fit and paper centring as native plotting map millimetres to stored coordinates. Page vectors carry stable element/part attributes and view/units metadata; use native resave/project/plot for independent native execution and actual output. Geographic template still requires explicit target crs and map_scale. No national sheet-number, geodetic scale accuracy or arbitrary saved page-object format is inferred. apply/get/cancel use the existing ledger."""
        return prepare_map_frame(bridge,part,parameters,template,request_id)

    @server.tool()
    def mapgis_prepare_annotations(file:str,expected_sha256:str,field:str,template:dict,request_id:str)->dict:
        """Make a new native annotation WT from a saved attribute field. Points retain position, lines use half-length, polygons use an interior representative point (avoids holes). Keeps source attributes/physical mapping, explicit CRS/scale and native text style. apply/get/cancel publish a new branch; no source mutation."""
        return prepare_annotations(bridge,file,expected_sha256,field,template,request_id)

    @server.tool()
    def mapgis_prepare_seams(files:list[dict],parameters:dict,template:dict,request_id:str)->dict:
        """Join 2..100 saved native files by an explicit class field: files=[{file,expected_sha256}], parameters={field,grid_size?}. Optional positive precision grid snaps to a shared zero-origin lattice, then GEOS dissolves each class. Reports movement and source mappings; only the group field is retained in the derived attributes. Requires a common declared CRS and template name/scale; inputs are preserved."""
        return prepare_seams(bridge,files,parameters,template,request_id)

    @server.tool()
    def mapgis_prepare_attribute_report(file:str,expected_sha256:str,sql:str,request_id:str,limit:int=100000)->dict:
        """Persist a real read-only native attribute query/aggregate as JSON plus UTF-8 CSV. Same SQL limits and authorizer as query_attributes. Whole result set/member hashes publish atomically through apply_patch; truncation is explicit and source bytes stay unchanged."""
        return prepare_attribute_report(bridge,file,expected_sha256,sql,request_id,limit)

    @server.tool()
    def mapgis_native_project_info(file:str,expected_sha256:str)->dict:
        """Inspect a saved MPJ through the installed exact-build project library in an owned process. Reopens vector layers and reads actual native active counts; MSI backgrounds return native extents. No project edit/save call. Does not bind or change an external editor. Native paths must fit the verified CP936 interface."""
        return native_project_info(bridge.root,file,expected_sha256)

    @server.tool()
    def mapgis_prepare_native_project(layers:list[dict],parameters:dict,request_id:str)->dict:
        """Prepare a portable native MPJ with copied WT/WL/WP and MSI layers. layers=[{file,expected_sha256}], parameters={name:'map.mpj',coordinate_frame:'native',system_library?:{folder,expected_manifest_sha256}}. Optional system libraries travel as sibling .lib files with an exact-MPJ-bound .agent.json manifest. Subsequent agent project edits and plots preserve/select these libraries automatically after relocation; this does not configure another GUI editor. Uses native Create/Append/Update/Save and fresh native reopen with the old directory hidden. apply publishes the entire set atomically; sources stay unchanged. Establish a common physical coordinate frame explicitly."""
        return prepare_native_project(bridge,layers,parameters,request_id)

    @server.tool()
    def mapgis_prepare_project_edit(file:str,expected_sha256:str,layers:list[dict],request_id:str,environment:dict|None=None)->dict:
        """Prepare a new portable branch of an OWNED saved MPJ through native project APIs. Pass the complete desired ordered layers: {source_index:1,state?:0..3,display_limits?:[minimum,maximum]} retains an existing one-based native_project_info index; {file,expected_sha256,state?,display_limits?} appends WT/WL/WP/MSI. Omitted source indexes are removed from the NEW project; list order is stacking order. States: 0 hidden, 1 displayed, 2 editable, 3 current; MSI uses 0/1, one current per vector kind. Display limits are native drawing scales (paper mm per stored native coordinate unit), not map-scale denominators. [0,0] disables limits; maximum <=0 is unbounded; endpoints are inclusive. Native project plots honor these limits. Optional environment partially overrides {translation:[x,y],scale:[sx,sy],rotation_degrees,paper_mm:[w,h]}; omitted values retain the original saved header. scale >=0.001, paper <=2000mm; transform order R(rotation)*diag(scale)*(nativeXY+translation), with view bounds in transformed coordinates. This is the native project layout transform, not a CRS reprojection. Native save and fresh-process relocation/reopen check identity, order, state, limits and member hashes. Originals stay unchanged; existing ledger apply/get/reconcile_artifact publishes atomically. External GUI unsaved state is independent."""
        return prepare_project_edit(bridge,file,expected_sha256,layers,request_id,environment)

    @server.tool()
    def mapgis_query_attributes(file:str,expected_sha256:str,sql:str,limit:int=1000)->dict:
        """Read-only SQLite over saved native attributes in table features. __record_id is physical identity; ID may repeat. SELECT/CTE/aggregates allowed, writes/ATTACH/PRAGMA/extensions denied; bounded query work and row limit. Source remains unchanged."""
        return query_attributes(file,expected_sha256,sql,limit)

    @server.tool()
    def mapgis_prepare_export(file:str,expected_sha256:str,driver:str,request_id:str)->dict:
        """Prepare a standard vector bundle: GeoJSON, GPKG or ESRI Shapefile. Full geometry/attribute readback; sidecar binds physical IDs, native symbols/text/styles and member hashes. GeoJSON uses the 2008 named-CRS extension for custom projected data. Shapefile truncation/collisions rejected. apply_patch atomically publishes all members; status verifies the complete set."""
        return prepare_export(bridge,file,expected_sha256,driver,request_id)

    @server.tool()
    def mapgis_prepare_import(file:str,expected_sha256:str,template:dict,request_id:str,layer:str|None=None,
                             expected_members:dict[str,str]|None=None,multipart:str='reject',reproject:bool=False)->dict:
        """Prepare new WT/WL/WP from GeoJSON, GPKG or SHP. template requires name, explicit output crs and map_scale. SHP requires every expected_members hash returned by vector_source_info (.shp/.shx/.dbf plus CRS/encoding/index files). Multipart point/line records require multipart=explode to copy attributes into separate native records with explicit provenance. CRS changes require reproject=True; the selected PROJ operation forbids ballpark/missing-grid fallback, transforms existing vertices and must fit the native CRS writer. A project-local input-hashed sidecar restores native annotations/styles. Invalid/mixed/3D geometry, duplicate identities and ambiguous GPKG layers fail. apply/get/cancel use the existing ledger."""
        return prepare_import(bridge,file,expected_sha256,template,request_id,layer,expected_members,multipart,reproject)

    @server.tool()
    def mapgis_vector_source_info(file:str,layer:str|None=None)->dict:
        """Inspect a local GeoJSON/GPKG/SHP through GDAL/Pyogrio. Returns actual layer names, geometry types, fields, CRS, encoding, bounds/count when cheaply available (-1/null means unavailable), and hashes of all shapefile members including DBF/PRJ/CPG. Select a GPKG layer explicitly when ambiguous. No repair, write or automatic projection. Pass members into prepare_import's expected_members to prevent partial-set imports."""
        return vector_source_info(file,layer)

    @server.tool()
    def mapgis_analyze_terrain(file:str,expected_sha256:str,height_field:str,base_height:float|None=None)->dict:
        """Analyze saved WT elevations using SciPy Delaunay. Returns planar hull area, triangulated 3D area and exact piecewise-linear cut/fill against an optional horizontal base. XY and elevations must share a linear unit; no extrapolation outside the convex hull. Does not mutate the source."""
        return analyze_terrain(file,expected_sha256,height_field,base_height)

    @server.tool()
    def mapgis_prepare_terrain(file:str,expected_sha256:str,height_field:str,parameters:dict,template:dict,request_id:str)->dict:
        """Prepare terrain outputs from saved WT values. Existing SciPy paths: operation=tin (WP), grid with bounds/step (WT), contours with grid:{bounds,step}/levels (WL); vectors need source CRS/map_scale. Native DTM: parameters={operation:'native.idw',bounds:[xmin,ymin,xmax,ymax],shape:[columns,rows],power:2,search_radius:500,anisotropy_ratio:1,anisotropy_angle_degrees:0}, template={name:'result.grd',crs:explicit source frame}. Calls the exact MapGIS 6.7 IDW export, all-point search, integer power 1..16, major/minor ratio >=1, no smoothing. The native all-point method uses search_radius/1e8 as coincidence tolerance; it includes rectangular extrapolation beyond the sample hull. Saves GRD plus bound coordinate/provenance metadata; GDAL and another native PID check every saved cell. Native TIN: parameters={operation:'native.tin',vertical_unit:'metre'|'foot'|'us_survey_foot',constraints?:[[physical WT record ID,record ID]]}, template={name:'result.tin',crs:explicit matching source frame}. Pinned CDT2.0.1 computes constrained Delaunay topology with original elevations; actual MapGIS APIs save/reopen the native TIN. Full convex hull; no inserted/interpolated heights; crossing constraints require an existing elevation node. CRS/height units are bound in a required sidecar for analysis, native factory coordinate parameters are retained. Proprietary MapGIS triangulator availability is not claimed. Separate height_field names the finite WT attribute. Apply/get/reconcile_artifact use the ledger; no mouse or desktop input."""
        if 'height_field' in parameters and parameters['height_field']!=height_field:
            from .core import BridgeError
            raise BridgeError('TERRAIN_HEIGHT_FIELD_CONFLICT')
        return prepare_terrain(bridge,file,expected_sha256,parameters|{'height_field':height_field},template,request_id)

    @server.tool()
    def mapgis_native_grid_info(file:str,expected_sha256:str)->dict:
        """Read saved Surfer GRD through GDAL and a fresh isolated MapGIS DTM process. Validates dimensions, grid-centre bounds, mask and every value. Native buffers are made from an owned copy; original GRD remains unchanged. At most 2000000 cells. No coordinate system is guessed from a GRD header."""
        from .native_terrain import native_grid_info
        return native_grid_info(bridge.root,file,expected_sha256)

    @server.tool(annotations={'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_native_tin_info(file:str,expected_sha256:str,base_height:float|None=None)->dict:
        """Inspect a saved MapGIS WTIN through a fresh isolated native process. Dense active profiles up to 100000 points/200000 triangles. Reads every native vertex/face, validates finite geometry, duplicate/nonmanifold faces, shared-edge/neighbor agreement, coverage and minimum angle. A hash-bound .tin.json supplies explicit CRS/vertical units; absent metadata stays unknown. With known units, reports 3D surface area and optional exact piecewise-linear cut/fill at base_height (given in the declared elevation unit); converts Z to XY units before analysis. Original TIN/metadata are preserved. Does not represent a WP as a proprietary TIN or drive the desktop."""
        from .native_tin import native_tin_info
        return native_tin_info(bridge.root,file,expected_sha256,base_height)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_tin_edit(file:str,expected_sha256:str,operation:Literal['node.height','node.move','node.insert','node.delete','edge.flip','constraints.replace'],parameters:TinEditParameters,template:dict,request_id:str)->dict:
        """Edit a bound CDT-generated native TIN into a NEW .tin branch. Native one-based node IDs: node.height updates=[{node_id,z}] preserves XY/faces; node.move updates=[{node_id,x,y}] retains Z and retriangulates; node.insert points=[[x,y,z]] appends nodes; node.delete node_ids=[...] records old-to-new identities and refuses constraint endpoints. constraints.replace edges=[[node1,node2]] retriangulates with explicit breaklines. edge.flip nodes=[a,b] exchanges one unconstrained interior edge only when its two-face domain is convex, retaining all XYZ and face slots. Crossing constraints require an existing elevation node. template={name:'new.tin'} inherits the hash-bound CRS/vertical units. CDT handles retriangulation; actual MapGIS APIs save the TIN and a different native process reads every record. Requires a dense full-hull model with this connector's coordinate metadata. No source mutation or desktop input. apply/get/reconcile_artifact use the ledger; interrupted saves reconcile without replay."""
        from .tin_edit import prepare_tin_edit
        return prepare_tin_edit(bridge,file,expected_sha256,operation,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_tin_profile(file:str,expected_sha256:str,parameters:ProfileParameters,template:dict,request_id:str)->dict:
        """Prepare an editable terrain cross section directly from saved native TIN faces, preserving exchanged edges and constraints without retriangulation. parameters={path:[[map_x,map_y],...],spacing:positive XY-unit distance,outside:'error'|'gap',vertical_exaggeration?:1}. Samples regular stations, route vertices and actual triangle crossings; explicitly preserves missing-domain gaps. Hash-bound source metadata must declare linear XY and elevation units. CSV/JSON distance and elevation retain original declared units. New section WL geometry converts X=horizontal distance to metres and Y=elevation to metres times exaggeration, with a distinct local section frame. template={name:'section.wl',map_scale:positive,style?:native line style}. Atomic bundle also includes native WT stations and profile.json with source XY/Z, native face IDs, unit factors and frame. At most 20000 stations. Use native resave/project/plot tools for actual MapGIS output. This is a terrain section, with no inferred geological strata or borehole interpretation. apply/get/cancel/reconcile_artifact use the ledger; no desktop input."""
        from .tin_profile import prepare_tin_profile
        return prepare_tin_profile(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_tin_vectors(file:str,expected_sha256:str,part:Literal['nodes','faces','edges'],template:dict,request_id:str)->dict:
        """Derive editable WT nodes, WP faces or WL edges from an actually saved MapGIS TIN through a fresh native reader, without retriangulation. template requires new name matching part, explicit matching CRS and map_scale, with optional native style. Keeps one-based native node/face IDs, original Z values and endpoint IDs in attributes; edges carry boundary/constraint flags from the bound model metadata. Vectors are 2D with Z attributes, distinct from the native 3D TIN file. Source TIN and .tin.json hashes guard atomic publication. apply/get/cancel/reconcile_artifact use the ledger; no desktop input."""
        from .native_tin import prepare_tin_vectors
        return prepare_tin_vectors(bridge,file,expected_sha256,part,template,request_id)

    @server.tool()
    def mapgis_prepare_grid_vectors(file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
        """Create editable native WL contours or WP isobands directly from a GRD using ContourPy, without reinterpolation. parameters={operation:'contours',levels:[...]} or {operation:'bands',levels:[lower,...,upper],colors:[native index per interval]}. Optional mask={file,expected_sha256,mode:'convex_hull' for WT|'geometry' for WP}. Template supplies name, explicit CRS and map_scale. If a grid metadata sidecar exists its grid hash/frame are checked and it is bound as a publication source. Returns empty levels, interval areas and native readback QA. Source values are not clipped for background removal; hull clipping is explicit. Apply/get/reconcile_artifact use the ledger."""
        from .grid_vectors import prepare_grid_vectors
        return prepare_grid_vectors(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_grid_analysis(file:str,expected_sha256:str,parameters:SurfaceParameters,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Derive slope, aspect, hillshade, TRI, TPI or roughness from a saved native Surfer GRD through the bundled GDAL public API. parameters requires operation and vertical_unit:'metre'|'foot'|'us_survey_foot'; template={name:'result.tif',crs:explicit source frame}. XY must use known linear projected/local units; no coordinate or elevation units are guessed. Gradient algorithm defaults to Horn; TRI defaults to Riley. compute_edges defaults false (complete 3x3 neighborhood, one-cell NoData border); true explicitly permits GDAL edge/missing-neighbor interpolation. Optional slope_format:'percent', aspect_convention:'trigonometric', flat_as_zero, or hillshade azimuth/altitude/z_factor belong only to their named operation. Flat aspect is undefined by default. Returns atomic TIFF plus hash-bound coordinate/algorithm/unit metadata, finite NoData, valid/masked counts and range; valid TPI=-9999 is preserved. Source grid/metadata/dependency hashes guard publication. This reuses GDAL algorithms; import the result with prepare_native_raster to_msi for native MapGIS storage/reopen and project composition. apply/get/cancel/reconcile_artifact use the ledger; no desktop input."""
        return prepare_grid_analysis(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_viewshed(file:str,expected_sha256:str,parameters:ViewshedParameters,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Compute a NEW numeric DEM viewshed with the bundled GDAL3.10 public API. parameters={observer:[x,y],vertical_unit:'metre'|'foot'|'us_survey_foot',observer_height?:2,target_height?:0,max_distance?:0,curvature_coefficient?:0,mode?:'edge',band?:1}; other modes diagonal/max/min. Heights are above sampled DEM in vertical_unit, scale/offset applied before unit conversion; distance is metres,0 unlimited. Template={name:'visibility.tif',crs:explicit matching projected metre frame}. Require square north-up cells and complete finite elevations in the rectangular analysis window, at most2M cells. NoData is rejected because this GDAL algorithm ignores it. Observer uses the containing cell centre; both requested and sampled XY are returned. Numeric codes0 hidden/1 visible/2 outside distance, exact cell-centre range mask; separate -display.tif is RGB for native MSI/project composition. Algorithm/input/runtime fingerprints guard recovery of completed analysis after parent loss without rerunning GDAL; an incomplete unacknowledged algorithm needs a new request. Source/member hashes guard apply/get/cancel/reconcile_artifact. This terrain-cell model does not infer vegetation, buildings or observed field visibility. Native storage/reopen/output use existing raster/project tools. No desktop input."""
        return prepare_viewshed(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':True,'idempotentHint':True,'openWorldHint':False})
    def mapgis_line_of_sight(file:str,expected_sha256:str,parameters:LineOfSightParameters,crs:dict,expected_members:dict|None=None,include_samples:bool=False,include_profile:bool=False)->dict:
        """Read-only terrain line of sight with no mouse. parameters={observer:[x,y],target:[x,y],vertical_unit:'metre'|'foot'|'us_survey_foot',observer_height?:2,target_height?:0,band?:1}; heights above DEM, source scale/offset applied. crs is explicit matching projected metre frame; require square north-up cells, endpoints inside, at most2M bounding-window cells/20000ray samples. Positions snap to containing cell centres. Reuses pinned MIT GDAL3.13 algorithm in Python to fix the installed3.10 ground-endpoint equality bug; no DLL upgrade or hidden height epsilon. Planar Bresenham cell-centre samples, equality visible, no Earth curvature/building/vegetation assumption. Missing/nonfinite ray cells fail; other unsampled cells do not obstruct. Returns visible, first ordered obstruction, sample_count, minimum clearance, sampled endpoints, map_features WL and endpoint_features WT for existing prepare_creation/native resave/project tools. include_samples explicitly adds the full elevation/clearance table; include_profile adds terrain/sight WL FeatureCollection with distinct local metre profile_crs. Same-cell ray is visible for nonnegative heights and has no nonzero2D line. Source/dependency hashes checked before/after; no writes to source or ledger."""
        result=line_of_sight(file,expected_sha256,parameters,crs,expected_members)
        if not include_samples:result.pop('samples')
        if not include_profile:result.pop('profile_features')
        return result

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_hydrology(file:str,expected_sha256:str,parameters:HydrologyParameters,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Prepare D8 drainage using pinned MIT PyFlwDir0.5.12, original DEM unchanged. parameters requires vertical_unit and depression_handling:'fill'; outlets:'edge'(default all valid boundary cells)|'min'(one minimum boundary outlet, connected domain), band?:1, nodata_boundary:'error'(default)|'open', max_elevation_rounding_error_m?:0, stream_min_cells?:positive integer, vector_outputs?:true. Declare template={name:'flow.tif',crs:matching projected metre frame,map_scale:positive}. Require square north-up cells, at most2M DEM cells. Apply band scale/offset and elevation-unit conversion. Upstream uses float32 elevations; default accepts only exact conversion, otherwise requires an explicit rounding allowance. Fill all depressions to pour points; no breaching/runoff/rainfall simulation. NoData needs explicit open-boundary choice and remains missing, valid zero stays valid. Atomic bundle contains six numeric GeoTIFFs: primary D8 codes, filled elevation/metres, fill depth/metres, upstream cells including self, upstream area/m2, basin labels; bound metadata/outletCSV. Optional editable WT outlets, WP basin cell polygons and WL flow segments when stream_min_cells supplied; terminal zero-length pit segments are retained as outlet points, not WL. At most20000 vector features per stream/outlet set; vector_outputs=false supports numeric-only larger graphs. Geometry/attributes readback is checked by the encoder; original native BAS60 save/reopen, MSI import and project/output compose existing tools. Completed algorithm outputs can be adopted after parent loss without repeating hydrology; source/snapshot/raw/runtime guards, apply/get/cancel/reconcile_artifact use the ledger. Crop boundary defines the model domain; outputs do not certify actual rivers or flood/discharge predictions. No desktop input."""
        return prepare_hydrology_isolated(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_processing(file:str,expected_sha256:str,parameters:ImageParameters,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Process saved GDAL-readable pixels into a NEW GeoTIFF without using a desktop. parameters requires operation and unique one-based bands. mean/median use radius(default1); gaussian additionally sigma(default1); sobel is gradient magnitude per pixel with normalized kernel/8, not map slope. Neighborhood edge defaults complete valid support; reflect explicitly mirrors boundaries. normalized_difference requires two bands and masks a zero denominator; linear_combination requires one weight per band plus optional offset. stretch uses percentiles(default2,98), outputs byte0..254 with255 reserved NoData; valid black0 remains valid. threshold requires one band and explicit threshold (>=); morphology adds erode/dilate/open/close, radius and iterations(default1,max10), with missing support propagated through every pass. kmeans reuses SciPy kmeans2 with local seeded RNG, kmeans++ initialization, classes2..32, iterations(default30), sample_size<=200000 and explicit standardize(defaultfalse); numeric clusters have no inferred geological labels. Labels1..K sorted by source-unit centres,0=NoData. isodata adds an auditable ISOCLUS-style adaptive variant using existing NumPy/SciPy: provide isodata={initial_classes,max_classes,min_cluster_pixels,split_stddev,merge_distance,max_merge_pairs?,split_fraction?,center_tolerance?}, with shared seed,iterations(default30),sample_size and standardize. Small fitting clusters are pruned, diffuse clusters split along maximum-variance feature axes, and nearby disjoint pairs merge by population-weighted centres; a single diffuse centre can split. Minimum counts and thresholds apply to fitting samples, in standardized units only when requested. Saves centres, scales, seed/sample-index hash, actions, counts and explicit convergence/capped status. Predictions use lexicographically sorted source-unit centres and first-minimum ties;1..Klabels,0=NoData. These numeric clusters have no inferred geological labels or proprietary solver equivalence. colorize requires one label band and palette=[{value:integer,rgb:[r,g,b]}], every valid label mapped explicitly; RGB channels0..254 reserve255 for NoData. pca fits every jointly valid pixel using NumPy thin SVD, with optional components1..min(bands,valid_pixels), standardize and whiten(defaultfalse). It records centering, scaling, loadings, eigenvalues, variance fractions, actual rank, sign convention and float32 score loss; whitening null components fails. Equal-eigenvalue subspaces have no unique individual axes. PCA is the assistant open backend, with no inferred geological labels. RGB/HSV/HLS conversions use exactly three ordered bands and explicit rgb_maximum; HSV/HLS channels are0..1, hue is cyclic turns, and input gamut is checked without implicit stretching or calibration. fft/ifft use NumPy complex128 and save both real/imaginary components as float64. fft accepts one real band or two ordered real/imaginary bands; ifft requires two. fft_norm=ortho(default),backward,forward; fft_shift(defaultfalse) applies fftshift on forward and ifftshift on inverse. Any missing sample is rejected; no implicit zero fill, padding or windowing. Frequency output has no spatial CRS and uses pixel-index coordinates with cycles/pixel bins. ifft optionally restores the same-size original grid from fft_reference={file,expected_sha256,expected_members}; otherwise it stays in index coordinates. No inverse imaginary component is discarded. sieve requires one exact signed32-bit integer label band, explicit minimum_region_pixels and connectivity4(default) or8. It reuses GDALSieveFilter: small connected equal-label regions merge into their largest eligible neighbour; isolated regions without a neighbour large enough remain unchanged. NoData is a barrier, zero remains a valid class, output is int32 with a collision-free NoData marker. This is not an inferred spectral-nearest or proprietary MapGIS rule. Other numeric output is finite float32 with collision-free finite NoData. Inputs use raw stored band samples; no implicit scale/offset calibration. Selected-band binary masks are intersected and dependencies hash-bound through publication; partial alpha rejected. Affine/CRS/GCP grid retained except for the explicit frequency/index policy above. At most16000000 selected band cells; crop/tile larger inputs. template={name:'new.tif'}; inspect raster_source_info and pass complete expected_members for dependent files. Native MSI conversion/readback and project output are separate tools. apply/get/cancel/reconcile_artifact use the ledger."""
        return prepare_raster_processing(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_classification(file:str,expected_sha256:str,parameters:ClassificationParameters,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Prepare NEW supervised class GeoTIFF and an auditable model/AOI/validation bundle.

        Select1..8 unique bands, method=minimum_distance|mahalanobis|parallelepiped,
        and training=[{class_id:1..254,name:optional,polygon:[closed_outer_ring,optional_holes]}].
        Each ring is an array of2D coordinates. Use2..32 explicit classes. Pixel
        coordinates mean column,row on image edges; aoi_space=map additionally
        requires aoi_crs equal to the saved affine raster CRS, without implicit
        reprojection. GCP imagery permits pixel AOIs; rectify before map AOIs.
        Different training classes cannot overlap. Optional validation uses the
        same AOI schema and MUST select disjoint pixels; only these held-out
        annotations produce the confusion matrix, accuracy and per-class metrics.
        This is pixel disjointness, not proof of independent geological validation.
        No validation supplied means no accuracy claim. Optional standardize is
        fitted only on training samples. minimum_distance uses Euclidean distance;
        mahalanobis uses each class covariance(ddof1), Cholesky solves and optional
        explicit covariance_ridge*identity. Singular covariance fails without a
        sufficient explicit ridge. This is a stated open algorithm, not a claim
        of exact proprietary generalized-distance behavior. max_distance optionally
        rejects either distance classifier. parallelepiped uses closed training
        min/max boxes. ambiguity=unclassified(default)|lowest_id resolves exact
        distance ties and overlapping boxes.0 is valid unclassified,255 is NoData.
        Selected-band binary masks intersect; valid0 and original grid/CRS/GCP
        remain. Preserve model.json and training/validation AOI rasters with labels.
        At most16000000 selected cells. Native MSI/MPJ/vector/plot conversions are
        separate explicit tools. Inspect raster_source_info, supply complete
        expected_members, then apply/get/reconcile_artifact through the ledger.
        """
        return prepare_raster_classification(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_crop(file:str,expected_sha256:str,window:Annotated[list[StrictInt],Field(min_length=4,max_length=4)],template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Copy an integer pixel window [column,row,width,height] into a NEW lossless TIFF with all bands; no resampling, rounding, padding or CRS guess. Retains exact stored samples, per-band masks (including partial alpha), nodata, palette, scale/offset, units and band descriptions. Affine origin is translated; GCP pixel positions are shifted and world controls/CRS retained, even if controls now lie outside the crop. No new controls invented: a native MSI import may require explicit in-bounds affine controls. RPC/geolocation-array inputs first need explicit rectification. Stale full-image statistics are omitted. Reads/writes/checks 256-pixel windows and independently compares saved samples and masks. template={name:'crop.tif'}. Inspect raster_source_info and pass its complete expected_members for dependent files. apply/get/cancel/reconcile_artifact use the ledger. Does not clip vectors or replace project layers; compose existing file_operation/native raster/project edit tools for a mixed project."""
        return prepare_raster_crop(bridge,file,expected_sha256,window,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_mosaic(sources:Annotated[list[dict],Field(min_length=1,max_length=1000)],parameters:dict,template:dict,request_id:str)->dict:
        """Mosaic saved rasters into a NEW GeoTIFF via Rasterio/GDAL, without a GUI. sources=[{file,expected_sha256,expected_members?:complete raster_source_info.members}]. parameters={method:first|last|mean,bounds:[left,bottom,right,top],resolution:[dx,dy],crs:explicit Earth CRS or referenced local frame,dtype?:output numeric type,nodata?:unused finite marker or null}; template={name:'mosaic.tif'}. Every source must already be affine north-up, in the same declared frame, with matching bands/types/calibration/units/colors, same resolution and aligned pixel origins (1e-7 pixel arithmetic tolerance). No implicit reprojection, resampling, GCP fitting or seam adjustment. Rectify first through existing tools. first uses the first valid whole pixel, last the last, mean an equal-weight average; partial alpha/different band masks must be composed explicitly, palette mean is rejected. Valid numeric zero stays valid because separate temporary coverage tracks occupied pixels. Final output retains real binary validity, palette and matching band metadata; stale statistics omitted. Safe dtype widening is explicit; mean defaults float64, optional float32 quantization. A finite nodata marker cannot equal any valid result; use a wider type/unused marker or keep a real mask. At most100M output band cells, merge chunks64MB with tiled output/readback. Original source/member hashes guard atomic apply/get/cancel/reconcile_artifact. Native MSI conversion and project plotting are separate existing steps; an arbitrary TIFF mask is not silently discarded for native import."""
        from .raster_composite import prepare_raster_mosaic
        return prepare_raster_mosaic(bridge,sources,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_mask(file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Clip an affine raster to a declared2D Polygon/MultiPolygon AOI with holes, retaining real validity and valid zero. parameters={geometry:GeoJSON polygon,crs:explicit same Earth CRS or referenced local frame,crop?:true,all_touched?:false,dtype?:safe wider type,nodata?:unused finite marker or null}; template={name:'masked.tif'}. Geometry must already use the raster frame; no inferred reprojection, repair or Z. Default selects pixel centers using GDAL polygon rasterization; all_touched selects touched pixels. crop uses the original integer source window, without resampling, including rotated affines; crop=false preserves the full grid and may return an explicitly empty mask. Source band masks must be common and binary; alpha/per-band differences need explicit composition first. Valid samples, calibration, colors and grid remain; outside/invalid samples are filled with the explicit nodata marker or0 under a separate real mask. Marker/value collisions fail. At most100M output band cells; blockwise writes and exact saved pixel/mask readback. GCP/RPC sources require affine rectification first. Source/member hashes remain guarded through atomic apply/get/cancel/reconcile_artifact; native MSI import is a separate verified numeric-mask operation."""
        from .raster_composite import prepare_raster_mask
        return prepare_raster_mask(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool()
    def mapgis_table_info(file:str,expected_sha256:str,sheet:str|None=None,limit:int=5)->dict:
        """Read XLSX with openpyxl without opening Excel. Returns sheet names, columns, numeric columns, missing counts, row count and up to 20 preview rows. Select sheet explicitly for a workbook with multiple sheets. Values come from cached formula results; a missing numeric cache is reported as missing and cannot be imported silently. Checks the source hash before and after streaming; original remains unchanged."""
        from .tables import table_info
        return table_info(file,expected_sha256,sheet,limit)

    @server.tool()
    def mapgis_prepare_table_points(file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
        """Create a WT directly from raw XLSX: parameters={sheet,x_field,y_field,attribute_fields:[...]}; template={name:'samples.wt',crs:explicit frame,map_scale}. Keeps every nonempty worksheet data row, physical worksheet-row mapping and all selected attributes; no anomaly filtering. Numeric XY and nonnull selected attributes required. The workbook is hash-bound through apply. Produces WT plus source/coordinate metadata as an atomic bundle. No Excel UI, macros or desktop interaction."""
        from .tables import prepare_table_points
        return prepare_table_points(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_table_join(file:str,expected_sha256:str,table:JoinTable,parameters:JoinParameters,request_id:str)->dict:
        """Join an XLSX onto existing native WT/WL/WP as a NEW branch. table={file,expected_sha256,sheet}; parameters={native_key,table_key,key_mode:'numeric'|'text',unmatched:'error'|'fill',columns:[{source:workbook column,name:new native field,type:'double'|'string'|...,length?:GBK string capacity,missing_value?:explicit unmatched replacement,null_value?:explicit matched-empty replacement}]}. Retains every native physical record, including repeated attribute keys; table keys must be unique/non-null. Text keys are exact; numeric mode explicitly compares finite decimals, including numeric strings. New fields only; existing fields/geometry/style/text and all nonattribute section bytes are preserved. Source workbook and native file hashes guard publication. Returns BUNDLE state/output, metrics of matched/unmatched/unused rows and an output sidecar with worksheet-to-physical-record mapping. Fix duplicate/null/missing keys or explicitly provide replacements on JOIN_* errors. apply/get/cancel/reconcile_artifact use the existing ledger; native save/reopen is a separate operation. No Excel UI, macros, network database writes or original-file mutation."""
        return prepare_table_join(bridge,file,expected_sha256,table,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    def mapgis_database_info(database:DatabaseSource,limit:Annotated[StrictInt,Field(ge=0,le=1000)]=20)->dict:
        """Inspect an offline local SQLite/Access/classic dBASE III table through an owned snapshot. database={file,expected_sha256,format:sqlite|access|dbf,table:exact base table forSQLite/Access ornull forDBF,encoding:explicit DBF codec ornull}; Access provider defaults Microsoft.ACE.OLEDB.16.0 (12.0 also explicit). No source writes, Access application, VBA, credential or remote connection. Active SQLiteWAL/journal/SHM orAccesslock companions must be absent and are bound through publication. <=512MiB database,200000rows,1000columns,2000000selectedvalues,128MiB selectedtext/binary and4MiBpercell. Dates retain engine civil fields as ISO; DBF physical deleted slots excluded fromjoins and retained in file; explicit C blank/allNUL byte policy. Returns column types, counts, bounded preview and type/lineage limits. Classic DBF C/N/F/D/L only; no inferred codepage/memo/complex types."""
        return database_info(bridge,database,limit)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_database_join(file:str,expected_sha256:str,database:DatabaseSource,parameters:DatabaseJoinParameters,request_id:str)->dict:
        """Left-join a hash-bound offline SQLite/ACEAccess/classicDBF table onto native WT/WL/WP as a NEW branch, preserving all physical records and nonattribute bytes. Uses the existing exact native schema/attribute encoder; numeric calculations do not pass through Excel. parameters={native_key,table_key,key_mode:text|numeric,unmatched:error|fill,columns:[{source,name,type,length?,missing_value?,null_value?,null_flag?,value_mode:identity|hex|boolean01|datetime_iso|date_component|time_component}]}. External keys unique/non-null; native keys may repeat. Each source column containingNULL REQUIRES a new null_flag short field:1=sourceNULL,0=non-null,-1=unmatchedrow. NULLreplacement and unmatchedreplacement explicit and never used to infer state; emptytext,valid0,emptybinary stay distinct. Binary needshex andboolean needsboolean01. datetimecomponent extraction requires explicit mode, offset-aware dates refused; identityISOtext retains fullciviltime. Raw selected values, true DBFphysicalslot/SQLsnapshotrow+exactkey, metrics and humanliteralXLSX audit preserved. Source/main+absentdatabasecompanions guarded through apply. Existing XLSXtable.join remains unchanged. apply/get/reconcile_artifact use the atomicbundle; original save/freshreopen/plot separate. No external source mutation or automatic synchronizer."""
        return prepare_database_join(bridge,file,expected_sha256,database,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_database_writeback(file:str,expected_sha256:str,database:DatabaseSource,parameters:WritebackParameters,request_id:str)->dict:
        """Write selected native attributes to a NEW local database branch; originals remain unchanged. parameters={native_key,table_key,key_mode:text|numeric,unmatched:error|ignore,native_duplicates:error|identical,columns:[{native,database,null_flag?,value_mode:identity|hex|boolean01|datetime_iso|date_component|time_component}],access_output?:isolated_table_copy}. Targetkey immutable, sourcekeys unique; identical policy requires every duplicate native physicalrecord to agree on all values/nullstates. null_flag1 restores actualNULL,0 writes value,-1 refused for a matched row. SQLite: whole taskcopy, transaction, constraints, exact rowreadback and deny writes outside declaredtable/columns. ClassicDBF: prevalidate all fieldbytes with strictwidth/decimalprecision; no rounding/truncation, preservedeletedslots/uneditedbytes/header; outputstandaloneDBF+declaredCPG. Access REQUIRES explicit isolated_table_copy: new ACCDB contains ONLY this table's scalar data,fieldtypes/sizes; query/VBA/data-macro/default/index/relationship/otherproperty objects are not copied. This is not a wholeAccessdatabase clone. Type-bound parameters preserve enginecivil dates without localtimezone shifts; AccessDATETIME writes require datetime_iso. No Access UI or macroexecution. Atomic database/lineage bundle; original source and absentcompanions bound until apply; no live servercredentials, originals write or backgroundsync."""
        return prepare_database_writeback(bridge,file,expected_sha256,database,parameters,request_id)

    @server.tool()
    def mapgis_prepare_geochemistry(file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Process raw XLSX chemistry: parameters={sheet,elements:['W','Sn',...],id_field:'ID'}. Iteratively removes only values above Q3+1.5*IQR, using linear inclusive quartiles; sample SD has ddof=1. C=background mean+2SD; reports C/2C/4C and C/4C/16C. This screening does not certify normality. Preserves raw samples and returns per-element summary; the bundle contains full per-round/sample-row evidence, CSV summary and XLSX Raw/Summary sheets. Use ALL raw points for subsequent gridding. Apply/get/reconcile_artifact use the ledger; no Excel UI."""
        from .geochemistry import prepare_geochemistry
        return prepare_geochemistry(bridge,file,expected_sha256,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_geochemistry_statistics(file:str,expected_sha256:str,parameters:BackgroundParameters,request_id:str)->dict:
        """Explicit per-domain raw/log10 background estimators: iterative_sigma needs screen_sigma and threshold_sigma; median_mad needs mad_scale and threshold_sigma; quantile needs quantile>0.5. grouped first performs independent sigma screening, then needs grouped={estimator:mean_sd|left_mode,intervals,precision:full|section_decimals,mode_tie:error|lowest}; left_mode requires trim_sides=upper and reports the distinct lower-half equation rather than ordinary SD. Histogram frequencies/edges and estimator spread are saved; zero-width, uncovered samples and undefined modes fail explicitly. Units require every element's common unit or explicit null. domain_fields are caller-defined statistical partitions, never inferred geological populations. Sample SD uses ddof1; sigma removes strictly outside bounds and keeps ties, resetting each element. Incomplete iteration, missing/censored/formula/nonfinite measurements and nonpositive log inputs are rejected. Every raw sample stays in Raw/Classified XLSX and samples.csv, with physical worksheet-row lineage, per-round removals and analysis/native thresholds; classification uses the analysis scale. Apply/get/reconcile_artifact use the existing bundle ledger. Classified includes original coordinates and short derived fields for existing map tools; use explicit native circle graphics for class colours when a library symbol has fixed internal colours. No Excel macro execution, normality/mineralization validation or exact legacy VBA equivalence. Original upper-boxplot tool remains separate."""
        return prepare_statistics(bridge,file,expected_sha256,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_assay_qa(file:str,expected_sha256:str,parameters:AssayQAParameters,request_id:str)->dict:
        """Preserve a literal XLSX assay table and produce Raw/Wide/MapReady/Measurements/Issues/Batches plus JSON/CSV. Declare id_fields, batch_fields (source lab/method/batch/basis partitions), optional coordinate_fields and analytes. Each analyte needs name/value_field, quantity=mass_fraction|mass_concentration, measurement_basis, exactly one input_unit or unit_field, and target_unit. Supports explicit mass-fraction %,wt%,kg/kg,g/kg,mg/g,ppm,mg/kg,ug/g,g/t,ppb,ng/g and mass-concentration g/L,mg/L,ug/L, with micro-sign aliases; never assumes density, dry/wet, oxide/element conversion or ppm water equivalence. Source-specific qualifier_states/token_states map declared codes to measured/estimated/left/right/interval/missing/not_analyzed/rejected; unknown codes are retained as QA issues. Inline <,<=,>,>= and Unicode bounds are parsed; qualified_bound_source explicitly selects reported/detection_limit/quantitation_limit/upper_limit. Limits use constant values or row fields in the source unit; interval means [DL,QL). Missing/blank/measured0/estimated/censored/rejected stay distinct. Duplicate composite sample+batch keys, missing metadata, invalid limits/units/coordinates and conflicts remain visible; every row is retained. No substitution, outlier deletion, batch correction or inferred compatibility. Wide preserves non-exact cells as text; strict background tools reject them rather than silently imputing. MapReady holds textual states/values for the existing explicit CRS/native table.points route. Formula cells rejected before ledger; apply/get/reconcile_artifact use the existing atomic bundle. <=500000 sample-analyte pairs; source preserved."""
        return prepare_assay_qa(bridge,file,expected_sha256,parameters,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_geochemistry_combination(samples:SpatialSource,elements:list[ElementAnomaly],parameters:CombinationParameters,template:dict,request_id:str)->dict:
        """Combine hash-bound native WP anomaly footprints with native WT sample anchors. Each element declares a distinct name/value_field, unit or explicit null, positive threshold, optional positive background, positive weight and regions={file,expected_sha256}. Union duplicate/overlapping records per element, polygonize boundaries including holes, retain faces supported by minimum_elements distinct elements. parameters declares domain_id, boundary=strict_interior|unique_inclusive|all_inclusive, comparison=gt|ge, strength_rule=weighted_mean_member_ratio|weighted_sum_member_ratio|maximum_weighted_member_ratio, negative undefined_numeric_value and optional sample_id_field. Optional concentration_centres=all_observed_maxima produces a separate WT and table of every member element's observed maximum sample, preserving all exact ties; no geometric-centroid substitution. template={name:*.wp,crs,map_scale,optional style}; common explicit projected/local linear frame required. Descriptive per-face counts/mean/min/max/sample SD/threshold counts, mean/threshold, ratio*area_km2, and optional RNAP=(mean-background)/background*area_km2. Strength uses member ratios only. JSON/XLSX null stays distinct from measured0; native undefined marker explicit with N_SAMPLES. Every physical input sample and source-region relationship retained; boundary, multiple and unassigned owners reported. Exact finite nonnegative measurements required, no censored-value substitution or automatic threshold fitting. Atomic WP/XLSX/CSV/JSON bundle, empty intersection reported without a fake WP. apply/get/reconcile_artifact use existing ledger; native save/reopen/MPJ/plot separate. <=500000 selected sample-element values/source vertices and bounded region/pair budgets. Sampled indices do not establish mineralization, ore probability, metal tonnage or resources."""
        return prepare_combination(bridge,samples,elements,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_soil_sampling_grid(file:str,expected_sha256:str,parameters:SoilGridParameters,template:dict,request_id:str)->dict:
        """Generate new soil survey WT points, WT labels and clipped WL exploration lines inside a hash-bound WP area, retaining holes. Explicit native-frame origin, clockwise bearing from coordinate-grid +Y, metre point/line spacings, base numbers, odd/even or sequential line formula, signed-number policy and budgets. Absolute parity needs even base line and odd number step. template={name:*.wt,crs,map_scale,optional style}; no inferred Earth frame or true/magnetic north. CSV/JSON map every sample ID to lattice coordinates and output records. Empty line geometry is reported without a fake WL. Source untouched; apply/get/reconcile_artifact use the bundle ledger; original save/reopen/MPJ/plot are separate tools. No field accessibility or standard-compliance claim."""
        return prepare_soil_grid(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_sediment_sample_numbering(file:str,expected_sha256:str,parameters:SedimentNumberingParameters,template:dict,request_id:str)->dict:
        """Number all hash-bound WT anchors as stream-sediment sample positions. Explicit upper-left cell-centre/corner origin, metre dimensions, row/column count and first/last large-grid IDs. Large cells row-major; half-cell quadrants A/B/C/D=NW/NE/SW/SE; within-cell sort x ascending,y descending, then explicit physical-record tie policy. boundary_rule declares internal edge ownership; outside points never dropped. Preserve human text IDs through existing_id_field, skip reserved_sample_ids, and choose unused output field names. Unknown preserved suffix stays empty, not zero. New WT preserves every original physical ID and nonattribute section byte including original graphics/CRS/scale; template map_scale applies to the separate new labels. CSV/JSON retain numbering/previous-ID provenance. WT symbols/circles/arcs/text are explicitly treated as anchor positions, preserving their full graphics. template={name:*.wt,crs,map_scale}; style overrides refused. Apply/get/reconcile_artifact use the existing ledger; original save/reopen/MPJ/plot separate. Does not design sampling locations, infer QC reservations or certify standards/field quality."""
        return prepare_sediment_numbering(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool()
    def mapgis_analyze_network(file:str,expected_sha256:str,parameters:dict)->dict:
        """NetworkX/GEOS over saved WL: operation=route, service_area or components. Explicit directed boolean, optional cost_field, source/target XY or cutoff; snap_tolerance and node_intersections are opt-in. Physical IDs and edge direction preserved; negative costs refused. Service area returns reachable network segments including partial edges, not a filled isochrone."""
        return analyze_network(file,expected_sha256,parameters)

    @server.tool()
    def mapgis_prepare_network(file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
        """Prepare native WL for route, reachable segments or connected components using analyze_network parameters. Keeps source edge IDs/costs/attributes and explicit source CRS. New branch only; apply/get/cancel use the existing ledger."""
        return prepare_network(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool()
    def mapgis_raster_source_info(file:str,expected_sha256:str|None=None)->dict:
        """Inspect actual raster pixels, band validity masks, CRS/grid/GCPs and all GDAL-reported or recognized sibling dependencies. Returns members={basename:sha256}. Pass the complete map as expected_members for native import or warp when any sidecar exists. Hashing streams rows; source data stay unchanged. External VRT/network dependencies must first be materialized."""
        return raster_source_info(file,expected_sha256)

    @server.tool()
    def mapgis_prepare_native_tin_scene(operation:str,file:str,expected_sha256:str,parameters:TinSceneParameters,template:dict,request_id:str,expected_members:dict|None=None,texture:TextureInput|None=None,remove_texture:bool=False,line_overlay:TinLineOverlayInput|None=None,remove_line_overlay:bool=False,flight:TinFlightParameters|None=None)->dict:
        """Prepare original TIN perspective rendering without mouse or DEM conversion.

        create reads a dense active WTIN; render reopens an agent .scene.json with
        sibling model/camera/image dependencies. Explicit projected/local metre XY
        and metre elevations are required. Original topology bytes stay unchanged;
        a separate original-SDK local display area prevents large-world XY loss.
        Width/height32..2048, FOV30..120; eye/target are world metre coordinates.
        Saved exact native_camera restores local float vectors and double origin.
        Output includes original TIN, camera, native MSI, RGB TIFF/PNG and an agent
        manifest, not a proprietary native scene format. Optional texture is full
        coverage registered uint8 RGB2..1024px; original MSI extraction, explicit
        local display translation; canonical world RGB pixels retained. Render
        inherits texture, can replace it, or remove_texture.
        Optional line_overlay binds unchanged native-metre WL; saved TIN facet
        intersections and barycentric XYZ feed original BAS60/Tin3DView line
        draw. Source CRS assignment explicit; optional height_offset_m defaults0.
        Render inherits, replaces or remove_line_overlay; no retriangulation,
        no bridge across missing TIN domain, no full2D line-pattern claim.
        flight reopens a saved scene with explicit world-metre XYZ path points,
        native PAT speed (not metres/second), fps and optional frame_start/count.
        Requires a saved exact camera; render view/scale changes first. Original
        interpolation/timer/rendering, bounded ASCII PAT writer and Windows VFW
        AVI; every frame checked by fresh native stream readback. Saved final
        camera renders the last frame; flight replay uses the initial camera.
        Use apply_patch to publish and reconcile_artifact after interruption.
        """
        return prepare_native_tin_scene(bridge,operation,file,expected_sha256,parameters,template,request_id,expected_members,texture,remove_texture,line_overlay,remove_line_overlay,flight)

    @server.tool()
    def mapgis_prepare_native_scene(operation:str,file:str,expected_sha256:str,parameters:SceneParameters,template:dict,request_id:str,expected_members:dict|None=None,texture:TextureInput|None=None,remove_texture:bool=False,line_overlay:LineOverlayInput|None=None,remove_line_overlay:bool=False,flight:FlightParameters|None=None,annotations:AnnotationOverlayInput|None=None,remove_annotations:bool=False,region_overlay:RegionOverlayInput|None=None,remove_region_overlay:bool=False)->dict:
        """Create or rerender original MapGIS6.7 terrain without desktop input.

        operation=create reads a north-up affine DEM/GRD with explicit vertical_unit
        (metre/foot/us_survey_foot), optional band and max_elevation_rounding_error_m
        (default0). template={name:'map.scene.json',crs:projected metre CRS or
        referenced local metre frame}. At most2M cells,2..16384 rows/columns;
        preprocess missing heights explicitly. DEM bounds are source pixel centres.
        Original DemArea saves DEM_INFO/pyramid/data; a fresh process checks every
        float32 height. Original NewTin3DFly draws the perspective RGB frame on its
        owned hidden OpenGL context. Original MsiBase encodes MSI in a separate
        child, followed by exact fresh native pixel comparison and TIFF/PNG output.
        Perspective images have pixel coordinates and no terrain CRS.

        parameters: mode=overview(default)|walk,width/height32..2048,quality0.01..1
        (default1, retain at least two rows/columns), optional paired eye/target XYZ
        in source XY/metre Z. Native effective eye is returned; positions may clamp.
        operation=render reads saved relative native members, retains exact DEM
        bytes and saved view settings unless overridden; omit elevation calibration.
        The JSON is an agent manifest, not a proprietary native scene format.

        texture={file,expected_sha256,expected_members?} adds rectified full-coverage
        uint8 RGB in the same frame,2..1024 pixels per axis, no missing pixels.
        Original TexDbs creates a single-base-tile texture; source TIFF/MSI remain.
        Render inherits it; replace with texture or clear with remove_texture=true.
        Own native cache uses the original allocator; initial vendor allocation
        attempts still occur. No original DLL/IAT/system/licence changes.

        line_overlay={file,expected_sha256,coordinate_frame:'native_metres',
        source_crs?:explicit frame} adds one unchanged saved WL, up to20000 records
        and200000 vertices. Native physical XY must be metres (projected storage
        map_scale1000); all vertices must lie within DEM centre bounds. Original
        NewTin drapes existing vertices, without densification, retaining WL bytes,
        IDs and native styles. Default CRS comes from the legacy header; source_crs
        explicitly assigns the known source frame when that header is insufficient,
        must match the scene, and never reprojects coordinates. Render inherits,
        replaces, or clears via remove_line_overlay=true. Clip/transform a separate
        branch before composing when required.

        annotations={file,expected_sha256,coordinate_frame:'native_metres',source_crs?,
        font?:{face:'SimSun',points:14,bold:false,italic:false},height_offset_m?:5,
        max_view_distance_m?:1.7976931348623157e308,visible?:true} loads one unchanged
        ordinary-text WT in the same native metre frame; up to20000 active records,
        200000 physical slots and1MiB total plain GB2312 text. Anchors must lie within
        the DEM. Original 3D font settings apply to all labels; saved WT text styles
        and bytes stay intact. Font points1..30; installed glyph coverage and each
        label's atlas fit are checked. Original GDI glyphs are used, including black
        through an owned display-buffer compatibility pass. Render inherits,
        replaces, or clears with remove_annotations=true. Rich text and symbol WT
        records require another explicit workflow.

        region_overlay={file,expected_sha256,coordinate_frame:'native_metres',
        source_crs?,opacity?:1,visible?:true,height_offset_m?:0,max_view_distance_m?}
        composes solid WP fills with holes/multipart footprints in the same frame.
        The unchanged WP and a derived regions.mesh travel together. Existing CDT
        triangulates sampled boundaries and covered DEM nodes (at most100000 mesh
        vertices); the original renderer samples bilinear terrain heights and
        native fill colours. Standard OpenGL draws fills before original WL and
        annotations, preserving the original region display offset. It does not
        call the licensed TinBas triangulator. 2D fill patterns and per-region
        texture images are not applied. Render inherits, replaces, or clears via
        remove_region_overlay=true; inputs need native metre storage and bounds.

        operation=flight records an existing saved scene. flight={points:[[x,y],...],
        speed:original native spatial step,fps?:24,frame_start?:0,frame_count?:null,
        max_xy_rounding_error_m?:0}. Speed must fit render.fly_speed_range and is
        not metres/second. Original route interpolation and camera-height behavior
        apply; source XY must fit native float32 within the explicit allowance.
        Walk mode is automatic. Omitting frame_count records the complete route
        from frame_start; at most1000 total drawn steps and AVI below1GiB. Frame
        windows draw earlier steps to preserve camera history. Windows VFW writes
        uncompressed DIB24 AVI without the original codec dialog; a fresh native
        process checks every frame hash. scene MSI/TIFF/PNG show the last frame.
        flight.input and parameters retain inputs for repeatable flight. The last
        camera is captured before native Stop restores the initial view. render
        from a flight restores this final pose by default; explicit eye/target or
        mode=overview selects a new view. parameters.native_camera is an opaque
        returned saved pose for walk mode, with a pinned model basis; keep it intact
        to avoid source-world float32 position loss. Old alpha flight manifests
        without a final pose must be rerecorded from flight.input first.

        apply/get/cancel/reconcile_artifact publish complete directories atomically
        and adopt completed rendering/encoding after parent loss without replay.
        Software OpenGL/MFC windows stay on the owned private desktop; no mouse,
        keyboard or input-desktop switching.
        """
        return prepare_native_scene(bridge,operation,file,expected_sha256,parameters,template,request_id,expected_members,texture,remove_texture,line_overlay,remove_line_overlay,flight,annotations,remove_annotations,region_overlay,remove_region_overlay)

    @server.tool()
    def mapgis_prepare_native_raster(operation:str,file:str,expected_sha256:str,parameters:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Native TIFF/MSI conversion with no mouse: operation=to_msi or to_tiff, parameters={name?,gcps?:[{pixel:[col,row],world:[x,y]}],max_error?}. Supports uniform uint8/uint16/uint32/int8/int16/int32/float32/float64 bands (1..32), with finite numeric nodata where masks exactly match that marker; palette/extra invalid masks and partial alpha require explicit conversion. Typed paths use native MSI tile creation/reads, preserving type and every pixel with explicit nearest top-left pyramid sampling; fresh native extent is checked against the pixel-to-world control fit; the old TIFF adapter is retained only for verified byte gray/RGB. Typed streams are bounded to the verified 2GB native profile; split larger rasters explicitly. Sidecar inputs require complete expected_members from raster_source_info; dependencies are snapshotted and rechecked on publication. to_msi writes native GCPs, closes and reopens in a fresh process, and proves exact pixels. GCP saving is not rectification. A hashed sidecar preserves CRS identifiers the native converter drops; to_tiff restores it only when bound to the exact MSI. apply publishes the complete file set."""
        return prepare_native_raster(bridge,operation,file,expected_sha256,parameters,request_id,expected_members)

    @server.tool()
    def mapgis_prepare_raster_warp(file:str,expected_sha256:str,parameters:dict,request_id:str,expected_members:dict|None=None)->dict:
        """Rectify/reproject rasters with GDAL, without a GUI. GCP models: gcp_model=affine(default), polynomial2, polynomial3 or tps, with3/6/10/3 minimum controls. max_gcp_error bounds the largest fitted world-axis residual; max_inverse_error_pixels(default0.5) checks control/in-hull model inverse residuals. checkpoints=[{pixel:[col,row],world:[x,y]}] must be withheld from fitting; optional max_checkpoint_error applies only with supplied checkpoints. Runtime Rasterio1.4.4 warping separately uses its documented0.125-pixel approximation. Source GCP frame/error units, controls, model residuals and exact target grid are saved beside the output; a small fit residual alone does not establish ground accuracy. parameters={name,target_crs,bounds:[xmin,ymin,xmax,ymax],resolution:[dx,dy],resampling:'nearest'|'bilinear'|'cubic',max_gcp_error?}. Sidecar inputs require complete expected_members from raster_source_info. Explicit target grid; 64MB warp memory, no full-image array. Common band-validity intersection is retained as an internal mask, resampled with nearest; valid numeric zero stays distinct from missing data. Original pixels remain in the source. New GeoTIFF is read back before preparation; apply/get/cancel use the ledger. Independent checkpoints determine real ground accuracy."""
        return prepare_raster_warp(bridge,file,expected_sha256,parameters,request_id,expected_members)

    @server.tool()
    def mapgis_reconcile_artifact(request_id:str)->dict:
        """Reconcile multi-file results after parent interruption using source/context/member fingerprints. Adopt completed native operations and sealed artifacts. Supported EPS format exports preserve completed files and finish missing formats; an active exact converter returns RUNNING, incomplete candidates stay in separate attempts. reused_formats/newly_converted_formats/conversions describe this work; native editing is never repeated. Unacknowledged image completion may need diagnostic readback conversion. Other incomplete producers are not blindly replayed. Use apply_patch after PREPARED."""
        with bridge.connect() as db:
            row=db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        if row:
            import json
            if json.loads(row['receipt']).get('artifact_type')=='volume.plot':
                return reconcile_volume_plot(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type') in {'volume.scatter.import','volume.grid','volume.slice'}:
                return reconcile_volume(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='terrain.native_idw':
                from .native_terrain import reconcile_native_idw
                return reconcile_native_idw(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type') in {'terrain.native_tin','terrain.tin_edit'}:
                from .native_tin import reconcile_native_tin
                return reconcile_native_tin(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='terrain.viewshed':
                from .visibility import reconcile_viewshed
                return reconcile_viewshed(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='terrain.hydrology':
                from .hydrology import reconcile_hydrology
                return reconcile_hydrology(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type') in {'native.scene.tin.create','native.scene.tin.render','native.scene.tin.flight'}:
                from .tin_scene import reconcile_native_tin_scene
                return reconcile_native_tin_scene(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type') in {'native.scene.create','native.scene.render','native.scene.flight'}:
                from .native_scene import reconcile_native_scene
                return reconcile_native_scene(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type') in {'native.msd.create','native.msd.edit'}:
                from .native_msd import reconcile_native_msd
                return reconcile_native_msd(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='native.subset':
                from .native_com import reconcile_native_subset
                return reconcile_native_subset(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='native.append':
                from .native_append import reconcile_native_append
                return reconcile_native_append(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='native.project.parameters':
                from .native_project_parameters import reconcile_project_parameters
                return reconcile_project_parameters(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='native.legend':
                from .native_legend import reconcile_native_legend
                return reconcile_native_legend(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='geology.borehole_chart':
                from .borehole_charts import reconcile_borehole_chart
                return reconcile_borehole_chart(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='native.project.plot':
                from .native_project import reconcile_project_plot
                return reconcile_project_plot(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='cartography.map_series':
                from .map_series import reconcile_map_series
                return reconcile_map_series(bridge,request_id)
            if json.loads(row['receipt']).get('artifact_type')=='plot.export':
                from .plot_export import reconcile_plot_export
                return reconcile_plot_export(bridge,request_id)
        return reconcile_bundle(bridge,request_id)

    @server.tool()
    def mapgis_prepare_plot_export(file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Export an actual native EPS to PDF, RGB TIFF and/or JPEG without desktop input. parameters={formats:['pdf','tiff','jpeg'],dpi:300}; dpi72..600, at most100M image pixels. Original EPS snapshot and Ghostscript identity are bound to the request. PDF/EPS comparison keeps mean<=1/fraction>=.99 plus spatial/color checks;72dpi tiling alias is rechecked at300dpi within32M pixels. JPEG quality95; no georeferencing inferred from paper. After parent loss, reconcile_artifact adopts verified completed formats, waits for an exact live converter, and converts only missing/incomplete formats in separate attempts. Files/contexts changed after checkpoints are rejected; unacknowledged images get full pixel readback comparison. apply_patch publishes the full set atomically; native editing/source EPS stay unchanged."""
        from .plot_export import prepare_plot_export
        return prepare_plot_export(bridge,file,expected_sha256,parameters,request_id)

    @server.tool()
    def mapgis_native_workspace(action: str, workspace_id: str, parameters: dict,
                                expected_revision: int | None = None, request_id: str | None = None) -> dict:
        """Own a persistent native buffer without mouse/UI. open: parameters={source,expected_sha256}; status: {}. edit: parameters={operation:'point.move'|'line.replace'|'arc.replace'|'node.move'|'feature.delete',parameters:{updates:[{record_id,x,y}]}|{updates:[{record_id,coordinates:[[x,y],...]}]}|{record_ids:[1,3]}}. For arc.replace use arc_id instead of record_id and preserve both shared endpoints. For WP node.move use updates:[{node_id,x,y}]; all incident arc endpoints move together with ownership/identity checks. Coordinates are source map units; the backend converts drawing scale. save: {}; edit/save require expected_revision and stable request_id. close: {} refuses dirty work, {discard:true} explicitly discards back to last save. recover: {} restores completed checkpoints and reconciles an interrupted edit/save without replay. close/recover require expected_revision. Saved outputs are new deliverables; the source stays unchanged. Dirty checkpoints survive parent/native failure. edit feature.append uses parameters={file:incoming saved native file,expected_sha256:incoming hash,record_ids:ordered incoming physical IDs}. It appends into the latest dirty checkpoint, preserving earlier edits, target IDs and saved baseline; same coordinates and compatible primary/known WP auxiliary schemas required. Adoption restarts only this owned native child, returns buffer_restarted, and commits revision/request/dirty together. New records follow appended drawing order. Save/discard/recover and request retry retain their semantics; a completed CopyList is not repeated. This buffer belongs to the agent, independently of any external MapGIS editor and its unsaved memory. Requires the explicitly enabled exact-build x86 backend. Smooth WL with operation line.smooth and {record_ids:[1],max_chord_error:0.01}; smooth WP with arc.smooth and {arc_ids:[1],max_chord_error:0.01}. These use SciPy natural/periodic cubic interpolation and native linearized vertices, preserving ends/shared nodes and all attributes; error is in source map units. point.radius edits native circle/arc drawing-mm radii and bounds. line.resize accepts {updates:[{record_id:1,start_delta:2,end_delta:-1}]}; positive signed source-unit arc lengths extend an endpoint, negative values shorten it. Outside the original range use the first/last nonzero segment direction; preserves identity, style and attributes. No automatic boundary snapping."""
        return workspaces.execute(action,workspace_id,parameters,expected_revision,request_id)

    @server.tool()
    def mapgis_reconcile_native_request(request_id: str) -> dict:
        """Reconcile RUNNING/FAILED native file or plot requests after interruption without repeating the edit. Verifies the recorded child PID/start/executable, durable completion receipt, source snapshots and actual candidate. File candidates get an independent comparison and fresh native reopen; plots get page/layer checks and independent rendering. Incomplete candidates remain FAILED. Then use apply_patch for PREPARED results."""
        return reconcile_native_request(bridge, request_id)

    @server.tool()
    def mapgis_prepare_native_plot(layers: list[dict], view: dict, request_id: str,system_library:dict|None=None) -> dict:
        """Compose actual native EPS without desktop input. layers ordered bottom to top: [{file,expected_sha256}], 1..100 saved WT/WL/WP. view={native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}; obtain raw native bounds with native_file_info, ensure all layers share the intended coordinate frame. Preserves aspect ratio, centers on the physical page, uses installed MapGIS symbols/fonts and verified winout60/mapdsp60. Independent Ghostscript preview rejects blank/truncated output. PREPARED candidates use existing apply_patch/get_request/cancel_request; originals stay unchanged. Geological content still needs source review."""
        return prepare_native_plot(bridge, layers, view, request_id,system_library)

    @server.tool()
    def mapgis_native_file_info(file: str, expected_sha256: str) -> dict:
        """Open a saved WT/WL/WP through BAS60 in a fresh isolated x86 process, without desktop input. Requires exact build, installed licence and explicit isolated legacy DEP compatibility opt-in. Cross-checks native active count against the independent Reader. Never reads live unsaved editor memory."""
        return native_file_info(bridge.root, file, expected_sha256)

    @server.tool()
    def mapgis_prepare_native_operation(operation: str, file: str, expected_sha256: str,
                                       parameters: dict, request_id: str) -> dict:
        """Prepare a new branch through actual BAS60 calls without mouse/UI. operation=resave with {}; point.text with {updates:[{record_id:1,text:"灰岩T#-2#=d"}]} replaces ordinary annotation0 content through raw native Get/Update, preserving XYZ/62graphic bytes/attributes/unselected records and recalculating native bounds. Text is nonempty GBK without NUL, at most32767 bytes each and1MiB per batch; formatting markers retained literally, no silent replacement or truncation. Use feature.delete to remove an annotation. point.style with {updates:[{record_id:1,style:{height:6,width:4,font_zh:2}}]} updates ordinary annotation0/symbol1 through native Get/UpdatePointInfo, recomputing bounds and preserving geometry/text/attributes/nontargeted graphic bytes. Common color/layer0..255 and angle; positive height/width; annotation spacing>=0,font_zh/font_en/font_style0..255; symbol symbol_no0..32767,pen_width>=0. Font IDs are numeric choices in the selected library, not a complete availability dictionary. Curves use their existing graphic/radius operations. point.radius for WT circle/arc records with {updates:[{record_id:1,radius_mm:5}]} uses native point-info get/update, regenerates per-record bounds and checks every other parameter/centre/attribute; radius is drawing millimetres. point.move with {updates:[{record_id:1,x:12.5,y:13.75}]}; line.replace for WL with {updates:[{record_id:1,coordinates:[[x,y],...]}]}; arc.replace for WP with {updates:[{arc_id:1,coordinates:[[x,y],...]}]} preserving both endpoints; node.move for WP with {updates:[{node_id:1,x:12,y:0}]} moves every incident arc endpoint, retaining native topology identities; or feature.delete with {record_ids:[1,3]} for WT/WL/WP. Coordinates use source map units, not raw drawing millimetres. Line replacement preserves physical identity, attributes and style; it inserts/removes/moves vertices through the native API. WP deletion checks shared arc ownership, holes and active regions before and after native save/reopen. Requires an owned work copy and explicit legacy x86 compatibility opt-in. Verifies saved geometry/attributes/style and opens it in another native process before PREPARED. apply_patch/get_request/cancel_request use the existing ledger. RUNNING or FAILED is never blindly replayed. line.smooth with {record_ids:[1],max_chord_error:0.01}, or arc.smooth with {arc_ids:[1],max_chord_error:0.01}, interpolates natural open / periodic closed cubic curves using SciPy, samples under an explicit source-unit chord-error bound and writes editable native vertices. Preserves exact endpoints/shared nodes, physical identities, all attributes and valid WP topology; no claim of native spline-object or original GUI algorithm equivalence. line.resize accepts {updates:[{record_id:1,start_delta:2,end_delta:-1}]}; positive signed source-unit arc lengths extend an endpoint, negative values shorten it. Outside the original range use the first/last nonzero segment direction; preserves identity, style and attributes. No automatic boundary snapping."""
        return prepare_native_operation(bridge, operation, file, expected_sha256, parameters, request_id)

    @server.tool()
    def mapgis_search_native_help(module: str, query: str = '', topic_id: str | None = None, limit: int = 10) -> dict:
        """Search installed HLP/CHM by module/words; pass returned topic_id to read the full topic. Source hash checked every time. Documentation is reference; execution uses the selected SDK, file or authorized private-process route."""
        return search_native_help(bridge.root, module, query, topic_id, limit)

    @server.tool()
    def mapgis_find_operations(query: str = "", module: str | None = None) -> dict:
        """Find local operations by task words/module. DISCOVERED is not executable or tested; describe before choosing an execution path."""
        return find_operations(bridge.root, query, module)

    @server.tool()
    async def mapgis_describe_operation(operation_id: str) -> dict:
        """Read parameters, preconditions, installed executable, native help and acceptance checks. Native UI requires the host's Sky and fresh observations."""
        result = describe_operation(bridge.root, operation_id)
        if result["executable_by_mcp"]:
            tools = {tool.name: tool for tool in await server.list_tools()}
            tool=tools.get(result['execution']['tool'])
            if tool is None:
                return result|{'executable_by_mcp':False,'execution_status':'MCP_TOOL_NOT_LOADED',
                    'availability':'RUNTIME_RELOAD_REQUIRED',
                    'resolution':'Use a fresh existing stdio/CLI entry for this source revision; the described tool is not registered in this process.'}
            add_operation_schema(result, tool.inputSchema)
            result["execution_status"] = "MCP_TOOL_AVAILABLE"
        else:
            result["execution_status"] = "HOST_OBSERVATION_REQUIRED"
        return result

    @server.tool()
    def mapgis_register_scan(source: str, request_id: str) -> dict:
        """Register original raster bytes/dimensions without full-image decode. Source/preview/tile coordinates remain distinct."""
        return raster.register_scan(source, request_id)

    @server.tool()
    def mapgis_scan_window(scan_id: str, window: list[int], request_id: str, max_dimension: int = 2048) -> dict:
        """Render a bounded source-pixel window using libvips. Returns exact preview-to-source mapping; never disables Pillow large-image protections."""
        return raster.render_window(scan_id, window, request_id, max_dimension)

    @server.tool()
    def mapgis_prepare_georeference(scan_id: str, crs: dict, gcps: list[dict], request_id: str,
                                   checkpoints: list[dict] | None = None, max_error: float = 1.0,
                                   pixel_frame: str = "source", pixel_to_source: list[float] | None = None) -> dict:
        """Fit an affine with explicit datum/axis order and independent checkpoints. Small fitting residual alone cannot pass acceptance. Errors use declared CRS units."""
        return raster.prepare_georeference(scan_id, crs, gcps, request_id, checkpoints, max_error, pixel_frame, pixel_to_source)

    @server.tool()
    def mapgis_write_georeferenced_window(scan_id: str, window_id: str, georeference_id: str, request_id: str) -> dict:
        """Write bounded RGB GeoTIFF plus a MapGIS .gcp file from an accepted affine. Native TIFF-to-MSI drops the GeoTIFF GCP metadata: explicitly import .gcp, save and reopen, then check overlay. Local grids stay local; source pixels and fingerprints are preserved."""
        return write_georeferenced_window(raster,scan_id,window_id,georeference_id,request_id)

    @server.tool()
    def mapgis_extract_candidates(scan_id: str, window_id: str, config: dict, request_id: str) -> dict:
        """Extract reviewable line/area/symbol candidates from a fingerprinted bounded window. GeoScan centerlines/GDAL cell-edge polygons preserve source offsets; map coordinates require an accepted georeference. Explicit roles/RGB mask/palette, no inferred geological age, all candidates UNREVIEWED. Areas optionally accept exclude_regions:[{geometry:preview-pixel Polygon,reason}] for symbols/text represented separately; exclusion_fill_px explicitly fills those occlusions from nearest visible palette, with inferred pixel counts recorded. max_fill_px controls other unknown gaps. No occlusion is inferred silently. Returns GeoJSON path and QA."""
        return extract_candidates(raster,scan_id,window_id,config,request_id)

    @server.tool()
    def mapgis_match_scan_windows(source:dict,reference:dict,parameters:dict,request_id:str)->dict:
        """Match a cropped scan to an already referenced whole image with OpenCV SIFT and similarity RANSAC. source={scan_id,window_id}; reference={scan_id,window_id,georeference_id}; parameters={ratio_threshold:.7,ransac_threshold_px:2,min_inliers:20}. Reads bounded fingerprinted windows, composes preview-to-source transforms, and returns actual keypoint GCP/checkpoint pairs in the declared reference frame. Rejects sparse/poorly distributed matches. Pass returned gcps/checkpoints to prepare_georeference with an explicit error tolerance. Checkpoints use distinct image keypoints but share match-model selection; this is image alignment, with no new datum or ground-accuracy claim."""
        from .scan_match import match_scan_windows
        return match_scan_windows(raster,source,reference,parameters,request_id)

    @server.tool()
    def mapgis_trace_scan(scan_id:str,window_id:str,config:dict,request_id:str)->dict:
        """Trace a scan stroke without a mouse. Config: mode='guided'|'until_junction', role='boundary'|'fault'|'contour'|'protected'|'annotation'|'dyke', anchors=[[preview_x,preview_y],...], rgb:[r,g,b], color_tolerance, snap_px, stroke_width_px:[min,max], gap_closing_px:0, simplify_px, optional georeference_id. Uses the existing GeoScan thinning core and a NetworkX ink graph. until_junction uses the first two anchors as start/direction and pauses at a branch with explicit choices; guided follows explicit anchors and pauses on equal shortest routes. Pixel centres use x+.5/y+.5. Output is fingerprinted GeoJSON with source/map transform, snapped anchors and UNREVIEWED QA. To retreat/change a route, resubmit explicit revised anchors under a new request; existing native workspace edits handle later geometry changes. No unrequested gap joining or geological classification."""
        from .seed_trace import trace_scan
        return trace_scan(raster,scan_id,window_id,config,request_id)

    @server.tool()
    def mapgis_merge_candidate_tiles(scan_id: str, tiles: list[dict], request_id: str,
                                      georeference_id: str | None = None, simplify_px: float = 0) -> dict:
        """Merge full-resolution area candidates using explicit disjoint core rectangles in original pixels. tiles: [{request_id, core:[left,top,width,height]}]. Compare overlapping windows, clip duplicate halos, union each explicit color class, validate matched coverage, optionally simplify jointly and georeference once. Requires same source and palette; all geology remains UNREVIEWED. Returns immutable GeoJSON path and measured seam disagreement."""
        return merge_candidate_tiles(raster,scan_id,tiles,request_id,georeference_id,simplify_px)

    @server.tool()
    def mapgis_prepare_raster_vectorization(file:str,expected_sha256:str,parameters:RasterVectorParameters,
        template:dict,request_id:str,expected_members:dict[str,str]|None=None)->dict:
        """Prepare NEW editable native WP regions from one thematic integer raster band. parameters={band?:1,connectivity?:4|8,max_regions?:20000}, <=16000000 pixels, <=100000 regions and2000000 ring vertices. Exact signed32-bit classes, valid0 and independent NoData mask; fractional/overflow/nonfinite valid values fail. Preserves pixel edges, holes and shared boundaries without smoothing.Point-touching8-connected parts are separate valid native polygon records with shared region_id and distinct part_id; zero-area retraced ring links are removed and every source pixel/class count is checked. Attributes region_id/part_id/class_id/pixel_count/coord_area retain logical region and class identity and exact part pixel count. coord_area is squared source coordinate units, never inferred square metres/geodesic area. template={name:'regions.wp',crs:explicit matching source CRS,map_scale:positive,style?:native region style}. GCP/RPC/geolocation images require separate explicit rectification; no implicit reprojection. Unreferenced rasters require an explicit local frame whose reference_sha256 equals the source hash. Reuses the native new-WP encoder and guarded creation ledger; native BAS save/reopen/plot remain separate explicit operations. Sources and all raster members remain unchanged, including through apply. Input manifest contains editable GeoJSON and QA/provenance; native export tools provide standard vector files."""
        return prepare_raster_vectorization(bridge,file,expected_sha256,parameters,template,request_id,expected_members)

    @server.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False})
    def mapgis_prepare_raster_fusion(multispectral:RasterReference,panchromatic:RasterReference,
        parameters:FusionParameters,template:dict,request_id:str,reference:RasterReference|None=None)->dict:
        """Fuse registered multispectral/PAN rasters into a NEW numeric float64 GeoTIFF. Each source={file,expected_sha256,expected_members?:complete raster_source_info.members}. Requires exactly equal dimensions, affine/CRS/GCP/pixel interpretation; explicitly use raster_warp/resampling first for different grids, as the original manual also requires. parameters={method:weighted|ihs|wavelet_ihs|wavelet_awlp|wavelet_maxabs,bands:ordered1..8,pan_band?:1,matching:none|mean_std,radiometry?:stored|scale_offset,nodata?:-9999}. weighted requires pan_weights:one explicit PAN fraction0..1 per band. ihs/wavelet_ihs require3ordered RGB bands and the documented linear neutral-axis IHS convention. Wavelet methods accept levels1..6(default1),edge:reflect|complete; separable B3 spline[1,4,6,4,1]/16 with2**j holes, no decimation. wavelet_ihs adds signed PAN details to intensity; wavelet_awlp injects details proportionally to band/intensity, optional normalized nonnegative intensity_weights, intensity_epsilon?:0,zero_intensity:mask|reject. wavelet_maxabs keeps MS lowpass and the signed larger-absolute MS/PAN detail per pixel/band/scale; magnitudes within64eps times the per-band source/scaled-PAN peak keep MS to prevent sign flips from convolution roundoff; may retain noise. mean_std fits PAN to intensity using lowpass PAN variance for wavelet methods and full PAN otherwise, over final valid support. No missing fill, implicit scale/offset calibration, clipping, inferred sensor response/MTF/haze or proprietary parameter equivalence. Shared validity propagates through every nonzero wavelet tap; valid0 remains distinct from missing. Saves full source/calibration/method metadata and change diagnostics, which are not accuracy. Optional same-grid reference with exactly the ordered output bands requires parameters.reference_scale_ratio=originalMS/PAN resolution ratio; computes RMSE/MAE/SAM/ERGAS against that actual guarded image, with undefined zero-vector/zero-mean cases reported. Caller reference suitability and physical registration are not inferred. template={name:'fused.tif'}. <=16000000 selected spectral cells; original MSI roundtrip/MPJ/plot use existing tools separately. apply/get/reconcile_artifact use the guarded atomic ledger, preserving every source/member."""
        return prepare_raster_fusion(bridge,multispectral,panchromatic,parameters,template,request_id,reference)

    @server.tool()
    def mapgis_prepare_creation(features: dict, template: dict, request_id: str) -> dict:
        """Prepare NEW WT/WL/WP from map-coordinate GeoJSON with unique feature IDs, explicit CRS/scale and native style. Shared arcs/holes use pinned mapgis2shp. Apply/get/cancel use the existing request ledger; native save/reopen is a separate explicit operation. WT Point geometry is the centre; style point_type=2 stores an exact native circle with positive radius_mm, pen_width, color, border_color and filled=0/1; point_type=3 stores an exact native arc with explicit0<=start_angle<end_angle<=360 degrees counterclockwise from east. Radius/pen width are drawing millimetres, not ground-coordinate distances. These kinds require a projected CRS or declared local drawing frame; no geographic approximation. Ordinary symbol/text parameters remain as before."""
        return prepare_creation(bridge, features, template, request_id)

    @server.tool()
    def mapgis_prepare_vector_operation(operation_id: str, features: dict, template: dict,
                                        parameters: dict, request_id: str) -> dict:
        """Prepare a NEW native branch using GEOS/PROJ: topology.polygonize/node/snap_grid/trim_dangles, correction.affine, overlay.clip/difference, geometry.buffer/simplify/dissolve, projection.transform, style.classify. trim_dangles requires parameters={max_length:positive source-coordinate length} and explicit per-feature role:boundary|fault|contour|annotation|protected|dyke. Nodes boundary lines, merges degree-2 chains and removes short leaf branches in one pass; other roles and source attributes stay intact. No snapping or recursive erosion. Explicit CRS/units and stable IDs; existing request apply/get/cancel commits the result."""
        return prepare_vector_operation(bridge,operation_id,features,template,parameters,request_id)

    @server.tool()
    async def mapgis_get_capabilities() -> dict:
        """Report local scope, installation, processes and exact-build live-state prerequisites; discovery is not a live acceptance test."""
        import psutil
        process=psutil.Process()
        return capabilities(bridge.root)|{'loaded_runtime':{'server_module':str(Path(__file__).resolve()),
            'pid':process.pid,'process_start':process.create_time(),
            'tool_count':len(await server.list_tools()),'operation_count':find_operations(bridge.root)['count'],
            'catalog_policy':'descriptions frozen with the importing Python revision; schemas come from registered tools'}}

    @server.tool()
    def mapgis_get_live_state(pid: int) -> dict:
        """Read exact-build 6.7 project/layer paths, selection pools and unsaved flags. Read-only. Active MDI identity requires independent Sky observation; never guesses it from view count."""
        return live_state(bridge.root, pid)

    @server.tool()
    def mapgis_inspect_file(file: str, offset: int = 0, limit: int = 10) -> dict:
        """Read native WT/WL/WP geometry, attributes and style. record_id is physical 1-based, not attribute ID; no GUI selection read."""
        return bridge.inspect(file, offset, limit)

    @server.tool()
    def mapgis_read_features(file: str, expected_sha256: str, record_ids: list[int] | None = None,
                             offset: int = 0, limit: int = 100) -> dict:
        """Read saved WT/WL/WP as paged GeoJSON with separate native styles/text. IDs are physical slots, attribute ID may repeat. Require the current file fingerprint; coordinates need explicit CRS/unit confirmation. For whole-file processing use prepare_file_operation without sending every vertex through the model."""
        return read_features(file, expected_sha256, record_ids, offset, limit)

    @server.tool()
    def mapgis_prepare_file_operation(operation_id: str, file: str, expected_sha256: str,
                                      template: dict, parameters: dict, request_id: str,
                                      record_ids: list[int] | None = None) -> dict:
        """Run existing GEOS/PROJ operations directly on a saved native file or explicit physical IDs. Same operations as prepare_vector_operation. Declare CRS/units and new WT/WL/WP name in template. Optional role_by_record maps string physical IDs to boundary/fault/contour/annotation/protected for polygonize. Produces a new derived branch with provenance and physical source mapping; the source hash is checked through publication. Clip/difference keep the source geometry family and omit boundary-only contacts of lower dimension. WT selection uses anchors, not symbol/text footprints; circle/arc centres are rejected as curves. Supported graphic fields, primary attributes and annotation text retained; auxiliary native sections are not copied, structural region indices regenerated. Apply/get/cancel use the existing ledger; does not edit live unsaved memory."""
        return prepare_file_operation(bridge, operation_id, file, expected_sha256,
                                      template, parameters, request_id, record_ids)

    @server.tool()
    def mapgis_make_work_copy(source: str, request_id: str) -> dict:
        """Copy a native vector into a new owned workspace, preserving original bytes. Repeated ID reconciles the same copy."""
        return bridge.copy(source, request_id)

    @server.tool()
    def mapgis_prepare_patch(file: str, expected_sha256: str, record_ids: list[int], request_id: str,
                             attributes: dict | None = None, style: dict | None = None,
                             attribute_updates: dict | None = None, style_updates: dict | None = None) -> dict:
        """Preview fixed-field edits to explicit physical records on an owned copy. attributes/style apply uniform values; attribute_updates/style_updates map selected canonical string physical IDs to individual attribute/graphic values, e.g. {'1':{'color':3},'3':{'color':5}} for style_updates. Uniform and individual fields must not overlap within their group. Existing date/time fields accept ISO strings; DATE accepts null, TIME cannot represent null or time zones. SQL query rows keyed by __record_id can feed these maps for calculations, joins and attribute-to-parameter classification; ID attribute may repeat. Preserves geometry, annotation auxiliary sections and every untargeted byte. WT symbol points support pen_width (float32 drawing mm, nonnegative); annotation font bytes are not pen width. Exact WT circles support border_color/filled/pen_width; arcs support start_angle/end_angle/pen_width, plus both color/layer. Each primitive uses its complete merged graphic profile for validation. Radius byte patches are rejected; use native operation point.radius, which updates bounds through BAS60. No source write."""
        return bridge.prepare(file, expected_sha256, record_ids, request_id, attributes, style, attribute_updates, style_updates)

    @server.tool()
    def mapgis_private_control(action: Literal['policy','open','status','observe','act','close'],
                               module: str = '', session_id: str = '', parameters: dict | None = None,
                               request_id: str = '', token: str | None = None) -> dict:
        """Call the installed original6.7 NON-SERVER modules in newly owned private processes. The user approved this project's general exception; native_general_private_ui=true remains required in the task root. policy gives exact-build modules. open(module,session_id,parameters={inputs:[{id,file,expected_sha256,dependencies?:[{name,file,expected_sha256}]}],outputs:[{id,name,directory?:bool}],idle_seconds?:15..900}) copies owned inputs, the exact executable and its own environment/libraries; survey uses its SuvSlib copy. Installed files remain unchanged. status gives owned identities/bindings. observe(request_id) gives controls, menus, client_rect and a token. act(request_id,token,parameters={operation,hwnd,...}) supports: set_text with value OR binding (optional member relative to a directory binding); button; command with observed command_id; select with index; set_check with value0/1/2; pointer with event=move|left_down|left_up|left_click|right_down|right_up|right_click|double_click and client x,y; key with unmodified ENTER/ESCAPE/TAB/SPACE/BACKSPACE/DELETE/INSERT/HOME/END/arrows/PAGEUP/PAGEDOWN/F2..F12; tree_items with parent_item=0 or observed item, limit1..512 and optional after_item for paged text/child discovery; tree with observed item and verb=select|expand|collapse; capture, which returns a PNG painted by the owned window inside the same restricted job, never by reading a screen DC. No physical input, foreground change or existing-process attachment. Delivery acknowledgment is not business completion: observe and independently read saved/reopened outputs. Same request reconciles; changed payload/stale tokens fail. close(parameters={discard:bool}) closes normally; explicit true may terminate only the verified own job even if its UI is unresponsive. Server-dependent modules, unbound paths, unknown commands, passwords and host clipboard are excluded. Read docs/PRIVATE-CALLS-PROPOSAL.md and docs/LOCAL67-ROUTES.md for verified workflows, routing and actual installation limits."""
        return private_control(bridge,action,module,session_id,parameters,request_id,token)

    @server.tool()
    def mapgis_describe_sdk(library: str | None = None, class_name: str | None = None,
                           interface: str | None = None, member: str | None = None,
                           offset: int = 0, limit: int = 100) -> dict:
        """Read original exact-installed MapGIS6.7 Automation types, methods, parameter directions/defaults and enum constants. Choose library then class_name OR interface; page with offset/limit. No registration, code offsets or native method execution. current_call_policy lists the bounded private typed-flow route; metadata alone does not mean every method or menu has executed."""
        return sdk_describe(bridge.root, library, class_name, interface, member, offset, limit) | {'current_call_policy': sdk_policy()}

    @server.tool()
    def mapgis_prepare_sdk_flow(parameters: dict, request_id: str) -> dict:
        """Execute a typed sequence of original SDK methods in the existing private x86 process, using owned copies and no user mouse/keyboard. First read describe_sdk and its current_call_policy. parameters={inputs:[{id,file,expected_sha256}],outputs:[{id,name:unused.wt|wl|wp,source:input_id}],objects:[{id,class}],steps:[{id,target,member,kind:method|get|put|putref,arguments:[{value:scalar}|{ref:earlier_symbol}|{out:new_symbol}|{input:id}|{output:id}],expect:optional_exact_scalar}]}. Original signatures select argument types; output arguments require out symbols. Coordinates are RAW original workspace/drawing units; no automatic conversion to source/geographic units. A read-only flow with outputs=[] can first read GetPos/rect. Load/Save bind explicit input/output, use empty DSN/login fields and require true. A failed assertion stops subsequent calls. Initial policy allows coordinate/graphic value objects and geometry workspaces, retaining physical IDs, exact source primary/auxiliary attribute tables and full map parameters on output. No attribute/schema edits, dialogs, DB, printing, raw addresses or arrays. Outputs are staged and original SDK reopened; apply_patch publishes the complete bundle. Reconcile existing request after interruption; no blind replay."""
        return prepare_sdk_flow(bridge, parameters, request_id)

    @server.tool()
    def mapgis_prepare_multimedia(file: str, expected_sha256: str, parameters: MultimediaParameters, request_id: str) -> dict:
        """Prepare a portable WT/WL/WP branch containing ORIGINAL native multimedia fields (codes9..13) and byte-identical sibling assets. parameters={fields:[{name,type:multimedia_text|multimedia_image|multimedia_map|multimedia_audio|multimedia_video}],assets:[{id:ASCII_identifier,file:owned_task_file,expected_sha256}],links:[{record_id:physical_ID,field,asset:id}],native_resave?:false}. This is the FULL desired primary media schema/links: every existing primary media field must be declared; omitted links become blank on the new branch. Ordinary attributes, physical IDs and geometry remain. Assets copy to media/id.ext with source hashes; unused/duplicate assets and links rejected. Clears the two native multimedia directory slots to use the file/working directory, never the sender's absolute path. Atomic nested bundle/reconcile/replay; inputs unchanged. Optional native_resave uses the enabled original isolated x86 SDK, fresh save/reopen and comparison before publishing the complete media bundle. Does not launch a viewer/player or execute/transcode assets; installed playback codecs and GUI activation remain NOT_VERIFIED. Auxiliary multimedia, embedded binary/table fields and native external-database linking are separate gaps."""
        return prepare_multimedia(bridge,file,expected_sha256,parameters.model_dump(),request_id)

    @server.tool()
    def mapgis_prepare_attribute_schema(file: str, expected_sha256: str,
                                        fields: list[dict], request_id: str,
                                        table:Literal['primary','arc','node']='primary',
                                        attribute_updates:dict|None=None) -> dict:
        """Prepare a NEW native file with a full desired attribute schema, preserving physical IDs, deleted slots, geometry/style/text and every non-attribute section byte. Retain/rename: {name:'岩性',source:'code'}; add: {name:'厚度',type:'double',default:2.5}; convert: {name:'count',source:'old',type:'int'}. Omitted fields are dropped and order follows this list. Types: string/byte/short/int/float/double/date/time/boolean/multimedia_text/multimedia_image/multimedia_map/multimedia_audio/multimedia_video; boolean requires JSON true/false or integer0/1 and has no NULL storage; media values are relative local references up to255 GBK bytes, blank means no link. Use prepare_multimedia to copy assets into a portable bundle. Date all-zero storage is NULL; invalid nonzero dates are refused. Time midnight is valid and has no NULL, microseconds are read without flooring binary64 residue; finer-than-microsecond values refused. String length is explicit GBK byte capacity. Names fit 19 GBK bytes; no truncation, fractional-to-integer loss or ambiguous duplicate names. Requires an owned source and its current hash. Uses pinned upstream codecs; apply/get/reconcile_artifact use the existing ledger. Native save/reopen is a separate explicit operation. table=primary (default) edits WT/WL/WP primary attributes; table=arc or node edits WP auxiliary attributes by physical arc/node IDs. Empty auxiliary tables require explicit type/default for every initial field. Optional attribute_updates maps canonical physical ID strings to desired-field values, including renamed/new fields, e.g. {'2':{'厚度':12.5}}; duplicate attribute ID values do not identify records. Read auxiliary schema/values through read_wp_arcs. Originals and ledger snapshots remain available."""
        return prepare_attribute_schema(bridge, file, expected_sha256, fields, request_id,table,attribute_updates)

    @server.tool()
    def mapgis_read_wp_arcs(file: str, expected_sha256: str, offset: int = 0,
                            limit: int = 20, arc_ids: list[int] | None = None) -> dict:
        """Read saved WP arcs with separate arc_id, owner_region_ids, node_ids, source-map coordinates and native line style. Each arc/node includes its actual auxiliary attributes or null when no stored row, with attribute_tables schemas and unassigned_closed_arc_ids. Closed native ring arcs may legitimately have both node IDs zero; only assigned nodes are returned. Returned nodes touch this page's arcs; each node includes all incident arc IDs and owner region IDs across the whole file. node_topology reports whether endpoint identities are usable. Arc/node IDs differ from region physical IDs and attribute ID. Source fingerprint and native signed walks are checked. Page active arcs or choose arc_ids. Use arc.replace for interior vertices with fixed endpoints, or node.move with {updates:[{node_id,x,y}]} to move every incident arc endpoint together while retaining node/arc/region identities. This reads saved files, independently of external GUI unsaved memory."""
        return read_wp_arcs(file, expected_sha256, offset, limit, arc_ids)

    @server.tool()
    def mapgis_prepare_delete(file: str, expected_sha256: str, record_ids: list[int], request_id: str) -> dict:
        """Prepare deletion of explicit active physical IDs in an owned WT/WL copy. Attribute ID is not identity. Keeps other bytes, native pools/styles and slot numbering; produces a NEW branch through apply_patch. No live unsaved-memory edits. WP requires native topology editing. Same-layout recovery can restore this request's flags; native reopen remains a separate check."""
        return prepare_deletion(bridge, file, expected_sha256, record_ids, request_id)

    @server.tool()
    def mapgis_apply_patch(request_id: str, expected_epoch: StrictInt | None = None) -> dict:
        """Apply a prepared PATCH, CREATE or DELETE to a NEW output branch after source hash recheck. Idempotent; does not overwrite originals or live documents. Reconcile get_request after interruption before retrying."""
        return bridge.apply(request_id, expected_epoch)

    @server.tool()
    def mapgis_get_request(request_id: str) -> dict:
        """Inspect the persisted request and output hash. Reconcile this state after interruption before retrying."""
        return bridge.status(request_id)

    @server.tool()
    def mapgis_compare_files(before: str, after: str, limit: int = 100) -> dict:
        """Read-only saved WT/WL/WP comparison by physical slots: geometry, attributes, graphic parameters, annotation text and coordinate header. Returns complete counts and bounded details; limit=0 gives counts. Renumbering is not semantic identity. Does not certify native UI or reopen."""
        return compare_native_files(before, after, limit)

    @server.tool()
    def mapgis_verify_request_output(request_id: str, file: str) -> dict:
        """Compare a saved file with an applied request's frozen candidate. Accepts native bookkeeping changes when records/text/styles/coordinate header match. Reports matches_candidate_records; never rewrites request status or labels an unobserved native reopen as verified. Use after native Save with independent live project/dirty checks."""
        return bridge.verify_output(request_id, file)

    @server.tool()
    def mapgis_cancel_request(request_id: str) -> dict:
        """Cancel an unapplied preview. Refuses to cancel an applied or uncertain output."""
        return bridge.cancel(request_id)

    @server.tool()
    def mapgis_prepare_recovery(applied_request: str, later_file: str, expected_sha256: str, request_id: str) -> dict:
        """Prepare a NEW recovery branch reverting only this request's fields. Uses compatible spans or a guarded mapping of unchanged physical records after native layout changes. Preserves later independent edits; same-field, deleted, renumbered or changed-geometry conflicts fail. Not universal native Undo."""
        return bridge.recover(applied_request, later_file, expected_sha256, request_id)

@cache
def registered_input_schemas() -> dict[str, dict]:
    # Registration derives schemas from signatures without opening a project or
    # calling any backend. Standalone description can use the same schemas.
    registry = FastMCP("MapGIS schema registry")
    _register_legacy_tools(registry, None, None, None, None)
    return {tool.name: tool.parameters for tool in registry._tool_manager.list_tools()}


def _legacy_registry(bridge: Bridge, workspaces: NativeWorkspaces) -> FastMCP:
    from .mcp_surface import complete_annotations

    raster = RasterWorkspace(bridge.root)
    projects = ProjectWorkspaces(bridge, workspaces)
    legacy = FastMCP("MapGIS 6.7 本机原生文件工具")
    _register_legacy_tools(legacy, bridge, raster, workspaces, projects)
    complete_annotations(legacy)
    return legacy


def build_server(root: str | Path, surface: Literal['legacy', 'compact', 'hybrid'] | None = None) -> FastMCP:
    from .mcp_surface import (StructuredFastMCP, register_modern_tools,
                              register_resources_and_prompts, select_surface)

    surface = select_surface(surface)
    bridge = Bridge(root)
    legacy = _legacy_registry(bridge, NativeWorkspaces(bridge))
    if surface == 'legacy':
        server = legacy
    else:
        server = StructuredFastMCP(legacy.name)
        register_modern_tools(server, legacy, bridge, surface)
    register_resources_and_prompts(server, legacy, bridge.root, surface)
    return server


_AUTHORITY = re.compile(r'(\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::([0-9]{1,5}))?')
_HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def _host_name(value: str) -> str:
    try:
        return str(ip_address(value))
    except ValueError:
        name = value.lower()
        if len(name) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in name.split('.')):
            return name
        raise ValueError('Invalid HTTP hostname') from None


def _loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def validate_streamable_http_config(host: str, allow_non_loopback: bool = False) -> str:
    """Normalize a bind address and require explicit opt-in outside loopback."""
    if '%' in host:
        raise ValueError('HTTP bind addresses must omit interface scope identifiers')
    if host.startswith('[') and host.endswith(']'):
        host = str(IPv6Address(host[1:-1]))
    host = _host_name(host)
    if not _loopback(host) and not allow_non_loopback:
        raise ValueError('Streamable HTTP requires a loopback host or --allow-non-loopback')
    return host


def _authority(value: str | None) -> tuple[str, int] | None:
    match = _AUTHORITY.fullmatch(value or '')
    if match is None:
        return None
    host, port = match.groups()
    try:
        name = str(IPv6Address(host[1:-1])) if host.startswith('[') else _host_name(host)
    except ValueError:
        return None
    port = int(port) if port is not None else 80
    return (name, port) if 0 < port <= 65535 else None


def validate_http_request_headers(host_header: str | None, origin_header: str | None,
                                  allowed_hosts: Collection[str]) -> bool:
    """Accept an exact HTTP authority and, when supplied, a same-origin browser request."""
    authority = _authority(host_header)
    if authority is None or authority not in {_authority(value) for value in allowed_hosts}:
        return False
    if origin_header is None:
        return True
    return origin_header.startswith('http://') and _authority(origin_header[7:]) == authority


def streamable_http_app(server: FastMCP, host: str, port: int, *, allow_non_loopback: bool = False):
    """Wrap the SDK's HTTP endpoint with exact authority and Origin checks."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import PlainTextResponse

    host = validate_streamable_http_config(host, allow_non_loopback)
    if not 0 < port <= 65535:
        raise ValueError('HTTP port must be between 1 and 65535')
    names = {host}
    if _loopback(host) or host in {'0.0.0.0', '::'}:
        names.update({'localhost', '127.0.0.1', '::1'})
    authorities = (f'[{name}]' if ':' in name else name for name in names)
    allowed_hosts = {f'{authority}:{port}' for authority in authorities}
    server.settings.json_response = True
    # The outer ASGI guard also rejects duplicate headers and cross-alias Origins.
    # The SDK still validates Content-Type and enforces its request body limit.
    server.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope['type'] == 'http':
            hosts = [value.decode('latin-1') for key, value in scope['headers'] if key.lower() == b'host']
            origins = [value.decode('latin-1') for key, value in scope['headers'] if key.lower() == b'origin']
            if len(hosts) != 1 or not validate_http_request_headers(hosts[0], None, allowed_hosts):
                await PlainTextResponse('Invalid Host header', status_code=421)(scope, receive, send)
                return
            if origins and (len(origins) != 1 or not validate_http_request_headers(hosts[0], origins[0], allowed_hosts)):
                await PlainTextResponse('Invalid Origin header', status_code=403)(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


async def _run_guarded_stdio(server, protocol_output):
    import anyio
    from mcp.server.stdio import stdio_server

    protocol = server._mcp_server
    async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (read, write):
        await protocol.run(read, write, protocol.create_initialization_options())


def main(*, default_root: Path | None = None, protocol_output=None):
    from .mcp_surface import SURFACES

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=default_root is None, default=default_root)
    parser.add_argument("--surface", choices=SURFACES)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-non-loopback", action="store_true")
    args = parser.parse_args()
    if args.transport == 'streamable-http':
        import uvicorn

        try:
            host = validate_streamable_http_config(args.host, args.allow_non_loopback)
        except ValueError as exc:
            parser.error(str(exc))
        if not 0 < args.port <= 65535:
            parser.error('--port must be between 1 and 65535')
        server = build_server(args.root, surface=args.surface)
        app = streamable_http_app(server, host, args.port, allow_non_loopback=args.allow_non_loopback)
        uvicorn.run(app, host=host, port=args.port, proxy_headers=False)
    else:
        server = build_server(args.root, surface=args.surface)
        if protocol_output is None:
            server.run(transport='stdio')
        else:
            import anyio

            anyio.run(_run_guarded_stdio, server, protocol_output)
