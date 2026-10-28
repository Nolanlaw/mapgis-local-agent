# Windows 分享版使用说明

分享包适用于 Windows 10/11 x64，自带 Python 和运行依赖。解压后不需要另装 Python。

## 安装与自检

1. 将压缩包完整解压到可写目录。
2. 双击 `01-Setup.cmd`，输入 MapGIS 安装根目录。也可以输入 `program` 目录或 `mdiedit6x.exe` 的完整路径；只使用文件工具时直接回车。
3. 双击 `02-SelfTest.cmd`，确认结果为 `PASS`。该自检使用合成文件检查 MCP 服务、文件读写和恢复，不会启动 MapGIS。
4. 双击 `03-Diagnostics.cmd` 查看运行环境和 MapGIS 版本匹配情况。

安装后会在解压目录生成 `mcp-config.local.json` 和 `setup-state.local.json`；填写 MapGIS 路径时还会生成 `config.local.json`。移动文件夹后重新运行 `01-Setup.cmd`，以更新客户端路径。手动修改过的客户端配置会被保留，需要核对后再重新配置。

## 在 Agent 中使用

在支持本地文件、终端和电脑操作的 Agent 宿主中打开解压目录，先让 Agent 阅读 `AGENTS.md` 和 `docs/NATIVE.md`，再提供工程路径与任务。例如：

> 查看当前 MapGIS 工程的图层和选中的区。

普通 MCP 客户端可以加载安装时生成的 `mcp-config.local.json`。桌面编辑和出图还需要宿主提供 Sky 电脑操作能力；分享包不包含 Agent 宿主或模型。

## 命令行

在解压目录打开 PowerShell，运行：

```powershell
.\MapGIS-CLI.cmd capabilities
```

`MapGIS-MCP.cmd` 是 stdio 服务入口，通常由 MCP 客户端启动。直接双击后它会等待客户端输入。

自行调用包内 Python 时，使用以下参数，避免加载用户目录中的 Python 包：

```powershell
.\runtime\python\python.exe -s -B -X utf8 .\scripts\mapgis_cli.py capabilities
```

## MapGIS 兼容性

文件工具可以在没有 MapGIS 的情况下使用。原生编辑、保存和出图需要 MapGIS 6.7、有效许可，以及工程所用的字体和符号库。

实时读取工程、选区和未保存状态，目前匹配 build 051118 的四个程序模块。`03-Diagnostics.cmd` 显示 `MATCH` 表示模块指纹匹配；`UNSUPPORTED_BUILD` 表示需要重新适配。许可和 Sky 宿主是否可用仍需分别确认。

出图前使用 `scripts/Prepare-NativeOutput.ps1` 将工程及其显式依赖暂存到短 ASCII 路径，再在 MapGIS 中核对加载的图层和输出文件。步骤见 `docs/NATIVE.md`。

## 数据与恢复

保留原始文件，在副本上编辑。文件工具使用物理记录编号定位对象，属性字段中的 ID 可以重复。恢复操作需要基线和请求记录，并会检查目标字段是否有后续冲突；它不能恢复崩溃前任意未保存的内存内容。

扫描图流程可生成可编辑点线区草稿；地层、花纹、文字和符号的解释需要复核。可以明确使用本地演示坐标，不能把它当作真实地理精度。完整范围与已知问题见 `docs/CAPABILITIES.md`。

## 第三方组件

Ghostscript 位于 `runtime/ghostscript/`，可将原生 EPS 转换为 PDF。对应源码、GEOS 源码及许可证随包保留；组件清单见 `THIRD-PARTY.json`，原生格式库源码位于 `vendor/mapgis2shp/`。

分享包包含 Rasterio 使用的 GDAL 栅格运行时；没有附带可选的 pyogrio 通用矢量转换引擎。需要这些功能的开发者可按 `requirements.lock.txt` 准备开发环境。

## 查找原生功能

安装时填写 MapGIS 路径后，会从本机 HLP/CHM 建立帮助索引。Agent 可用 `mapgis_find_operations` 找到整理后的入口，再用 `mapgis_describe_operation` 读取参数和工作流；其他菜单可通过 `mapgis_search_native_help` 指定模块、关键词和主题编号检索。索引失效或缺失时，重新运行 `scripts/index_native_help.py`。目录状态、帮助说明与实际执行结果分别记录。

例如：

> 从这张扫描件建立一个本地演示坐标工程，生成点线区草稿；保留开放断层，检查接边，保存重开后导出 EPS 和 TIFF。

> 查找 DTM 的格网与等值线操作，先用解析平面小样核对输出，再处理我的数据。

新增原生库的源码、构建脚本、许可和替换说明见 `docs/THIRD-PARTY.md`。
