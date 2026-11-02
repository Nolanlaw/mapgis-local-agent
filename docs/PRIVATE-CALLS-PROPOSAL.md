# 已启用的本机6.7私有进程调用

用户已批准私有Win32方式覆盖本机6.7非服务器功能，并且不占用其鼠标、键盘和前台。已实现的SDK/COM/文件接口继续使用。本后端用于没有现成接口的原厂菜单和参数对话框，直接操作软件已有功能。授权记录为evidence/release-070/general-private-ui-authorization.json，发行版通过setup --enable-private-ui显式启用；当前制作项目已授权。

## 已可审查的实现

- `src/mapgis_agent/native_ui_core.py`：只创建新进程，使用新建的私有桌面。GUI进程在挂起状态加入本会话作业后才恢复运行；作业关闭只结束其自有进程树。没有绑定既有MapGIS实例、SwitchDesktop、SetCursorPos、SendInput、全局按键或进程内注入。
- `src/mapgis_agent/private_control.py`：只接受摘要固定的本机模块，排除数据库服务器入口；输入先复制到任务私有目录，输出路径事先绑定，目录绑定可用member指定内部文件。主文件依赖可按显式名称/摘要复制。调用方不能传任意EXE或PID。
- 原厂EXE、运行DLL、颜色查找表、MCONFIG.GIS、CLIB和系统库均使用任务副本。环境五个目录指向短ASCII任务路径；数字测图使用SuvSlib。没有修补原厂二进制或修改安装配置。普通模块调用持续复用同一session_id，避免重复启动。
- `mapgis_private_control`：policy/open/status/observe/act/close；实际菜单/控件观察给出一次性token，操作限当前观察窗口和命令。支持中文文本、按钮、菜单、选择框、复选框、树目录分页/展开/选择、客户区画布消息和无全局状态的按键消息。助手使用文件RPC和独立空标准输入，避免继承MCP输入管道阻塞；启动器、helper和应用分别记录创建时间。请求有稳定ID和完整载荷，重复请求读回回执；过期token或其他进程窗口拒绝。命令投递回执不是运算完成证明，结果仍需观察及原厂保存/重开。
- `capture`在同一受限自有作业内启动短时重绘进程，通过PrintWindow绘制独立PNG，不读取屏幕或窗口DC。重绘进程有8秒期限并实际结束。原WindowDC试验失败，已移除该路线并删除唯一夹带桌面背景的图像，09结果保留失败原因。
- Windows作业限制用户句柄、剪贴板、桌面切换、全局原子、显示和系统参数；文件对话框确认要求完整任务路径绑定。口令控件文本脱敏，当前不提供口令输入路线。服务器命令显式排除。

模块名单包括本机启动器核对的19个非服务器入口和实际存在的原厂DTM3DFly子模块。DTM6x是缺少解包载荷的启动壳，另保留其诊断元数据。名单本身不是业务验收；对应与结果见[本机调用范围表](LOCAL67-ROUTES.md)。未增加其他版本、独立产品或新服务器部署。

## 已实际执行的验证

`evidence/release-070/private-control-checks.json`记录两个自有合成GUI进程的31项检查，包括中文控件读回、选择/复选框、中文树目录及选择、原窗口菜单执行一次、UTF8文件逐字节核对、请求对账/载荷冲突、旧token/其他进程窗口/未观察命令/未绑定绝对路径拒绝、密码脱敏、画布与按键消息、独立PNG解码。此前同一重绘方法的图像已实际查看，只含自有窗口。输入桌面前后保持原值，两个GUI及helper均结束。

真实MapGIS流程：reports-native-result.json证明原厂BB构表、保存、全新进程重开、重存字节一致，以及原厂生成WL后独立BAS60读取163条线。library-native-result.json证明原厂DBS矩形分幅图库生成/保存/新进程重开，导出的WP索引由独立BAS60确认1区及[0,0,500,500]范围。上述进程均已结束。原有3BN/3DM专用接口和已验证SDK入口继续使用。

高程库已完成GRD入库、原生Inf/Hdr/Dem保存和新进程目录树重开；网络编辑已完成WN保存、新进程重开和WL折点读回；数字测量已完成SUV保存、新进程重开和TXT坐标/高程读回。19个主入口中18个读到真实主界面；DTM6x诊断为缺少载荷的解包壳。实际DTM3DFly三维子模块已装入GRD并读到完整菜单，GUI导出错误与SDK替代路线单独记录。全部自有GUI及helper已结束。启动观察与业务结果分别留证。

## 启用边界

当前项目`AGENTS.md`原文为：

> Live MapGIS 6.7 observations use scripts/native_session.mjs with the documented `@oai/sky` object in node_repl. Do not replace this with another UI driver.

随后用户明确批准将相同边界扩展到其余非服务器模块，项目AGENTS.md已记录例外，本机开关为true。其他根目录缺少显式开关时仍直接拒绝open/act；database_setup即使启用也不能打开。无需再次询问同一授权。

调用流程：open声明任务输入及输出；observe取得窗口/控件/菜单和token；act按当前观察调用；每次变化后重新observe；保存后用原厂重开和独立文件读取核对。某些菜单只切换工具，须随后向画布发送指定点消息，报表构表即属此类。正常关闭使用close；明确discard=true时可收回无响应的自有作业，并校验PID、创建时间、程序摘要和作业归属。用户现有窗口、键鼠和前台、原始数据、安装与许可保持。
