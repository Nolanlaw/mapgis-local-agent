# MapGIS Local Agent

连接 AI 助手与 MapGIS 6.7 的本地工具。它提供 MCP 接口，用于查看工程状态、检查原生图层，并配合桌面操作完成编辑和出图。

[下载 Windows 版](https://github.com/Nolanlaw/mapgis-local-agent/releases/latest) · [使用说明](docs/SHARE.md) · [原生操作](docs/NATIVE.md)

## 功能

- 读取 `WT`、`WL`、`WP` 文件中的几何、属性和样式。
- 在副本上修改属性或样式，记录请求，并恢复指定修改。
- 查看运行中 MapGIS 的工程、图层、选区和未保存状态。
- 按窗口处理扫描大图，拟合配准、提取待复核候选，建立新的点线区。
- 使用 GEOS/PROJ 完成造区、裁剪、缓冲、简化、分类和坐标转换。
- 配合支持 Sky 的 Agent，在 MapGIS 中完成几何编辑、保存及 EPS/TIFF 出图。
- 按任务发现操作，检索本机各模块的原生帮助，读取参数、前提和结果检查。

文件中的对象使用**物理记录编号**定位；属性字段里的 ID 可以重复，不等同于记录编号。

## 快速开始

1. 在 [Releases](https://github.com/Nolanlaw/mapgis-local-agent/releases/latest) 下载 `MapGIS-Agent-0.4.0-Windows-x64-Share.zip`，完整解压到可写目录。
2. 双击 `01-Setup.cmd`，输入本机 MapGIS 安装路径。只使用文件工具时可直接回车。
3. 双击 `02-SelfTest.cmd`，确认结果为 `PASS`。`03-Diagnostics.cmd` 可检查依赖和 MapGIS 版本。
4. 在支持电脑操作的 Agent 宿主中打开解压后的文件夹，提供工程路径和任务。普通 MCP 客户端可以加载安装时生成的 `mcp-config.local.json`。

分享包自带 Python 和运行依赖。GitHub 自动生成的 `Source code` 压缩包只包含开发源码。

## 使用示例

打开 MapGIS 工程后，可以让 Agent 执行：

> 查看当前工程有哪些图层，以及选中了哪些区。

也可以在解压目录打开 PowerShell，查看工具和环境信息：

```powershell
.\MapGIS-CLI.cmd capabilities
```

更详细的操作步骤见[使用说明](docs/SHARE.md)和[原生操作](docs/NATIVE.md)。

## 环境要求

- Windows 10/11 x64。文件工具可在没有 MapGIS 的情况下运行。
- 原生编辑和出图需要 MapGIS 6.7、有效许可、工程使用的字体与符号库，以及提供 Sky 电脑操作能力的 Agent 宿主。
- 实时状态读取目前匹配 MapGIS 6.7 build 051118 的四个程序模块。运行 `03-Diagnostics.cmd` 可查看是否匹配；其他构建需要重新适配。

本机已验证多选、几何增删、保存重开、扫描图可编辑草稿与原生 EPS/TIFF 出图；另有投影/图框、传统 TIN/格网/等值线/体积、点缓冲样本。39 项整理入口之外，还可检索安装模块的帮助。详见[功能与限制](docs/CAPABILITIES.md)。其他实体电脑的完整原生流程，以及从任意地质图图片自动重建工程，尚未验证。详见[验证记录](https://github.com/Nolanlaw/mapgis-local-agent/blob/main/docs/VALIDATION.md)。

## 从源码运行

准备 Python 3.12 或更新版本，然后在项目目录运行：

```powershell
.\scripts\Setup.ps1 -PythonPath "C:\Python312\python.exe"
```

把示例路径替换成自己的 Python 路径。需要绑定 MapGIS 时，可追加 `-MapGISInstallation` 参数，指向包含 `program\mdiedit6x.exe` 的安装目录。

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test .\tests\native_session.test.mjs
```

## 依赖与许可

原生格式读写复用 [mapgis2shp](https://github.com/leecugb/mapgis2shp)，MCP 服务使用 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。扫描处理复用 GeoScan、Stratigraphic Amenity、libvips、GDAL、GEOS 和 PROJ；本机帮助索引复用单独的 winhlp 工具。来源与许可证见 [NOTICE.md](NOTICE.md) 和[第三方组件](docs/THIRD-PARTY.md)。

当前仓库为私有仓库。通过 GitHub 下载需要仓库读取权限；也可以直接转发原始分享包。
