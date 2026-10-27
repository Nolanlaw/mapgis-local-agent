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
