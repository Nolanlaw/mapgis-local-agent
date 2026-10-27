# MapGIS 本机助手 0.3.1

MapGIS 6.7 的本机连接器：九个原生文件/MCP工具、精确版本的当前工程/图层/选区与未保存状态读取，以及由宿主Sky执行的原生编辑、保存、出图工作流。

## 下载可直接解压的 Windows 分享版

前往[0.3.1 Release](https://github.com/Nolanlaw/mapgis-local-agent/releases/tag/v0.3.1)，下载 **MapGIS-Agent-0.3.1-Windows-x64-Share.zip**。这是带独立Python和运行依赖的离线应用包。

1. 完整解压到可写目录，运行01-Setup.cmd，填写自己的MapGIS安装路径；没有MapGIS时回车先用文件工具。
2. 运行02-SelfTest.cmd，确认PASS；03-Diagnostics.cmd检查版本及依赖。
3. 在有电脑操作能力的Agent宿主打开解压目录，阅读AGENTS.md和docs/NATIVE.md；一般MCP客户端可以使用生成的mcp-config.local.json。

GitHub自动生成的Source code ZIP只含开发源码，独立Python和第三方运行时位于上述Share.zip。分享包完整使用说明见[docs/SHARE.md](docs/SHARE.md)。仓库保持私有；下载者需要仓库读取权限，也可以直接转发下载后的干净Share.zip。

## 环境与能力边界

- Windows10/11 x64；原生功能需用户自备MapGIS6.7、有效许可及字体/符号资源。
- 当前实时适配严格匹配build051118的四个模块指纹；不支持的构建显式拒绝，不猜测当前工程或选择集。
- Python负责WT/WL/WP读写、物理记录身份、请求续接/恢复和只读进程状态；原生几何编辑、保存和EPS/TIFF输出由现有宿主Sky执行。
- 未附聊天模型、Sky宿主、MapGIS安装程序、许可工具或任何密钥。普通MCP客户端提供文件工具，不会自行获得桌面操作能力。
- 完整地质图图片复绘需要具体图件验证；不承诺任意图片自动重建，或恢复崩溃前任意未保存内存。

## 开发源码

本仓库保留src/、scripts/、tests/、文档、依赖锁和固定版本的原生格式上游源码。

使用已有Python3.12+运行scripts/Setup.ps1 -PythonPath '已有解释器的绝对路径'；需要原生绑定时另传-MapGISInstallation '包含program/mdiedit6x.exe的根目录'。脚本只在项目内建立.venv与客户端配置。

测试入口：.venv/Scripts/python.exe -m pytest -q；Node环境运行node --test tests/native_session.test.mjs。原生操作说明中的runtime/python/python.exe用于Release分享包，源码开发环境对应.venv/Scripts/python.exe。

## 验证与来源

0.3.1的37项Python与14项Node回归通过；最终分享包8830个负载文件实际解压逐项校验，九工具及修改/取消/逐字节恢复通过；中文空格路径迁移、生成MCP配置、版本边界与离线PDF转换实际验证。详见[验证范围](docs/VALIDATION.md)。这是同一台电脑上的隔离验证，其他实体电脑完整原生流程未实测。

格式实现复用[leecugb/mapgis2shp](https://github.com/leecugb/mapgis2shp)的固定提交2acf993e81a1d8e1984f110aff6725672ac09149，Apache-2.0；MCP使用官方Python SDK。第三方来源与许可见NOTICE.md、THIRD-PARTY.json及vendor许可证。Share.zip附Python、Ghostscript、GEOS及相关许可/所需对应源码。

仓库和Release均排除课程图件、本机配置、运行数据库、聊天/记忆、凭据、许可程序与逆向分析工程。接收者以后转发时使用原始Share.zip，避免把自己的工作目录和配置再次上传。
