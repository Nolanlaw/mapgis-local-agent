# 本机 MapGIS6.7 的 Agent 调用入口

本次范围是当前安装的6.7、非服务器功能、全程由Agent执行。已有SDK/COM/文件工具直接使用；没有专用接口的原厂操作，通过`mapgis_private_control`在新建私有进程中观察菜单、填写参数、操作画布、保存和导出。用户已授权这个后端；本项目开关已启用。运行不需要人工点击，也不占用用户桌面的键鼠或前台。

当前源码为123个工具、158项操作说明。这是加载版本的校验信息。操作发现、原厂帮助、运行观察和结果读回各有用途；一个模块的代表流程通过，不表示每种参数组合都已实测。

## 各入口如何调用

下表对照本机启动器的20个作业分组。其中数据库服务器分组按用户要求排除；三维分组的解包壳缺少载荷，改用本机实际存在的原厂三维子模块和既有SDK。`private`列是`mapgis_private_control.open`的module值。

| 本机功能分组 | private | 可复用的专用通路及本次实际结果 |
|---|---|---|
| 数字测量、测图 | `survey` | 原厂新建SUV、录入控制点、保存、全新进程重开；TXT读回东200、北100、高程50 |
| 图形编辑 | `editor` | 原厂SDK组合调用、点线区及属性/样式编辑、持续工作区、保存/恢复；其余菜单和控件走私有进程 |
| 输出 | `output` | 原厂EPS、MPJ出图及PDF/TIFF/JPEG；原厂其他本机参数走私有进程 |
| 文件转换 | `conversion` | 原生文件、DXF、SHP/GPKG/GeoJSON等既有交换工具；原厂转换对话框可调用 |
| 数据库服务器配置 | 排除 | SQL Server、远程空间库和相关服务器管理不做；已有SQLite/Access/DBF本地文件工具保留 |
| 属性管理 | `attributes` | 主/辅助属性、字段、计算、关联、分组与读回；其他原厂管理控件可调用 |
| 分幅图库 | `library` | 原厂新建DBS、保存、新进程重开、导出WP；独立BAS60确认1个500×500索引区 |
| 影像库 | `image_library` | 原厂MSD/MSI创建、编辑、保存与独立重开；管理界面可调用 |
| 空间分析 | `analysis` | 既有叠加、拓扑、查询、统计等接口；其余原厂菜单和参数可观察调用 |
| 数字地面模型 | `terrain` | 原厂GRD/TIN存读、节点/约束编辑、等值线、剖面等工具；原厂参数可调用 |
| 网络输入编辑 | `network_legacy` | 原厂录入3个折点、保存WN、新进程重开、导出WL；独立读回全部坐标一致 |
| 网络分析 | `network` | 已有路径、连通、可达分析；原厂编辑分析界面可调用 |
| 影像处理 | `raster` | 原厂MSI数值/GCP/金字塔、影像运算和校正；原厂处理界面可调用 |
| 三维地形、场景、飞行 | `terrain_flight` | 实际原厂DTM3DFly已启动、装入GRD、读取模型/场景/飞行/模型应用菜单；成果导出优先原厂DEM/TIN场景和飞行SDK |
| 高程库 | `dem_library` | 原厂GRD入库，生成Inf/Hdr/Dem；新进程按目录树重开原生数据层 |
| 误差校正 | `correction` | 已有向量/影像校正和独立检查点；其他原厂参数可调用 |
| 投影变换 | `projection` | 已有投影、坐标、图框/网格工具；原厂投影对话框可调用 |
| 报表 | `reports` | 原厂构表并保存BB，新进程重开、重存字节一致；原厂生成WL，独立读到163条线 |
| 裁剪 | `clip` | 已有矢量、图幅、CAD及工程裁剪；其他原厂菜单可调用 |
| 图像浏览 | `image_view` | 原厂MapSee窗口/菜单可调用；已接通文件读回和影像输出 |

上表把调用路线接到实际模块。五个原先没有核心路线的分组（测量、图库、高程库、网络编辑、报表）现在均有原厂保存和全新进程重开的成果。其余分组复用已有专用工具，并由统一私有界面机制提供原厂操作入口。

## Agent的执行顺序

1. `mapgis_find_operations`和`mapgis_describe_operation`发现专用接口。原厂GUI操作先调用`mapgis_private_control(action="policy")`取得精确模块；`mapgis_search_native_help`按module检索，再用topic_id读正文。
2. 在任务根目录配置现有合法安装与已授权开关。`open`声明输入文件、SHA256、依赖和输出绑定，创建新私有进程。输入先复制；程序、DLL、系统库、色表和环境也使用任务副本。
3. `observe`取得真实窗口、菜单、控件和一次性token；`act`执行当前观察允许的操作。菜单有时只切换工具，例如报表构表还需随后向画布发送两个角点。
4. 等待真实对话框/结果状态。保存或导出后核对落盘文件，再用原厂新进程或独立读取器检查结果。`POSTED_NOT_COMPLETION_PROOF`只表示消息已投递。
5. `close`正常关闭。无响应时可按授权显式`discard=true`收回这个自有作业；状态与回执用于确认实际结束。已有request_id先对账，不能因超时盲目重复编辑。

支持的act操作：`set_text`、`button`、`command`、`select`、`set_check`、`pointer`、`key`、`tree_items`、`tree`、`capture`。路径使用binding及可选相对member；树目录按已观察的item分页、展开和选择。画布消息使用客户区坐标；按键不改变系统全局状态。capture通过同一受限作业内PrintWindow重绘，只用于观察；原厂输出仍走保存/导出接口。

原有需要Sky的交互跟踪说明，在本机开关启用时会由describe_operation返回私有进程调用路线，同时保留原参考入口和原validation状态。

## 当前可用的命令行入口

开发目录执行：

```powershell
& '.venv/Scripts/python.exe' -s -B -X utf8 scripts/mapgis_cli.py --root '任务根目录' call --arguments '调用参数.json'
```

例如调用参数：

```json
{"tool":"mapgis_private_control","arguments":{"action":"open","module":"reports","session_id":"report-example","parameters":{"outputs":[{"id":"result","name":"result","directory":true}]}}}
```

后续调用沿用session_id，observe后用返回token和HWND执行当前操作。工具完整参数来自注册schema和工具说明。MCP的stdio入口是`scripts/launch_mcp.py`；旧聊天连接关闭时，同一注册表的CLI可以立即使用，无需人工操作MapGIS。

## 已确定的本机限制与处理

- `DTM6x.exe`的资源内是104字节AutoHotkey脚本，调用7z解包`_dtmsfx.exe`后退出。本安装没有该载荷。`terrain_3d`保留为这个启动器的诊断记录；实际三维功能使用`terrain_flight`及原厂场景SDK。安装和许可均未修改。
- 本次DTM3DFly的GUI场景MSI导出返回`MS_CANNOT_ALLOCMEM`，没有记为成功。三维成果采用已有原厂SDK通路；此前TIN飞行114帧全部独立读回、长路径容量及末帧重开证据在`work/automation-20261030/recovery-20261101/`。
- MapSuv生成的示例WT，其属性头声明的字段数/记录宽度与后续载荷不一致，独立读取器保持拒绝。原厂BAS60实际打开并确认1个有效点；SUV保存重开后的TXT坐标/高程读回通过。没有放宽通用属性一致性检查，也没有将这份WT记为完整属性验证成功。
- 原厂个别操作依赖外部设备、已有数据、字体/符号或许可。任务使用本机已存在资源和明确业务参数；不会新增服务器/设备部署或更改许可。原始数据和用户现有窗口保持。

## 实际证据

当前本机证据位于`evidence/release-070/`：

- `reports-native-result.json`、`library-native-result.json`
- `dem-library-native-result.json`、`network-native-result.json`、`survey-native-result.json`
- `private-control-checks.json`：统一控件机制、中文树目录、输入桌面不变、请求/窗口归属与结束检查
- `dtm-native-result.json`：解包壳诊断、实际三维子模块调用及GUI导出错误
- `agent-cli-01/result.json`：正式CLI调用原厂SDK修改、保存、不同进程读回

当前交付包、恢复验证和清理记录由本机交付目录README索引。历史生态调查和旧基线表保留为参考，不再用其条数计算本目标剩余量。
