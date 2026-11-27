# MapGIS Local Agent 3.0.0

让 AI Agent 通过本机 **MapGIS 6.7（非服务器模块）** 或无头开源引擎安全、可审计、可复现地完成专业地质制图与空间分析任务。统一封装为 **Python SDK / 现代 CLI / MCP（`stdio` + `Streamable HTTP`）/ 独立 Agent Skill** 四种接入形态，全程在项目自有隔离进程与私有桌面运行，**零原件覆写、零桌面键鼠抢占、零 Windows 注册表污染**。

> 本机 MapGIS 6.7 安装、合法许可、字体和系统库由使用者自行提供。完整实测回执与运行边界见 [v3.0.0 交付报告](docs/V3-DELIVERY-REPORT.md) 与 [本机调用范围](docs/LOCAL67-ROUTES.md)。

---

## v3.0.0 核心亮点

- **三态 MCP 协议面（Token 成本直降 94.4%–96.3%）**：
  - `hybrid`（**推荐默认**）：**21 个工具**（14,540 B / ~4,155 Token，较 `legacy` 123 工具 259,909 B 减少 **94.41%**），兼顾高频强类型直达与全目录派发；
  - `compact`：**15 个深模块工具**（9,647 B / ~2,757 Token，减少 **96.29%**）；
  - `legacy`：保留全部 **123 个原版工具**，100% 向后兼容旧工作流；
  - 三套表面均覆盖 100% `ToolAnnotations`、2 个静态 Resources、2 个 ResourceTemplates、5 个专业地学 Prompts；48 条中英地质同义词查询 Top-5 **48/48（100%）命中**（中位 0.72 ms）。
- **编译级安全与可复现执行引擎**：
  - **不可变 CAS 大对象存储**：实测写入与回读 **1,073,741,825 B** 单对象（突破 SQLite 1 GB 单值限制），配合单调递增 `publisher_epoch` Fencing 与单文件 `fsync` 原子提交；
  - **SQLite 持久异步任务机**：零外部中间件，服务重启后将未决 `RUNNING` 标记为 `UNCERTAIN_NEEDS_RECONCILE`，绝不盲目重放；
  - **声明式 `MapPlan IR`**：有序角色绑定 `CacheKey`（图层压盖顺序与连接左右侧严格保序）、静态 CRS/环路校验、故障注入断点续跑与 `.mpj` 反向导入；
  - **双模式脱敏 FDR 黑匣子**：导出 `.mapgis-fdr.zip` 取证包，自动脱敏绝对路径、敏感属性与坐标偏移并剥离 PNG EXIF 元数据。
- **地质行业质检与可视化闭环**：
  - **DRC / LVS 质检**：内置现行 **`GB/T 958-2015`** 与 **`DZ/T 0179-2025`** 地质制图规则包，检核悬挂点、自相交、狭长面、1:1 标识点基数、面积双重记账、图例-图面双向 LVS 一致性；
  - **视觉审图与自动注记避让**：零 GUI 生成全图 PNG、300 dpi DRC 违规局部放大裁剪图与像素差异热图；支持锁定已确认 ID 的可编辑原生 `.wt` 注记避让（贪心局部搜索 / 模拟退火可比）。
- **原生 6.7 深度突破与多软件互操作**：
  - **CGCS2000 & `EPSG:4528` 双重阻塞根治**：哈希绑定 `mapgis-crs-sidecar/v1` 伴生契约，无损保存 `EPSG:4490` / `4528` / `4549`，精准拦截 40,000,000 m 带号假东距混用；
  - **6/6 原厂无注册表 COM 库**：`MapGisBasCom1`、`proj60`、`AttCom`、`MapGisMapCom`、`MapgisLayerCom`、`MapGisDspCom` 全线接入，19/21 场景实测通过；
  - **自由工作室 `section V5` + UFO2 风格已验证 UI 宏**：支持最小曲率钻孔轨迹、视倾角与垂向夸大地质剖面及多道柱状图端到端成图，并经 `BAS60` 与私有桌面 `section V5` 打开验证；
  - **多图幅 DBS 接边工作流 & 三级原生进程池**：`A→B→A` 12 项跨环境零污染校验全通过（温热池只读中位 `11.6 ms`）；
  - **跨软件地学交接桥接**：一键导出并回读验证 ArcGIS Pro（GPKG）、Surpac（`.str` + 钻孔 CSV）、CorelDRAW（DXF + SVG）、QGIS（GPKG + QML）、GeoSciML-Lite，并附显式 **9 维保真/损耗声明矩阵**。
- **MapGIS-Bench 30 题评测 & 质量度量**：
  - `compact` 与 `hybrid` 在 30 道分层任务（基础/中级/专家各 10 题）上均达成 **30/30（100%）通过、4/4 安全红线全拦截**；
  - 全仓自动化测试 **`1,498 passed, 0 failed`**；SPDX 2.3 SBOM 覆盖 65 个依赖 / 84 个包，**0 个 `UNKNOWN` 许可**（[packaging/sbom.spdx.json](packaging/sbom.spdx.json)）。

---

## 快速安装与接入

### 方式一：一键生成客户端配置（Claude Code / Cursor / Antigravity / Codex）

从源码安装（需 Python 3.12+，仓库已附预编译 x86 `MapgisNativeWorker.exe`，常规使用无需安装 Visual Studio）：

```powershell
.\scripts\Setup.ps1 -MapGISInstallation 'D:\MapGIS67' -EnableNativeLegacyHost -EnablePrivateUi -Surface hybrid
```

> 仅使用无头文件/质检/转换功能可省略 `-MapGISInstallation` 及两个开关。`Setup.ps1` 会生成本地 `mcp-config.local.json`，不会修改你的全局客户端或系统设置。

也可以随时导出适配不同客户端的 `hybrid`（21 工具）配置 JSON 片段：

```powershell
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/mapgis_cli.py setup-snippet --client auto --surface hybrid --root 'D:\MapGIS-Agent'
```

- **Claude Code / Cursor / Antigravity**：将输出的 `mcpServers.mapgis_local` 合并到对应客户端 MCP 配置文件；
- **Codex CLI**：将 `--client codex` 输出的 `command` / `args` 写入 `~/.codex/config.toml` 的 `[mcp_servers.mapgis_local]`；
- **MCPB 桌面扩展**：前往 [GitHub Releases](https://github.com/Nolanlaw/mapgis-local-agent/releases/latest) 下载 `mapgis-local-agent-3.0.0.mcpb` 或自行打包 `scripts/package_share.py --mcpb-output <path.mcpb>`；
- **独立 Agent Skill**：见 [packaging/skill/SKILL.md](packaging/skill/SKILL.md)。

### 方式二：现代 CLI、Python SDK 与 Streamable HTTP

```powershell
# 现代 CLI：能力发现 / 地质质检 / 体检
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/mapgis_cli.py discover '拓扑造区' --root '<任务目录>'
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/mapgis_cli.py doctor --root '<任务目录>'

# 启动 Streamable HTTP（默认绑定 127.0.0.1:8765/mcp，内置 Host/Origin 校验）
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/launch_mcp.py --root '<任务目录>' --surface hybrid --transport streamable-http
```

```python
from mapgis_agent import MapGISClient, MapPlan

client = MapGISClient("D:/my-geology-task", surface="hybrid")
print(client.discover("钻孔剖面", limit=5))
print(client.detect_providers())
```

---

## 验证与基准

```powershell
# 核心自检
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/self_test.py
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/self_test_native.py
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/self_test_workspace.py

# MCP 三表面基准 & MapGIS-Bench 30 题配对评测 & SBOM 核验
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/benchmark_surfaces.py
.\.venv\Scripts\python.exe -s -B -X utf8 benchmarks/mapgis_bench/run_bench.py
.\.venv\Scripts\python.exe -s -B -X utf8 scripts/generate_sbom.py --output packaging/sbom.spdx.json
.\.venv\Scripts\python.exe -s -B -X utf8 -m pytest -q
```

---

## 已知运行边界（诚实声明）

- **原厂 6.7 COM / L2 边界**：`MapGisBasCom1.Field` 写入 NULL 返回 `HRESULT 0x80020009`；`MapGisMapCom.legend` 调用超时；`NetSolver`/`n60_oper` 组合底层调用触发原厂 `#32770` 弹窗，故不纳入无头直调；`MapSuvL67.exe` WT 异常头规范化器已通过注入样本验真，实机故障原样待补。
- **版本与架构边界**：`DTM6x.exe` 为缺载荷解包壳，三维地形走已验证的原厂场景 SDK 与 `DTM3DFly`；C/S 服务器数据库模块刻意排除；MapGIS 10.x 已预留 Provider 抽象契约（[src/mapgis_agent/providers/mapgis10_stub.py](src/mapgis_agent/providers/mapgis10_stub.py)），当前无真机运行时。
- **许可边界**：本项目原创代码采用标准 [Apache-2.0](LICENSE) 许可，按现状提供（As-is）；第三方开源组件（含独立子进程调用的 GPL-2.0 `winhlp`、独立 CLI 调用的 AGPL-3.0 `Ghostscript`、LGPL-3.0 `libvips`/`wellpathpy` 等）保留各自原始许可与源码合规机制，详见 [NOTICE.md](NOTICE.md)、[docs/THIRD-PARTY.md](docs/THIRD-PARTY.md) 与 [packaging/sbom.spdx.json](packaging/sbom.spdx.json)。

[v3.0.0 交付报告](docs/V3-DELIVERY-REPORT.md) · [能力说明](docs/CAPABILITIES.md) · [原生接口](docs/NATIVE.md) · [SDK 调用](docs/SDK-CALLS.md) · [安全策略](SECURITY.md) · [第三方来源与许可](docs/THIRD-PARTY.md) · [Apache-2.0 许可](LICENSE)
