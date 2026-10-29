# MapGIS Local Agent

把 MapGIS 6.7 的点、线、区和工程交给 Agent 处理。通过本地 MCP 或命令行调用，日常文件处理和已接通的原生操作不需要鼠标。

支持 Windows 10/11 x64。分享包自带 Python、开放地理算法和输出运行时；MapGIS 及其许可由使用者提供。

## 功能

- 读取 WT/WL/WP 的几何、属性、注记和图形参数；按物理编号查询、修改、创建和恢复。
- 在独立原生进程里移动点、删除点线区，检查 WP 共享弧段与孔洞，保存后用新进程重开。
- 维护 Agent 自己的原生编辑会话：未保存状态、修订检查、保存、放弃和进程中断恢复。
- 从扫描件提取可复核的点线区候选，执行仿射配准、GDAL 校正和原生 TIFF/MSI 转换。
- 新建带相对图层路径的 MPJ，迁移后重开，输出含矢量与 MSI 背景的原生 EPS，再导出 PDF/RGB TIFF。
- 用 GEOS/PROJ 造区、结点化、叠加、缓冲、简化、接边、转换坐标和生成图框；用 SciPy/ContourPy 生成地形 TIN、格网、等值线与切填方量；用 NetworkX 求路径和可达路段。
- 用只读 SQL 查询属性，导出 CSV/JSON 报表，交换 GeoJSON、GPKG 和 Shapefile。

实际范围见[功能说明](docs/CAPABILITIES.md)。原生符号库编辑、专有图库/影像数据库、完整三维地形与地质剖面尚未接通。

## 下载与安装

当前源码为 0.6.0，Windows 0.6.0 分享包已在本机完成独立解压和原生自检；Release 资产尚待上传。[Releases](https://github.com/Nolanlaw/mapgis-local-agent/releases/latest) 当前保留 0.5.0 稳定包。以下步骤适用于取得本版本分享包后。

1. 将 `MapGIS-Agent-0.6.0-Windows-x64-Share.zip` 完整解压到可写目录。
2. 运行 `01-Setup.cmd`，填写本机 MapGIS 根目录；只用文件工具时直接回车。
3. 运行 `02-SelfTest.cmd`。看到 `PASS` 后，把生成的 `mcp-config.local.json` 加入 MCP 客户端。

原生后端默认关闭。旧版 BAS60 需要本包独立 x86 进程采用 DEP 兼容构建；接受这一进程的取舍后，在解压目录运行：

```powershell
./runtime/python/python.exe -s -B -X utf8 scripts/portable.py setup --mapgis D:/MapGIS67 --enable-native-legacy-host
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_native.py
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_modules.py --native
```

这个开关只影响本包的独立子进程，不改 Windows DEP 或 MapGIS 安装文件。移动解压目录后重新运行安装入口，更新本地路径。

## 使用

MCP 入口是 `scripts/launch_mcp.py`。49 个工具使用类型化参数，57 项任务说明通过 `mapgis_find_operations` 和 `mapgis_describe_operation` 查询。

已有文件：`inspect_file → make_work_copy → prepare_* → apply_patch → get_request`。

持续编辑：`native_workspace(open) → edit → status → save → close`；中断后用 `recover`。

工具名均带 `mapgis_` 前缀。准备操作返回来源指纹、候选与检查结果，提交后才产生交付文件。重复请求用同一个 request_id 对账；编辑会话带回 expected_revision，避免重复执行或覆盖后来修改。参数见[后台接口](docs/NATIVE.md)。

也可以直接使用 CLI：

```powershell
./MapGIS-CLI.cmd tools
./MapGIS-CLI.cmd capabilities
```

## 从扫描件到出图

登记扫描件 → 分块预览 → 明确控制点和坐标框架 → 配准/校正 → 生成并复核候选 → 建立 WT/WL/WP → 创建 MPJ → 原生 EPS → PDF/TIFF。开放断层、孔洞、共享边界、颜色、中文与符号需要分别核对。

缺少真实坐标资料时可以用明确标注的本地演示坐标。颜色候选需要专业地质复核，示意控制点也不能证明地理精度。

## 开发

需要 Python 3.12；依赖见 `pyproject.toml` 和 `requirements.lock.txt`。原生宿主源码在 `native/`，使用 MSVC x86 构建。源码包不附带 MapGIS DLL、许可、字体或专有系统库。

```powershell
./.venv/Scripts/python.exe -s -B -X utf8 -m pytest -q
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test.py
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test_modules.py --native
```

[验证记录](docs/VALIDATION.md) · [分享说明](docs/SHARE.md) · [来源与复用](docs/SOURCES.md) · [第三方许可](docs/THIRD-PARTY.md)
