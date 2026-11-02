# MapGIS Local Agent 0.7.0

让Agent通过本机MapGIS6.7执行操作，使用MCP或CLI。日常任务由Agent选择专用SDK/文件接口，或在新建私有进程中调用原厂菜单、控件和画布，完成处理、保存、导出与结果检查。用户可继续使用自己的桌面键鼠。

范围是当前适配的MapGIS6.7非服务器功能；本机安装、合法许可、字体和系统库由使用者提供。对应入口及实际限制见[本机调用范围](docs/LOCAL67-ROUTES.md)。

## 下载与配置

前往[最新Release](https://github.com/Nolanlaw/mapgis-local-agent/releases/latest)，下载 **MapGIS-Agent-0.7.0-Windows-x64-Share.zip**。完整解压后使用包内Python，无需安装Python依赖。仓库保持私有，访问与下载需要相应权限。

先运行01-Setup.cmd配置安装路径，再运行02-SelfTest.cmd。启用原厂SDK兼容宿主及私有进程操作时执行：

```powershell
./runtime/python/python.exe -s -B -X utf8 scripts/portable.py setup --mapgis 'D:/MapGIS67' --enable-native-legacy-host --enable-private-ui
```

两个开关只授权包自身的子进程和任务副本；安装程序、许可和系统DEP设置保持。私有界面开关也启用已有3BN/3DM专用调用。移动目录后重新setup，已授权开关保留，独立手改的客户端配置会保护。

将生成的mcp-config.local.json加入MCP客户端，或让Agent使用MapGIS-CLI.cmd。CLI与MCP调用同一工具注册表。完整安装说明见[Windows包说明](docs/SHARE.md)。

## 已接通的调用

- 原生WT/WL/WP、属性/样式/辅助表、工程与工作区的读取、编辑、保存、新进程重开及请求恢复。
- 原厂SDK类型发现与组合调用；影像、格网、TIN、场景、飞行、体数据，以及已有投影、裁剪、统计和数据交换工具。
- mapgis_private_control：原厂非服务器模块的菜单、中文控件、目录树、画布消息、保存和导出，全程在任务自有私有进程中执行。
- 原厂BB报表、DBS分幅图库、Inf/Hdr/Dem高程库、WN网络编辑及SUV数字测量均已有保存、新进程重开与结果读回。
- 原厂EPS、PDF/TIFF/JPEG、可迁移MPJ以及现有本地SQLite/Access/DBF文件工具。

当前注册123个工具、158项操作说明，用find_operations和describe_operation查询；原厂功能可先调用private_control的policy并检索本机帮助。数量用于核对加载版本，实际业务结果仍按任务参数检查。

## 使用方式

给Agent数据路径和业务参数，例如“保留原件，裁剪指定图层，修改属性，另存工程，再导出PDF和TIFF”。文件操作通过prepare/apply与请求账本提交；私有界面通过open/observe/act/close执行。中断后先对账，不盲目重做。

本版本使用同一套本机6.7验证。原厂DTM6x启动文件是缺少载荷的解包壳，实际三维功能使用DTM3DFly及原厂场景SDK；已观察到的GUI导出错误保留，三维图像走已验证SDK路线。MapSuv示例WT的属性一致性检查失败，SUV与文本坐标/高程读回通过；检查保持。详见[私有调用说明](docs/PRIVATE-CALLS-PROPOSAL.md)。

## 开发与验证

源码包不附MapGIS安装、许可、私人数据或Python环境。开发使用Python3.12和requirements.lock.txt；原生宿主源码位于native/。Windows分享包另附独立Python、开放运行库、对应源码及许可。

```powershell
./.venv/Scripts/python.exe -s -B -X utf8 scripts/mapgis_cli.py tools
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test.py
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test_native.py
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test_workspace.py
./.venv/Scripts/python.exe -s -B -X utf8 scripts/self_test_modules.py --native
```

发行检查包含包实际解压、全部文件核对、包内解释器与新stdio、原厂调用及保存/读回。此前31项真实私有控件检查和启动回执丢失恢复已通过；本版安装开关和打包变更另行回归。发布回执对应这台验证电脑，不推断其他实体电脑的许可或真实地质精度。

[能力说明](docs/CAPABILITIES.md) · [接口](docs/NATIVE.md) · [SDK调用](docs/SDK-CALLS.md) · [第三方来源与许可](docs/THIRD-PARTY.md)
