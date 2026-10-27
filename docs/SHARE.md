# MapGIS 本机助手 0.3.1 · Windows 离线分享版

本包包含独立Python3.12运行时、九个MapGIS文件/MCP工具、精确版本只读状态适配器、原生操作说明、Ghostscript及其源代码。解压后无需安装系统Python或下载Python依赖。适用于Windows10/11的x64电脑。请完整解压到可写文件夹后使用。

所有启动入口和生成的客户端配置均禁用用户Python包目录，运行时使用包内搜索路径。自行编写命令时请使用runtime/python/python.exe -s -B -X utf8，再接脚本与参数，以保持同样的环境隔离。

## 三步开始

1. 双击 01-Setup.cmd。输入自己电脑上现有的MapGIS安装根目录，或program目录、mdiedit6x.exe完整路径。暂时没有MapGIS时直接回车，可先使用文件工具。脚本只生成本文件夹内的config.local.json、mcp-config.local.json、setup-state.local.json，不修改系统环境、全局MCP、模型或许可。
2. 双击 02-SelfTest.cmd，检查出现PASS；双击03-Diagnostics.cmd查看环境与版本匹配结果。自检会真实启动stdio服务，核对九工具，用合成线文件验证读写、请求对账、取消及逐字节恢复，然后清理其自有临时数据。
3. 在有本地文件/终端及电脑操作能力的Agent宿主中打开本文件夹，先让Agent阅读AGENTS.md和docs/NATIVE.md，再直接给它工程路径和任务。支持mcpServers JSON配置的客户端也可使用安装时生成的mcp-config.local.json；其他客户端按自身格式填入相同command/args。

文件夹移动后再运行01-Setup.cmd，会更新本包自己生成的路径。若配置文件被人工改动，程序会保留并报告，需先核对再重新配置。MCP命令使用MapGIS-MCP.cmd；命令行使用MapGIS-CLI.cmd capabilities等。双击MCP会等待客户端输入，这是stdio服务行为；日常使用交给Agent或客户端启动即可。

## 接收者需要自备

- MapGIS6.7与可用许可、原工程所用的符号/字体资源。许可要在MapGIS中实际确认。本包没有MapGIS安装程序、Dog工具或任何许可替代物。
- 原生当前工程/选区/dirty读取严格匹配build051118四个模块的SHA256；03-Diagnostics的MATCH仅表示二进制版本匹配，许可与宿主电脑操作能力仍须可用。UNSUPPORTED_BUILD时文件工具仍可使用，实时原生功能需重新适配。
- 原生界面操作需要宿主提供Sky电脑操作能力。一般stdio客户端提供文件工具；本包不包含聊天模型、Agent宿主或API密钥。

## 已验证能力

当前MPJ/图层绑定；多选与未保存标志；WT/WL/WP读取与明确记录的属性/样式修改；MapGIS原生几何增删和属性保存重开工作流；新分支请求续接与受保护恢复；原生EPS/TIFF出图及EPS转PDF。当前软件的完整地质图图片复绘仍需具体图件实测，不承诺上传任意图片即可全自动重建。

文件工具物理编号与属性ID不同。原始数据应保留，编辑在副本中执行。恢复仅针对有基线/请求记录且通过冲突检查的字段，不能恢复崩溃前任意内存。新图件的坐标系、比例尺、图例及符号库需分别核对。原生输出先用Prepare-NativeOutput.ps1暂存到短ASCII路径，实际检查工程依赖和输出文件，再保存交付。

Ghostscript在runtime/ghostscript，原生EPS转PDF可由Agent调用其中bin/gswin64c.exe的pdfwrite和EPSCrop选项。此包内含相应原始源码压缩包和许可证；GEOS源码也随包保留。Python及各依赖许可在runtime/python/LICENSE.txt、依赖dist-info/licenses等位置，详见THIRD-PARTY.json。第三方库依各自许可证使用和修改。可编辑助手源码在src/、scripts/，原生格式上游源在vendor/mapgis2shp。

本离线应用只带九工具实际需要的运行依赖，未附可选pyogrio/GDAL通用格式导出引擎；开发者需要通用格式互转时，可按完整requirements.lock.txt另行准备开发环境。

## 范围和数据

分享包不含制作人的课程图件、照片、聊天、记忆、凭据、运行数据库、本机安装配置或逆向分析工程。新安装产生的work、evidence和deliverables属于接收者自己的任务数据，之后转发时应重新生成干净包，避免把个人配置和图件一起转发。压缩包内bundle-manifest.json记录原始负载的SHA256。
