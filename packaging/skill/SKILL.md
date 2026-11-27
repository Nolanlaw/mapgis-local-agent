---
name: mapgis-local-agent
description: Use the MapGIS MCP server for saved WT/WL/WP/MPJ, geologic cartography, native MapGIS 6.7 operation discovery, reproducible MapPlan transactions, DRC, drillhole sections, and GIS interchange. 适用于地质图制图、原生文件、拓扑、图例、分幅、剖面和跨软件成果交接。
license: Apache-2.0
compatibility: Requires a configured MapGIS Local Agent v3 MCP server. Licensed native operations require supported Windows x86 MapGIS 6.7 modules running through the isolated host.
---

# MapGIS Local Agent

Install this file as `mapgis-local-agent/SKILL.md` in the host's skills directory. Connect a local MCP server using `mapgis-agent setup-snippet --client auto --surface hybrid`. In a checkout use `scripts/mapgis_cli.py` with its Python instead of the installed command. Choose a task root and keep inputs and outputs traceable to that root. Use stdio or local Streamable HTTP at `http://127.0.0.1:8765/mcp`.

Start with **hybrid (21 tools)**. Call `mapgis_discover` with a geological task or `operation_id`, read the returned schema and `execution_status`, then use the suggested direct tool or `mapgis_execute`. Use `mapgis_inspect` for registered read-only operations. `mapgis_environment` and `mapgis_probe_abi` via the dispatcher establish whether a native route is available. Ask for saved inputs when a route requires an external desktop observation.

## Edit and publish

1. Inspect the saved source and bind its SHA-256, coordinate frame, physical record IDs and a stable request ID. Make an owned work copy for edits.
2. Prepare a candidate with the selected operation. Review its diff, topology, attributes, geometry and any required native readback. Keep the request ID and review the publisher epoch with `mapgis_tx_manage(action="current_epoch")`.
3. Publish the accepted candidate with `mapgis_tx_commit(request_id, expected_epoch)`. Read back the saved output and compare it against the bound input and candidate. After interruption, query `mapgis_tx_manage(action="status")` or use the appropriate recovery route before replaying a request.

Use `mapgis_workspace` for an agent-owned native buffer with `expected_revision`. Save explicitly before closing or explicitly discard the unsaved changes. Native record IDs describe physical slots and may differ from business IDs.

## Maps and plans

- Submit `mapgis-plan/v1` MapPlan IR via `mapgis_run_plan`. Declare `crs`, `unit`, `scale`, each `node.id` and `op`, and each ordered input role. Bind source files by SHA-256 or bind a prior `from_node` and `output_key`. Preserve repeated roles and bottom-to-top layer order. Use `plan` and `diff` before `apply` and `resume` to reuse verified CAS outputs after an interruption.
- Open `mapgis://standards/cartography` and use `mapgis_audit_check` to check the implemented rules drawn from GB/T 958-2015 (regional map legends) and DZ/T 0179-2025 (geological map colours at 1:50,000–1:250,000). Review source geology, symbol semantics and unresolved exceptions before final delivery. [GB/T 958-2015](https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D80B51D3A7E05397BE0A0AB82A) · [DZ/T 0179-2025](https://www.ndls.org.cn/standard/detail/5f981a8c4c23427569015cfc43372e7a)
- `EPSG:4528` uses CGCS2000, the 120°E central meridian and **40,500,000 m** false easting. The corresponding central-meridian formulation uses **500,000 m**. Stop if layers mix these frames. Attach WKT2 to the exact saved vector SHA-256 and keep raw MapGIS drawing origin and EPSG false easting distinct. [EPSG:4528 definition](https://opengeospatial.github.io/ontology-crs/data/def/crs/EPSG/0/4528/index.html)
- For interchange, discover `mapgis_interchange_export` and `mapgis_interchange_verify`. Deliver the checked package with its coordinate, layer-order and attribute information.

## Native execution

- Use the typed `mapgis_sdk_flow` with the current allowlist before choosing `MapGisBasCom1`, `proj60`, `AttCom`, `MapGisMapCom`, `MapgisLayerCom` or `MapGisDspCom`. The six libraries have specific validated calls. `attribute-null` on `MapGisBasCom1.Field` and the `MapGisMapCom` legend case have recorded failures. Require an ABI-compatible installation and explicit local opt-in for the isolated x86 host.
- Use `mapgis_private_ui` for the authorized `section` module. Establish an owned `section V5` session, observe and bind the expected menu/control state, then run the bounded macro and verify the resulting native document or artifact. The three-step project-open macro checks the dialog, the bound input path and the opened document. Close the owned session after verifying outputs. Never attach it to the user's existing windows or send physical desktop input.
