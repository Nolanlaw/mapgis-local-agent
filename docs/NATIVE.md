# 后台接口

0.6.0 的日常执行入口是本地 MCP/CLI。Python 地理库处理文件，独立 x86 进程调用本机 MapGIS 6.7。原生库加载在自有私有桌面，不发送键鼠事件，不切换用户桌面。

## 运行条件

分享包先运行 scripts/portable.py setup。原生后端需要额外传入 --enable-native-legacy-host，接受本包独立子进程的旧版 DEP 兼容构建。系统 DEP、原 DLL 和其他应用不变。BAS60、mapprj60、msibase、mapdsp60、winout60 按实际用途逐项校验固定指纹。

普通文件工具无需 MapGIS。原生操作需要匹配版本、合法许可和所用字体/符号库；未知构建会返回错误。

## 保存文件

1. inspect_file 读取 SHA256；编辑先 make_work_copy。
2. native_file_info 由新进程打开 WT/WL/WP，返回有效数量和 native_bounds。
3. prepare_native_operation 支持 resave、point.move 和 WT/WL/WP 的 feature.delete。
4. 检查 PREPARED 候选，再 apply_patch 发布新分支，用 get_request 检查实际结果。

工具名均带 mapgis_ 前缀。例如 prepare_native_operation 的参数：

```json
{
  "operation": "feature.delete",
  "file": "D:/work/copy.wp",
  "expected_sha256": "当前文件的64位SHA256",
  "parameters": {"record_ids": [1, 3]},
  "request_id": "remove-1-3"
}
```

record_ids 是物理槽位，属性 ID 可以重复。WP 前后检查共享弧段所有者、孔洞、岛区和闭合环；原生保存后用另一个 PID 重开。原生可能保留已删属性载荷或孤立弧段，不把未压缩存储误判为有效区。

## 持续原生会话

native_workspace 维护 Agent 自己的原生 buffer：

| action | parameters | 其他参数 |
|---|---|---|
| open | source、expected_sha256 | workspace_id |
| status | {} | workspace_id |
| edit | operation 与 parameters，结构同原生编辑 | expected_revision、request_id |
| save | {} | expected_revision、request_id |
| close | {}；有未保存修改时明确 {discard:true} | expected_revision |
| recover | {} | expected_revision |

open 复制来源，edit 保持同一原生进程并写私有检查点。dirty 相对最后发布的保存；save 交付新文件后清除 dirty。close 默认拒绝丢弃未保存修改，discard 回到最后保存。每次操作返回 revision，下一次带回它。

父进程中断或原生进程丢失后，recover 核对完成回执、检查点、来源和新进程重开；没有完成回执的命令不会重放。独占租约防止另一客户端抢占。后台 buffer 与外部 GUI 的未保存内存独立，不写入外部进程。

旧原生库对长路径有兼容问题。会话临时建立有归属记录的短路径目录联接，实际数据仍在项目内；退出或恢复时只移除本次链接。不要清理活动检查点。

## 工程、影像和输出

- prepare_raster_warp：GDAL 仿射 GCP 校正/重投影，明确 target_crs、bounds、resolution、resampling。
- prepare_native_raster：to_msi/to_tiff，byte 灰度/RGB 原生转换和 GCP 保存，新进程逐像素读回；CRS/GCP 标识由精确 MSI 哈希绑定的 sidecar 保留。保存 GCP 不等于重采样。
- prepare_native_project：layers=[{file,expected_sha256}]，parameters={name:'map.mpj',coordinate_frame:'native'}；复制点线区/MSI，以相对路径保存，隐藏旧目录后迁移重开。
- native_project_info：读取实际图层计数、路径、指纹和 MSI 范围，没有 edit/save 调用。
- prepare_project_plot：可迁移 MPJ 的原生矢量/MSI EPS；view={native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}。
- prepare_native_plot：相同 view，按底到顶的矢量 layers 直接绘制原生 EPS。
- prepare_plot_export：实际 EPS 的 PDF/RGB TIFF；parameters={formats:['pdf','tiff'],dpi:300}。打印页不推断为地理栅格。

先确认所有图层使用同一物理坐标框架。多文件成果按完整集合提交；不同目录的 MPJ 先创建可迁移副本。保留 EPS、PDF/TIFF 和预览，独立核对中文、符号、填充与地质内容。

## 中断和恢复

已有 request_id 先 get_request。RUNNING/FAILED 不盲目重放：原生文件/独立图层 EPS 用 reconcile_native_request，多文件工程/栅格/交换/导出用 reconcile_artifact，会话用 recover。缺少完整候选时保留失败和原件。

同布局修改可按请求字节恢复；原生另存改变布局时，只有候选没有后续修改才能整体恢复。多文件成果冻结到项目账本，不依赖暂存文件才能发布；冲突时保留后来修改并报错。

## 外部编辑器与剩余模块

已有 native_session.mjs 仅在提供 @oai/sky 的宿主中绑定外部编辑器：精确 PID/启动时间、完整 MPJ、模块指纹、真实窗口和物理选区。get_live_state 只读。旧桌面工作流保留为参考，不作为后台验收。

原生系统库作者工具、专有图库/影像数据库、完整三维和地质剖面尚未接通。find_operations/describe_operation 返回各项后端和范围，search_native_help 检索本机原始帮助。帮助和目录不自动成为成功记录。
