## 后台原生调用（0.5.0）

保存到磁盘的点线区可以直接作为 Agent 工具使用，不占鼠标，也不需要打开编辑窗口。后台是本包自己的 32 位进程，调用本机 MapGIS 6.7 的 BAS60、mapdsp60 和 winout60。支持原生读取、另存、多点坐标移动、点线删除和多图层 EPS。它处理保存文件的副本；运行中编辑器尚未保存的内容仍走下方的桌面绑定流程。

旧版 BAS60 会执行动态生成的代码。后台宿主因此需要旧版 DEP 兼容构建，只降低这个独立子进程的 DEP 保护。Windows 设置、MapGIS 文件和其他应用均不改。接收者自行接受该取舍后，在包目录运行：

```powershell
./runtime/python/python.exe -s -B -X utf8 scripts/portable.py setup --mapgis D:/MapGIS67 --enable-native-legacy-host
./runtime/python/python.exe -s -B -X utf8 scripts/self_test_native.py
```

已有安装绑定时可省略 --mapgis。后台功能默认关闭，普通文件工具不需要此开关。另一个构建或缺少许可会返回错误，不能靠修改 DLL、关闭系统 DEP 或换成屏幕截图完成验收。

调用顺序：

1. 用 inspect_file 获取磁盘指纹；编辑先用 make_work_copy。
2. 用 native_file_info 实际打开文件，读原生有效计数和 native_bounds。编号使用物理槽位，record_upper_bound 是不含上界的编号范围。
3. prepare_native_operation 的 operation 可为 resave、point.move 或 feature.delete（WT/WL，参数 {record_ids:[1,3]}）；移动参数为 {updates:[{record_id:1,x:12.5,y:13.75}]}。候选经过独立 Reader 比较和另一原生进程重开，再用 apply_patch 写入新分支。
4. 出图用 prepare_native_plot。layers 是从底到顶的 [{file,expected_sha256}]；view 提供 native_bounds、paper_mm 和 coordinate_frame="native"。先核对图层的坐标框架。工具保持比例、居中并生成真正的原生 EPS；Ghostscript 独立解释文件并拒绝空白或残缺输出。通过后 apply_patch 交付 map.eps。
5. 查询 get_request 处理重复 ID。RUNNING、FAILED 和冲突不能直接重放。中断后调用 reconcile_native_request，它检查自有子进程身份、持久回执和实际候选；文件重新进行独立比较与原生重开，EPS重新解释检查。缺少完整候选时保持失败，不再次编辑。保留输入副本和请求记录；同布局编辑可恢复本次字节修改；原生另存改变结构时，只有候选未发生后续修改才能从持久基线完整恢复。冲突时保留后来修改并返回错误。

新建 WP 请用 prepare_creation，它补充原生绘制需要的弧段索引。第三方 Reader 能读出的 WP，未必能由原生绘制器正确画出；后台会检查实际几何调用结果。保留原生 EPS 和工具返回的预览，地质解释与配准精度单独复核。

后台仅适配已验证的固定 DLL 指纹。其他编辑类型、原生栅格校正、TIN、网络和专业模块并未因此获得后台接口；其已有桌面流程继续保留。

# 分享版原生操作入口

Agent先阅读所在宿主的Computer Use规范，取得真实Sky对象。Python解释器使用本包runtime/python/python.exe；文件入口为scripts/mapgis_cli.py，stdio入口为scripts/launch_mcp.py，根目录始终取当前解压位置。

调用便携Python时使用-s -B -X utf8参数；优先复用包内CMD或生成的MCP配置，避免加载接收者自己的用户包目录。

1. 运行scripts/portable.py setup --mapgis 本机安装路径；doctor核对四个二进制指纹。许可由MapGIS本身确认。
2. 用sky.list_windows实际发现编辑器窗口和唯一PID；加载scripts/native_session.mjs的NativeSession，传入sky、真实window、当前root、便携python和PID。窗口ID不当HWND使用。
3. captureLive重新绑定MDI标题、完整MPJ路径、图层工作区、物理选择池及工程/工作区dirty。同名工程打开原生工程属性ID3012消歧。旧token不在切换工程后复用。
4. 原生编辑、几何增删与保存继续由Sky操作MapGIS执行。编辑前留与请求指纹相符的基线；prepareStyle/guard/beginCommit/verifySaved/verifyReopened用于已实现的区颜色闭环。属性与几何需按实际原生读回和全部记录差量验证。
5. 文件菜单保存工程与图层右键保存项目分别处理；Ctrl+S在此旧版可对应选择命令，不能当作已保存。旧式控件输入后读回；需要坐标动作时先取最新截图。
6. 出图使用scripts/Prepare-NativeOutput.ps1，输入清单project为MPJ完整路径、files为显式同目录依赖文件名，不重复MPJ。新RequestId、新短ASCII目录联接，例如某可写盘根下mg67-plot01。核对原生所有加载路径均为本次暂存。
7. 原生设置适合本图的版面并显式保存，PostScript输出→AI格式EPS输出→文字变曲线输出；或R光栅输出→生成Tiff图像→RGB/300dpi。输出前确保同名目标可按任务处理，原生可能直接覆盖。检查实际文件、幅面、中文、上下标、符号、填充和图例；截图不代替导出。
8. EPS转PDF复用runtime/ghostscript/bin/gswin64c.exe；保留原EPS及转换来源，检查PDF。不同后端的RGB/CMYK颜色会有差别。
9. 保存后正常退出本次自有编辑器，使用新进程重开验证。清理前核对活动进程与联接目标，保留源图和当前可编辑成果，不结束其他会话。审批拒绝则记录范围，不能换方式绕过。

只读状态属于精确build兼容配置，不注入或写目标内存，不改许可、全局环境或MCP。不支持的build、过期进程、选择归属歧义以及同字段后续冲突必须显式返回。更多源码行为见src/mapgis_agent与scripts/native_session.mjs。

## 模块与任务

先用 `mapgis_find_operations` / `mapgis_describe_operation` 找到任务及工作流。整理后的流程在 `workflows/`：扫描数字化、投影与图框、地形与等值线、空间分析。其他菜单先用 `mapgis_search_native_help` 检索本机模块，再读取完整主题与实际界面。

本机影像帮助明确：影像校正仅在“校正预览”状态下可用。添加控制点文件会追加已有控制点；导入前先核对当前数量与目标坐标，不能把两组不相容控制点混合拟合。

模块发现不保证许可可用，也不等于该操作已实测。未知对话框或提交结果先观察和对账；每项实际输入、输出、保存和新进程重开分别检查。纯文件运算可以使用现有 GEOS/PROJ/GDAL 通道；不要把它们记成原生模块已经执行。
