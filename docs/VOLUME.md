# MapGIS 6.7 XYZV体数据工具

2026-11-01新增链已在本机通过有界实际验收；全项目仍为IN_PROGRESS。数据均为合成解析样本，不能作为真实地质解释。主要证据在 `work/implementation-20261101/`，研究条目的增量见其中 `implementation-delta.json`。

|工具|输入与输出|执行与验证|
|---|---|---|
|`mapgis_volume_source_info`|3BN/3DM摘要、维数、轴、统计、绑定的frame|文件边界和实际数值检查；可选原厂头信息重开|
|`mapgis_read_volume`|分页XYZV，最多1000条/次|读取实际保存值，返回物理索引/三维轴序|
|`mapgis_prepare_volume_import`|DAT → 兼容3BN|保留观测顺序，float32显式误差预算；可选原厂重开|
|`mapgis_prepare_volume_grid`|3BN → 兼容3DM|开放算法计算、独立存储读回；可选原厂逐层头信息重开|
|`mapgis_prepare_volume_slice`|3DM → GRD、XYZV CSV、绑定JSON|X/Y/Z、线性或最近邻；原厂DTM接口逐格读回|
|既有工具组合|切片GRD → 三色WP、等值线WL、注记WT → MPJ/EPS/PDF/TIFF|使用grid_vectors、creation、原厂save/reopen及project/plot工具，无新编码器|

写入沿用prepare → apply；`mapgis_get_request`与`mapgis_reconcile_artifact`沿用同一事务账本。稳定request_id绑定完整参数和来源；改变参数应使用新的request_id。已有成功请求复用结果。prepare并不修改原始输入。

`frame`必须明确米制投影或带来源摘要的本地XY、`z_axis=elevation_up|depth_down`、值名和值单位。XYZ单位为米；深度向下不隐式取负。有效零值保留；本轮不接受未知缺值编码。X切片列/行是Y/Z，Y切片是X/Z，Z切片是X/Y。前两者有独立剖面坐标，不能误标成地理平面XY；CSV保留全部世界XYZ。

DAT为ASCII/UTF-8：首个非空行是记录数，随后每行四个有限数值X Y Z V。3BN以32位计数开头，128字节头后逐条4个float32。原安装转换器的实际格式与部分手册描述有差别，采用IDA分析和原厂读取确认的count-first布局。3DM为`3DEM`签名、58字节固定头与分层目录、每层256字节头及x最快/y其次的float32值；整体数组为zyx。读取会检查总长、目录/层边界、维数、所有有限值及统计。

`idw_3d`在三维欧氏距离上查询近邻并做反距离权重，精确重合点保持原值。它计算调用者明确给定的整个长方体，包括观测凸包外；不声称自动地质边界。`borehole_linear_idw`先按连续相同XY孔组在Z上做线性插值，再在XY上做IDW；每孔至少两条严格递增Z，同一孔不能分散重现，垂向超界必须明确reject或clamp。近邻查询复用现有SciPy cKDTree，无新增求解服务。[SciPy cKDTree.query](https://docs.scipy.org/doc/scipy-1.16.2/reference/generated/scipy.spatial.cKDTree.query.html)

API上限为100万观测、200万网格节点、nx/ny 2..32767、nz 2..256；这些是防止失控的接口上限，本轮没有用小样本冒称完成最大规模性能验收。所有保存均受显式坐标/值误差预算约束；float32导致Z平面重合、误差超限或不可表达时拒绝。GRD还遵守现有DTM数值范围限制。

原厂3BN/3DM读回使用固定摘要的原安装`W60_Anly.exe`，在新私有桌面中创建自有进程，只载入任务快照。2026-11-01用户明确批准该有限Win32控件后端作为项目AGENTS界面驱动规则的例外；授权记录在 `work/implementation-20261101/evidence/native-ui-authorization.json`。它不接管现有窗口，不切换输入桌面、不发键鼠事件、不改安装或许可。控件消息有PID和菜单命令白名单校验及超时，ListBox按原顺序读回。[Windows desktops](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops)、[LB_GETTEXT](https://learn.microsoft.com/en-us/windows/win32/controls/lb-gettext)

import/grid的`parameters.native_reopen`、info的`native_reopen`默认为false，以保持既有请求指纹和无UI的格式路径。使用true时，本地配置须同时有`native_volume_private_ui=true`、既有`native_backend_legacy_dep_compatibility=true`和正确安装路径。此项目已按用户授权启用。原厂头信息与助手独立逐体素检查分别记录；并未声称用原厂API读到了每个3D体素。GRD则确实逐格从原厂重开读回。

原厂仍可能显示“MapGis多用户服务器工作不正常”的已知提示；真实提示原文保存在回执。当前头信息路径已能在该环境完成；这不证明原专有插值器或全部许可模块正常。未知非空警告仍拒绝，空白初始化中的对话框等待就绪。保留原DogServer67，不修改许可检查。

读回宿主使用独占命名Job，父进程退出会结束其自有辅助进程与新建MapGIS子进程；Windows虚拟环境启动器和实际Python进程都核对并纳入该Job。辅助进程标准输入独立设为DEVNULL，避免继承MCP通信管道造成启动停滞。完成候选的清单摘要先固化在账本，再做原厂读回；恢复时校验来源、候选和清单。已完成读回复用原PID/文件，不重放；读回中断后先确认已知进程死亡、清理自有短路径别名，再对同一候选只读重验，既不插值也不重新编码。

本轮证据：

- `mcp-volumes-native-03/result.json`：33次真实MCP，29次成功和4次预期拒绝；四份import/grid输出及两次info在六个原厂新进程读回。45个孔间网格值和12个三维IDW值均与独立公式一致；另一独立60节点x+y+z体场的X/Y/Z切片共47格，原厂逐格一致。重复prepare/apply/get保持既有结果。
- `recovery-native-01/result.json`：实际exit91发生在原厂读回后、封存前；7次MCP完成接管、清单篡改拒绝和还原后接管，原厂PID复用，无新增读回进程。
- `recovery-inflight-02/result.json`：运行中只终止精确身份的自有父进程；旧launcher/helper/MapGIS全部结束。3次MCP恢复只读重验、发布、读状态；候选SHA/mtime保持，自有旧别名实际消失。
- `final-related-tests.log`：同一轮27 passed，146条现有NumPy 2.5/MaskedArray弃用警告保留。
- `mcp-volume-sections-01/result.json`：60次MCP完成三轴高程正向上的剖切、等值线/色带和点线区原厂读取；等值线逐顶点满足v=u+w+固定坐标，色带并集覆盖剖面且面积无重叠。初版文字布局检查失败，不作为最终成图。
- `mcp-volume-sections-final-01/result.json`与`visual-review.json`：复用已验证几何和数值，36次MCP完成细线样式、原厂存读、三层MPJ及EPS/PDF/300dpiTIFF。修正中间版的文字重叠和线宽；最终三幅本地预览均已实际检查。色带边界/体数据没有重算。
- `mcp-volume-section-migration/result.json`：三个MPJ和完整依赖复制到新目录，另3次MCP由新原厂进程重开全部9层，所有交付成员摘要保持。

该组合示例明确将无frame的解析体场声明为elevation_up，与原数值测试中明确声明的depth_down解释分别记录。没有更改3DM字节或给真实钻孔偷偷换正负。输出原生局部坐标数值保留剖面米，页面view明确映射8mm/m（示例印刷比例1:125），2mm文字和0.2mm等值线；CSV/section.json保留世界XYZ和页面映射。打印TIFF是图面输出，未声称地理配准影像。

尚未完成原厂彩色三维体剖面、任意斜剖面及depth_down的本链图面组合、未知版本/对数轴/缺值3BN/3DM，以及原厂专有插值参数。自动色标/坐标刻度与专业图面模板可沿既有整饰工具组合，未混称全套专业制图。数值体场也没有隐式生成闭合矿体、断层拓扑或资源分类。

既有新stdio入口为 `scripts/launch_mcp.py --root PROJECT`，CLI为 `scripts/mapgis_cli.py --root PROJECT --arguments ARGUMENTS.json call`。可直接使用交付目录中的参数示例；CLI每次加载当前源码。当前已连接的旧长驻MCP进程没有自动换代码，工具schema应以新连接实际`list_tools`为准，本轮没有重启其他聊天。

回退仅涉及本轮源码/目录/配置增量；修改前快照在 `work/implementation-20261101/production-before/`。移除本地`native_volume_private_ui`可禁用这条原厂头信息后端；不要据此停止其他会话或DogServer。完整原安装、原始报告与数据从未纳入改写或交付包。
