# 功能与边界

当前源码注册123个MCP工具、158项任务说明。计数用于核对加载版本，不是完成率。后台工具运行于本机stdio，不开放监听端口，不暴露任意代码执行入口。当前用户范围见[本机6.7调用契约](LOCAL67-CALLING-CONTRACT.md)，旧生态调研不作为完成条件；当前源交付包括私有调用后端；旧分享包保留作恢复版本。各模块入口与真实运行结果见[本机调用范围表](LOCAL67-ROUTES.md)。

2026-11-02新增[原厂SDK组合调用](SDK-CALLS.md)：原厂类型库发现、按类型组合原厂方法/属性/输出对象，在自有x86私有桌面执行。三种点线区原厂平移、属性和资源保全、保存及不同进程/搬移重开已验；失败断言会停止后续保存。[原生多媒体](MULTIMEDIA.md)接通代码8布尔与9..13五类原生引用、完整相对资源打包、MPJ和制图迁移。两者均不占用用户鼠标键盘；未扩称全部原厂菜单或查看器/播放器已验证。

下方基础表和“尚未接通”小节保留早期0.6.0基线，后续有日期的增量逐项补充；不能把旧基线缺口直接当作当前剩余任务。当前方法级限制以工具实际描述、对应专题和运行回执为准。

## 原生后端

| 能力 | 调用和验证范围 |
|---|---|
| WT/WL/WP | BAS60 打开、另存，独立 Reader 对比，再由不同 PID 重开；区分有效数量和物理编号上界 |
| 点移动、点线区删除 | 多点坐标修改；WP 共享弧段、孔洞、岛区、删除至空、保存重开与字节恢复；原生可能保留已删属性载荷 |
| 未保存状态 | Agent 自有原生缓冲区，dirty 相对最后一次显式保存；每次编辑有修订号，私有检查点与发布保存分别记录 |
| 异常恢复 | 父进程中断、原生子进程丢失、完成回执早于账本提交；核对后恢复，不重放编辑；租约阻止另一控制器抢占 |
| TIFF/MSI | byte 灰度/RGB，原生转换后新进程逐像素读回；四个原生控制点关闭后重读。调色板、nodata 和其他位深尚未覆盖 |
| MPJ | 原生 Create/Append/Update/Save，复制 WT/WL/WP/MSI 为同目录相对引用；隐藏旧目录后迁移重开，检查计数与 MSI 范围 |
| 原生 EPS | 独立进程调用 mapprj60/mapdsp60/winout60，绘制矢量和 MSI 背景；明确页幅、坐标框架和图层顺序；Ghostscript 检查实际像素 |
| PDF/RGB TIFF | 从 EPS 转换；检查纸张、TIFF 位深/波段/像素尺寸，PDF 再渲染并与 EPS 比较；打印页不推断为地理栅格 |

固定适配已核验的 6.7 DLL 指纹，需要合法许可及本机字体、符号库。独立 x86 宿主采用显式启用的旧版 DEP 兼容构建，系统设置和原 DLL 不变。错误对话框留在自有私有桌面，不切换输入桌面。

WP 持续编辑曾在较长暂存路径下损坏原生坐标头；当前使用有归属记录的短路径目录联接，数据仍留在项目内。退出和恢复时只移除本次链接。是否适合某次原生调用，仍按来源指纹、独立结构检查和新进程结果判断。

## 开放算法后端

| 模块 | 实现与检查 |
|---|---|
| 扫描/配准 | libvips 分块，GeoScan/OpenCV 候选；显式仿射/GCP、独立检查点；GDAL 校正和重投影使用明确目标格网，限制工作内存 |
| 拓扑/空间分析 | GEOS 造区、交点结点化、共享精度格网、缓冲、裁剪、差集、融合、简化；来源编号和属性映射进入 QA |
| 地形 | SciPy Delaunay 和线性插值，凸包外不外推；ContourPy 全指定层等值线；三角曲面面积和跨基面切填分开积分 |
| 网络 | NetworkX 最短路径、连通分量、部分边可达范围；保留物理边编号、方向、平行边和成本；结点化/端点吸附需明确选择 |
| 投影/图框 | PROJ 显式 xy，拒绝 ballpark 或缺少最佳格网的转换；图框边界加密后投影，写出 WL 和 GBK 经纬注记 |
| 属性/报表 | 只读 SQLite SELECT/CTE/聚合；__record_id 是物理编号，ID 可重复；拒绝写入、ATTACH、PRAGMA、扩展，限制查询工作量；CSV/JSON 持久化 |
| 数据交换 | GeoJSON/GPKG 导入/导出、Shapefile 导出；读回几何字段，拒绝截断/字段名碰撞；绑定输入哈希的 sidecar 保留原生文字、样式、身份 |
| 注记/接边 | 点位置、线半长、区内部点生成注记；按显式类别/格网融合接边，报告移动和来源 |

这些模块复用开放库，结果写入可编辑原生文件。其算法行为与旧 MapGIS 模块内部算法分别记录。网络服务范围输出可达路段，并非填充的等时圈。

多文件成果沿用同一账本，逐成员冻结哈希和字节，按完整目录发布；父进程中断后从完成回执对账，来源变化或成员冲突会返回错误。

## 坐标与颜色

MSI 转换器保留仿射范围，却会丢失 EPSG 标识；本包用精确 MSI 哈希绑定的 sidecar 恢复 CRS/GCP。保存 GCP 本身没有重采样，校正由 warp 工具单独完成。

旧原生坐标头不能准确表达所有现代 CRS。图框工具写前检查，不能精确编码时拒绝；不要把近似 WGS84 头当作 CGCS2000。自定义投影 GeoJSON 使用 2008 named-CRS 扩展，不属于严格 RFC 7946。

色号和符号依赖使用者自己的系统库。已有课程草稿按本机色表校准，该表及专有系统库不随包分发。EPS 的 CMYK 解释和直接 RGB 输出可能存在颜色管理差异。

## 尚未接通

- 修改另一个 GUI 编辑器的未保存内存。已有精确 build 的只读工程/图层/多选/dirty 观测保留，Agent 自有后台会话独立运行。
- 原生子图库、线型库、填充库的编辑，以及任意原生符号的制作和管理。
- 专有地图库/影像数据库、完整三维地形和地质剖面模块；DXF 等未列出的交换格式。
- 任意菜单、任意 6.7 构建或任意 MapGIS 新版本的通用后台接口。
- 从任意地质图片全自动得到专业审定的地层、断层、花纹和完整图例。

这些是早期基线限制，部分已由后续增量接通；当前任务范围须按本机调用契约核对。目录、帮助主题或成功样本不能代替其余方法的运行验收。另一台实体电脑的许可、系统库和原生流程尚未验证。

## 查找接口

先 find_operations，再 describe_operation，读取参数、后端和具体范围。目录保留 2 个桌面参考入口，明确 executable_by_mcp=false。search_native_help 检索使用者本机的 HLP/CHM；帮助和索引不随包分发。

## 2026-11-02 当前源码的新增有界验收

- `mapgis_prepare_raster_vectorization`：整数主题栅格精确转可编辑WP，保留孔洞、有效0、int32类号及像元面积。8连通多面分成有效原生记录，使用相同region_id和不同part_id保留连通组。64次实际MCP、3组原厂BAS保存重开及GPKG/MPJ/EPS/PDF/TIFF见 `work/full-implementation-20261101/mcp-vectorize-02/result.json`。
- `mapgis_prepare_raster_fusion`：显式同网格加权、线性IHS、B3小波IHS/AWLP/maxabs五种开放数学变体；原手册要求的重采样另用既有warp。保留未裁切float64、完整有效性和显式校准，参考影像评价与输入变化诊断分开。125次累计实际MCP及7组原厂MSI数值/掩膜/坐标往返见 `work/full-implementation-20261101/mcp-fusion-01/result.json`，逐个保存参考向量另经标量fsum/hypot核验。首版maxabs舍入翻转失败保留在attempt-01，当前协议2采用明确的数值相等规则；不同有效范围的指标不能直接作同区域方法排名。
- `mapgis_volume_source_info` / `mapgis_read_volume`：只读显式encoding_policy=stored|physical支持官方3DEM联合log10Z/V标记1,1，默认linear_only保持。281次MCP分页覆盖两域各136640节点，并原厂重开40层存储域头信息；见 `work/full-implementation-20261101/mcp-volume-log-01/result.json`。physical是由保存float32对数逐值重建的数值域，未知单位/CRS和原先量化不消失；不把存储均值指数化为物理均值。混合标记与生产log模型变换/绘图另列缺口。

当前新增范围使用新建stdio/CLI实际验收；已有长驻宿主不会因源码变化自动更新schema。以上是原196条登记的增量证据，整个18族及新增6包仍为IN_PROGRESS。


2026-11-02：DXF 嵌套块/完整 MINSERT、非均匀缩放与镜像、明确纸空间对象已通过 6 组原厂保存重开（91 次实际 MCP），另 30 次 MCP 修正出图边距。普通二维 XCLIP 的嵌套/凹多边形/阵列裁剪，以及二维顶视视口的比例、DCS 中心、WCS 目标、旋转、冻结层和非矩形边界，另通过 10 组原厂保存重开与 151 次实际 MCP。各输出均独立核对坐标/部分编号，并实际查看 EPS 预览。原模型空间协议 2 和块/布局协议 3 请求指纹保持可沿用。证据：work/full-implementation-20261101/mcp-cad-clipping-01/result.json、visual-review.json。C107 仍 PARTIAL：复杂文字/字体/属性、反向/复杂空间过滤器和三维透视、原 CAD 编辑器及打印样式等价尚未全验。

2026-11-02：C107 增加显式字体哈希绑定的单行 TEXT/ATTRIB/常量 ATTDEF 轮廓，支持对齐、FIT/ALIGNED、倾斜、镜像、非均匀块变换及实际字形裁剪；输出为可编辑 WL/WP 字形几何，完整原文和字体来源进 UTF8 对应表。12 组原厂保存重开、181 次实际 MCP，独立自制字体精确几何和真实黑体中文均已查看预览。原协议2/3/4请求沿用。富MTEXT、SHX、复杂脚本和原CAD渲染等价仍为缺口；C107及总目标不据此标为完成。证据：work/full-implementation-20261101/mcp-cad-text-01/。

2026-11-02：C146 增加显式原始/对数迭代标准差、MAD及经验分位数，样本SD(ddof1)、分域、严格界外剔除且保留相等值；全量原样与每轮来源均保留，log阈值整体反变换。28项相关测试、148次实际MCP，安装宏原表1092样×4元素独立核对；4张64点原生图和4份图例经原厂读回/MPJ/EPS/PDF/TIFF与实际看图，逐图60正常色点+4高值色点。历史箱线请求沿用。已静态复现原宏跨元素样本数状态差异（原始Ag历史1081、新独立1080），原宏/原数据不变。原分组众值方法、删失/检出限和真实地质效能仍另有范围。证据：work/full-implementation-20261101/mcp-geochem-statistics-01/。

2026-11-02：C146 又接入原始/对数分组均值SD和左半部众值法，先明确sigma预剔除、再等宽分组；完整精度及明确小数舍入变体分开，频数、边界和估计器离差均入表。7组数值、72次实际MCP、4份原生文件/2图，原协议1两次沿用；分组均值56范围内+8高值、众值60+4，与独立数学分类和原厂图一致。没有执行Excel宏，也不宣称VBA Single/中间乘积舍入/Find状态逐位匹配。证据：work/full-implementation-20261101/mcp-geochem-grouped-01/。

C149 soil/sediment numbering bounded acceptance (2026-11-02): explicitmetregrid/bearing/numberformulas, nativeWT/WT/WL and CSV/JSON; sediment originalphysicalIDs/nonattribute bytes retained, manualIDs/reservednumbers/explicitboundaryowners. Evidence `work/full-implementation-20261101/mcp-sample-numbering-02/result.json`:193 cumulative MCP,5input/12output native save+freshreopen,5MPJ/EPS/PDF/TIFF and actualpreviews. SyntheticWGS84TM120E; field/norm/collisionlayout notcertified. Sharedcreation nowrefuses newCRSencodingthat changesdatum/ellipsoid/projection; inparticular pinnedencoder approximatesCGCS2000asWGS84, so usefaithfulsupportednativeCRSor explicitseparateprojectionroute. Existinghistoricaloutputsnotretroactivelyrecertified.

C144 assay QA (2026-11-02), `geochemistry.assay-qa` / `mapgis_prepare_assay_qa`: explicit source-specific qualifier and token dictionaries, measured/estimated/left/right/interval[DL,QL)/missing/blank/not-analyzed/rejected states, exact dimensional unit factors, source batch metadata, duplicate/coordinate/limit/unknown-unit issues. Everyrow retained; no imputation, batch correction, inferred dry/wet/density/oxide conversion or laboratory qualification. Raw/Wide/MapReady/Measurements/Issues/Batches with JSON/CSV. Nonexact Wide values remain text; existing strict background tools reject implicit statistics. Evidence `work/full-implementation-20261101/mcp-assay-qa-01/result.json`:17 focused checks,130 cumulativeactualMCP,24dry+12water synthetic rows,4 original nativefiles/2complete nativeplots reviewed; independent green/red pixelcomponents19/5and12/0 plus nativeannotationtextreadback. Original1092x4 values have4368 explicitly UNKNOWNunit issues; no original location/unit invented.

Runtime memory (2026-11-02): server/CLI entrypoints default to one OpenBLAS thread per tool process, preserving an explicit caller setting; this leaves the Windows global environment and other applications unchanged. New process needed to observe this change. PyFlwDirD8 executes in an owned short-lived Python process, avoiding cold Fortran import beside MCP stdin and releasing compiler memory after completion. Pinned algorithm, parameters, ledger/protocol1 and checkpoint recovery stay the same. Evidence `work/full-implementation-20261101/memory-disk-cleanup-20261102/mcp-runtime-memory-01/result.json`: actualfreshMCP idle private178036736B before/afterD8, compilerprocessended, source preserved and cached raw bytes/mtime unchanged.59related tests pass. Capability output now identifies the actual server PID/start time for resource ownership.
C144 explicit absent metadata: input_unit=UNKNOWN is retained as an unknown-unit issue; batch_fields=[] explicitly records missing batch metadata. Neither case invents source columns or enables numeric statistics.18focusedtests(current59relatedsuite) and directoriginal1092x4MCP case preserve all4368measurements with8736unit/batchissues.
Archived duplicate staging: `evidence/archives/validated-staging-20261102.zip` retains all1156removedAPPLIEDcandidatefilesdirectories after realrestorevalidation; publishedoutputs, ledgers andsource snapshots remain at their existingpaths.15actualpostcleanupMCPstatus/reconcile/apply checks acrossfivefamilies passed without changingpublishedbytes/mtime. Restore details: `work/full-implementation-20261101/memory-disk-cleanup-20261102/README.md`.

C150 combination anomaly (2026-11-02), `geochemistry.combination` / `mapgis_prepare_geochemistry_combination`: distinct-element union-first k-of-n faces with holes; caller-defined domain/thresholds/weights/units/boundary ownership, full physical sample and original region lineage, sampled mean/min/max/sample SD, threshold counts, mean/threshold and explicit area/RNAP equations. Optional all_observed_maxima produces native WT and table, retaining every tied member-element maximum including measured0; no inferred deposit centre. 10 focused tests and303 cumulativeactualMCP,6source/12output originalnative roundtrips plus6furthernativepresentationchanges/resaves;3MPJ/EPS/PDF/TIFF and actualfinalpreviews/fullresolutionpositionpixels accepted. Originalprotocol1 actualAPPLIED replay remains unchanged. Evidence `work/full-implementation-20261101/mcp-geochem-combination-02/result.json`; sampled indices are not mineral resources or ore probability. Current source116tools/153operations, not a completion percentage.

G04/C027 offline databases (2026-11-02): [DATABASES.md](DATABASES.md) documents `mapgis_database_info`, `mapgis_prepare_database_join`, and `mapgis_prepare_database_writeback`. SQLite/ACEAccess/classicDBF primarytable branches preserve explicit NULL/empty/zero/unmatched states and allnativephysicalIDs/nonattribute bytes.319cumulativeactualMCP,3nativeinputs/22nativeoutputs(including9QA/3annotations/1extra17digitdouble),3nativeprojects+plots andindependentSQL/GDAL/ACEwriteback/rollback/precision/lateWALoracles. Accessonlyexplicitisolatedtable/datafile, notwholeobjectclone.46relatedtests +1minimalMCPenvregressionseparate; thisisnotafreshwhole-repogreenrun. Actualstdio119tools/155operations. Afterallproviders private209408000B, ownedsessionended. Evidence `work/full-implementation-20261101/mcp-database-01/result.json`; source/install/licence/otherapps preserved. C028multimedia/auxiliaryexternaljoins/remoteSQLresources/complexDBF/wholeAccessobjectscoperemain.
