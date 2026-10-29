# Windows 分享版

适用于 Windows 10/11 x64，自带 Python 和运行依赖。MapGIS、许可、专有字体和系统库由接收者提供。

## 首次运行

1. 完整解压到可写目录，不在 ZIP 预览中启动。
2. 运行 `01-Setup.cmd`。输入 MapGIS 根目录、program 目录或 mdiedit6x.exe 路径；只用文件处理可直接回车。
3. 运行 `02-SelfTest.cmd`，确认 PASS；`03-Diagnostics.cmd` 检查依赖和版本。
4. 把生成的 `mcp-config.local.json` 加入支持 stdio 的 MCP 客户端。

安装只在解压目录写本地配置。移动目录后重新安装以更新路径；独立手改的配置会保留，需要自行合并。MapGIS-MCP.cmd 是 stdio 入口，由客户端启动后等待输入。

## 后台原生模式

默认关闭。旧 BAS60 需要本包独立 32 位进程采用 DEP 兼容构建，只影响该子进程。接受此取舍后运行：

```powershell
./runtime/python/python.exe -s -B -X utf8 scripts/portable.py setup --mapgis D:/MapGIS67 --enable-native-legacy-host
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_native.py
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_workspace.py
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_modules.py --native
```

已绑定安装可省略 --mapgis。路径含空格时加引号。自检使用合成数据，不操作鼠标，不修改安装或系统 DEP。许可仍由原生组件确认。

不启用原生后端也可运行开放模块自检：

```powershell
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_modules.py
```

## 交给 Agent

Agent 阅读 AGENTS.md 和 docs/NATIVE.md 后，通过 MCP 或 MapGIS-CLI.cmd 调用。例如：

> 用这些控制点校正扫描件，提取颜色区和开放线候选；保留原图，生成点线区草稿和可迁移工程，核对后输出 EPS、PDF 和 TIFF。

> 把这份 WP 打开到后台会话，删除物理区 3 和 5。检查孔洞和共享边界，查看未保存状态，再另存并重开验证。

49 个工具和 57 项任务说明通过 find_operations/describe_operation 查询。接口返回候选、实际检查和来源指纹，apply_patch 后才交付。后台运行无需电脑操作宿主；外部 GUI 的只读绑定有单独宿主要求。

## 数据和恢复

保留原图、可编辑源文件、工程全部图层和请求账本。输出使用新分支。record_id/__record_id 是物理槽位，属性 ID 可以重复。异常后先 recover/reconcile；重复 request_id 用于对账，避免盲目再执行。

扫描候选需专业复核，本地示意坐标要标明用途。真实地理精度和接收机的颜色/符号另行核对。[功能与限制](CAPABILITIES.md)列明尚未接通的模块。

开放依赖、许可和对应源码见 THIRD-PARTY.json、docs/THIRD-PARTY.md 与 third_party/sources。包内没有 MapGIS、许可工具、IDA/Ghidra、课程报告、私人图件或发送者配置。
