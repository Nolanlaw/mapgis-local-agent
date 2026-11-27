# MapGIS MCP 3.0.0 交付报告

v3.0 将同一套本机 MapGIS 文件操作、计划执行和发布账本接入 Python SDK、现代 CLI、stdio 与 Streamable HTTP MCP，以及独立 Agent Skill。未指定表面的 `build_server(root)` 沿用 `legacy`。独立 Skill、SDK、便携配置和 MCPB 选用 `hybrid`。

## Sprint 1–5 交付与实测记录

| 阶段 | 交付 | 可追溯的实测结果 |
| --- | --- | --- |
| Sprint 1 | CAS、请求账本、结构化错误、操作发现、持久任务和 MCP 表面 | CAS 实验写入 1,073,741,825 字节对象，超过 SQLite 的 1,000,000,000 字节单对象上限。账本 28,672 字节，发布状态 `APPLIED`，输出摘要和原件摘要核对通过。首轮工具表面 `123 / 14 / 20`，目录搜索 Top-5 为 `30/30`。[CAS 回执](../work/v3-sprint1/large-cas.log) · [表面回执](../work/v3-sprint1/surface-benchmark.json) |
| Sprint 2 | MapPlan IR、DRC/LVS、标签避让、视觉预览、FDR、ABI 和 Spike E 进程池 | 工具表面 `123 / 15 / 21`，Top-5 为 `35/35`。三种进程策略的 A→B→A 全部通过各 12 项隔离断言，报告 `violations=[]`。[发现回执](../work/v3-sprint2/surface-benchmark.json) · [进程池回执](../work/v3-sprint2/worker-benchmark.json) |
| Sprint 3 | 六库 COM、现代 CRS 伴生文件、`section V5`、DBS、钻孔剖面与 L2 | 六库各至少有一条真实调用成功，共 `19/21` 个 COM 场景发布。`EPSG:4490/4528/4549` 三个 WT 通过 BAS60 新进程重开。钻孔旗舰切片复用了 `bind` 和 `geometry` 两个节点，继续 `publish`，原厂 `section V5` 实际打开了项目。[COM](../work/v3-sprint3/com6-verification.json) · [CRS](../work/v3-sprint3/cgcs2000-probe.json) · [剖面](../work/v3-sprint3/flagship-geology-slice/flagship-receipt.json) |
| Sprint 4 | 跨软件交接、提供方探测、MapGIS-Bench 与热点重构 | 同一组 30 任务，`legacy` 通过 `10/30`，`compact` 与 `hybrid` 各通过 `30/30`。安全检查分别通过 `2/4`、`4/4`、`4/4`。三处热点最大圈复杂度分别从 `234→32`、`134→38`、`81→46`，最高降 `86.32%`。末端统计包含抽出的辅助模块。[配对回执](../work/v3-sprint4/mapgis-bench-summary.md) · [复杂度](../work/v3-sprint4/complexity-report.json) |
| Sprint 5 | SDK/CLI/双传输 MCP/Skill、SPDX、Windows 完整与核心分享包核算、Registry 和 MCPB | 官方 Schema 验证通过。stdio 中主动注入 Python `print` 和 fd 1 诊断，消息进入 stderr，客户端完成初始化和 `tools/list`。HTTP 通过同源初始化、会话和工具枚举，非法 Host、重复 Host 和跨站 Origin 均被拦截。SPDX 盘点 65 个安装分发，记录 84 个包，未解析许可数为 `0`。[SPDX](../packaging/sbom.spdx.json) · [解析回执](../work/v3-sprint5/sbom-summary.json) |

### Sprint 5 MCP 表面实测

| 表面 | 工具数 | `tools/list` 字节 | 相对 legacy 减少 |
| --- | ---: | ---: | ---: |
| `legacy` | 123 | 259,909 | — |
| `compact` | 15 | 9,647 | 96.29% |
| `hybrid` | 21 | 14,540 | 94.41% |

当前目录查询 Top-5 为 **48/48**，本轮中位 `0.7196 ms`，P95 `0.9494 ms`，每个查询取五次调用的中位数。三个表面的工具均带注解，各有 2 个资源、2 个资源模板和 5 个提示。[Sprint 5 表面回执](../work/v3-sprint5/surface-benchmark.json)

### 原生性能、CRS 与 L2 的验证口径

- 进程池中位读取时间为一次性进程 `881.919 ms`、粘滞进程 `729.373 ms`、预热池 `11.619 ms`，每种 12 次。预热耗时 `1,407.048 ms`，单次计时排除预热及独立事后读回。A→B→A 的 B 步，同一输入在一次性进程为 `765.638 ms`，预热池为 `183.399 ms`。记录中约 `870 ms` 的 `869.587 ms` 属于一次性 A 读取，未拿它与 B 步作配对。工作区编辑后退役原子进程，A2 可重新启动，回执记录原件摘要、物理 ID 和输出字节检查。[原始进程池回执](../work/v3-sprint2/worker-benchmark.json)
- COM 成功库为 `MapGisBasCom1.DLL`、`proj60.dll`、`AttCom.dll`、`MapGisMapCom.dll`、`MapgisLayerCom.dll`、`MapGisDspCom.dll`。`proj60` 读回 Beijing54、Xi'an 80 和 WGS84 椭球参数。CGCS2000 使用 `mapgis-crs-sidecar/v1` 将 WKT2 与向量 SHA-256 绑定。120°E 同一中央经线下，`EPSG:4528` 为 `40,500,000 m` 假东距，`EPSG:4549` 为 `500,000 m`。混用触发 `AUDIT_FALSE_EASTING`，原厂绘图原点偏移与 EPSG 假东距分别保存。[COM 回执](../work/v3-sprint3/com6-verification.json) · [伴生文件回执](../work/v3-sprint3/cgcs2000-probe.json)
- `section V5` 的原厂窗口在独立进程中观察。打开对话框与撤销对话框有状态转换回执，三步项目打开宏从菜单进入对话框、绑定任务 MPJ、核对原厂文档。旗舰工程发布了可编辑的原生剖面和柱状图，检查含最小曲率钻孔轨迹、坐标和出图预览。[对话框回执](../work/v3-sprint3/section-v5-macro-probe.json) · [旗舰回执](../work/v3-sprint3/flagship-geology-slice/flagship-receipt.json)
- IDA 9.1 `idalib` 实际分析成功，工具产品版本 `9.1.25.226`。L2 静态审计覆盖 9 个 DLL 的 1,929 个导出。原厂 `read_4x.dll` 读回 8 项、`RdWt_5x.dll` 读回 17 项，两次分别记录 0 个新窗口、0 个弹窗事件及输入摘要保持。其余导出的证据范围为静态审计、受限路由或单独探测。[IDA](../work/v3-sprint3/ida-probe-receipt.json) · [导出审计](../work/v3-sprint3/l2-exports-audit.json) · [运行回执](../work/v3-sprint3/l2-runtime-receipt.json)

## 启动与接入

```powershell
& '.venv/Scripts/python.exe' -s -B -X utf8 scripts/mapgis_cli.py discover '拓扑' --root '<任务目录>'
& '.venv/Scripts/python.exe' -s -B -X utf8 scripts/mapgis_cli.py setup-snippet --client auto --surface hybrid --root '<任务目录>'
& '.venv/Scripts/python.exe' -s -B -X utf8 scripts/launch_mcp.py --root '<任务目录>' --surface hybrid --transport streamable-http
```

Streamable HTTP 默认在 `http://127.0.0.1:8765/mcp`。非回环绑定需要明确指定 `--allow-non-loopback`，仅适用于有访问控制的受信环境。服务按绑定地址和端口匹配 Host，浏览器 Origin 必须与该请求的 HTTP authority 同源。`setup-snippet` 只输出 JSON。Claude、Cursor、Antigravity 与 `auto` 输出 `mcpServers`。Codex 输出 `mcp_servers` 的 JSON 表示，可将其中的 `command` 和 `args` 写入 Codex TOML 的 `[mcp_servers.mapgis_local]`。[MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) · [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

从 `mapgis_agent` 导入 `MapGISClient`、`MapGISConfig`、`Workspace` 和 `MapPlan`。SDK 的 `execute` 调用同一操作注册表，`async_job=True` 使用持久任务账本，可通过 `mapgis_job_manage` 查询。`MapPlan(...).add_node(...).to_spec()` 输出有序的 `mapgis-plan/v1`。`with client.open_workspace(...) as ws:` 退出时显式关闭原生缓冲区并丢弃未保存修改，发布前请调用 `ws.save(request_id=...)`。独立安装入口见 [Agent Skill](../packaging/skill/SKILL.md)。

MCPB 使用清单选择的本机 Python 与独立任务目录。可用 `scripts/package_share.py --mcpb-output <路径.mcpb>` 生成扩展，包内含 launcher、MapGIS 源码、图标、随附第三方声明和工作流。Python 依赖由所选 checkout 或离线分享包提供。`server.json` 是待发布的 Registry 元数据，可用于审阅与有权发布者的后续流程。[Registry 清单](../server.json) · [MCPB 清单](../packaging/mcpb/manifest.json)

## 许可和离线包体积

开发环境中扫描到 67 个分发。排除分析用 `pefile`，将连接器作为独立 Apache-2.0 根包并解析其余 65 个分发。保留软件包自身许可和已识别的内嵌原生库记录。`winhlp` 通过独立索引脚本使用，以 GPL-2.0 源码及原通知随带。Ghostscript 通过独立 CLI 使用。完整的标准 Apache-2.0 文本位于 `LICENSE`，连接器的适用范围移至 `NOTICE.md`。

`--split-sources` 将原打包器固定的 `third_party/sources/` 文件移入 `OpenSource-Compliance-Sources.zip`。主包 `THIRD-PARTY.json` 和 `sbom.spdx.json` 绑定伴生包文件名和 SHA-256，并记录随二进制一起交付和保留许可的要求。`core` 排除 OpenCV、PyFlwDir、Numba 和 llvmlite。依赖 OpenCV 或共享模块顶层导入的扫描、图像和部分原生制图/地形例程需要 `full`。两种包保留各自的 `rasterio.libs` 和 `pyogrio.libs`，实测 GDAL 版本分别为 `3.10.3` 和 `3.12.4`。

体积以 [Sprint 5 预算回执](../work/v3-sprint5/packaging-budget.json) 的文件级实测为准。预算基于当前源树、`.venv`、Ghostscript 与现有源码归档。完整发行仍需固定 CPython 运行时归档与其余源码输入。当前源码清单覆盖 Ghostscript、libvips 和 GEOS 3.14.1 的固定部分。Rasterio wheel 另带 GEOS 3.13.0，OpenCV wheel 还带 FFmpeg。随包声明保留这些组件的边界，完整再分发前需核对其对应源码条件。[GEOS LGPL-2.1 文本](https://raw.githubusercontent.com/libgeos/geos/3.14.1/COPYING) · [OpenCV wheel 上游许可](https://github.com/opencv/opencv-python/blob/4.x/README.md#licensing)

| 当前在场文件的主包变动 | 未压缩减少字节 | ZIP 估算减少字节 |
| --- | ---: | ---: |
| `full` → `split-sources` | 5,724,440 | 5,686,859 |
| `split-sources` → `core-split` | 247,788,196 | 84,926,716 |

在场的两个固定源码归档移入伴生包，未压缩共 `5,724,440` 字节，ZIP 估算为 `5,686,829` 字节。另有 22 个固定源码输入未出现在当前可测路径，其中 20 个在项目锁定清单中列明大小，共 `127,789,481` 字节。上表使用实际 DEFLATE 6 级与 ZIP 头开销，归档生成时添加的清单和启动器另计。[ZIP 预算与缺项明细](../work/v3-sprint5/packaging-budget.json)

## 已知运行边界

- `AttCom` 的对象与链接等场景已通过。`attribute-null` 的失败回执实际归属 `MapGisBasCom1.Field`，`HRESULT 0x80020009`，内部异常为 `0x80020005`。`MapGisMapCom` 的 `legend` 超时，同库其余已列场景通过。[COM 回执](../work/v3-sprint3/com6-verification.json)
- `NetSolver` 与 `n60_oper` 的组合预检观察到原厂 `#32770` 对话框，记录 8 个新窗口和 7 个弹窗事件。受限 L2 路由使用经确认的构建与入口。`MapSuvL67` 仍待补实样。本轮 `section V5` 证据覆盖选定项目打开和剖面切片。[弹窗回执](../work/v3-sprint3/l2-preflight/window-observation.json)
- MapGIS 10.x 仍按未安装且未实现的提供方报告。`DesktopProbe.exe` 属于可选的外部观察条件。文件工具、独立私有原生缓冲和外部用户窗口各自保有不同的授权与验收入口。[本机接口](NATIVE.md)

## Sprint 5 验收入口

以下 Python 命令均以 `& '.venv/Scripts/python.exe' -s -B -X utf8` 执行。

| 命令参数 | 退出码 | 观测与回执 |
| --- | ---: | --- |
| `scripts/generate_sbom.py --output packaging/sbom.spdx.json --summary work/v3-sprint5/sbom-summary.json` | 0 | 65 个安装分发，84 个 SPDX 组件，许可未解析数 0。[摘要](../work/v3-sprint5/sbom-summary.json) |
| `scripts/package_share.py --dry-run-budget work/v3-sprint5/packaging-budget.json` | 0 | 实测三种布局及两份独立 GDAL DLL。[输出](../work/v3-sprint5/packaging-budget.log) |
| `scripts/self_test.py` | 0 | 原始 123 工具、46 次成功调用、原件与恢复字节验证通过。[输出](../work/v3-sprint5/self_test.log) |
| `scripts/self_test_native.py` | 0 | 67 次调用，真实 WT/WL/WP 重开、EPS 渲染和中断恢复通过。[输出](../work/v3-sprint5/self_test_native.log) |
| `scripts/self_test_workspace.py` | 0 | 8 次成功调用，持久原生 PID、修订冲突、保存、丢弃和恢复通过。[输出](../work/v3-sprint5/self_test_workspace.log) |
| `scripts/benchmark_surfaces.py --output work/v3-sprint5/surface-benchmark.json` | 0 | `123 / 15 / 21`，Top-5 `48/48`。[输出](../work/v3-sprint5/surface-benchmark.log) |
| `scripts/portable.py doctor` | 0 | `FILE_TOOLS_READY`、ABI Tier A `EXACT_CERTIFIED`、3 个提供方。[摘要](../work/v3-sprint5/portable-doctor.log) |

另以真实 SDK 上下文移动并保存 WT 记录，读回 `(12.5, 13.75)`，上下文退出丢弃后续未保存删除，原始摘要保持且自有原生 PID 已退出。[SDK 工作区回执](../work/v3-sprint5/sdk-native-workspace.json)

定向回归使用 `-m pytest @files -q`，`@files` 来自完整 [29 个测试文件清单](../work/v3-sprint5/regression-files.txt)。退出码 `0`，`267 passed, 1 skipped`，现有 `pwsh.exe` 变体因本机没有 PowerShell 7 跳过。测试包含官方格式 Schema、MCPB 解包启动、来源伴生 ZIP 双向摘要、Win32 核心配置和 MapGIS-Bench 文件与事务冒烟。全仓回归由主 Agent 在本 Sprint 之后执行。[定向回归日志](../work/v3-sprint5/targeted-pytest.log)

本机构建了 `mapgis_local_agent-3.0.0-py3-none-any.whl`，解包核对 186 个成员、Apache-2.0 元数据、五个 extras 和 stdio 注册入口。CI 的 GPL 守卫核对到独立索引进程以外的 `winhlp` 直接导入为 `0`。[Wheel 核验](../work/v3-sprint5/wheel-readback.log) · [GPL 守卫](../work/v3-sprint5/winhlp-isolation.log)
