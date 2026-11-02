# 后台工作流

课程实验和扫描图流程，以及下述已接通的后台模块，均保留各自的实际调用与回读证据。使用时需要同一修订的 Python 服务和 32 位原生后端；已有分享包不会自动获得源码增量。

## 钻孔柱状图与投影剖面

明确的孔口、测斜、分层 XLSX → 原始行号核对 → 最小曲率轨迹 → 原生 WT/WL/WP → 三层 MPJ 保存重开 → EPS、PDF、TIFF。调用顺序见 [boreholes.json](../workflows/boreholes.json)。

- `mapgis_read_borehole_workbook` 显式绑定三张表及字段。孔号为原样字符串，量测字段使用数值原值；排序必须显式开启，原始行号仍保留。公式缓存不作为已核对的量测值。
- `mapgis_borehole_trajectory` 使用 wellpathpy 0.5.2 最小曲率法，查询点沿测斜方向的球面插值计算，保留 MD、TVD、XYZ 和狗腿度。水平/垂直单位均为米，方位从同一格网北顺时针计算，倾角从铅直向下计算；不自动换算磁北或高程基准。
- `mapgis_apparent_dip` 将已给定的真倾角投到指定方向的铅直剖面，输出沿剖面距离增大的有符号视倾角。不得把走向传作倾向。
- `mapgis_prepare_borehole_chart` 生成可编辑点、线、区、结果 Excel 和绑定 JSON。柱状图以测深向下；剖面以指定起终点及高程投影，注明水平/垂直比例和纵向夸大。图上柱宽是排版宽度，原始 XYZ 仍在表中。

岩性样式编号由工程系统库解释。技术算例所用库的颜色 0/1 为黑色、9 为白色，花纹 1/2 为两种不同原生花纹；其他库须先查询，不能把这些编号当成通用颜色或岩性标准。未编录区间留白并标注，不插补孔间地层。

实际验证使用明确标为“非实测资料”的两孔算例，包括一个四分之一圆弧轨迹、一个铅直孔及 30–40 m 未编录区间。29 次真实 MCP 调用完成三类原生保存回读、两种图件的 MPJ 重开和实际输出。白底、轴线、花纹、比例和缺失区间均检查过最终 TIFF。密集花纹在 72 dpi 预览中可能出现相位走样；同一严格像素条件可在 300 dpi 下复核，不降低 EPS 内部转换分辨率，缺区或变色仍报错。

父进程在首个原生保存完成、结果表完成、完整清单封存后三处实际退出；随后 21 次 MCP 调用完成恢复、提交和回读。已完成保存的 PID、候选摘要与修改时间不变，仅继续尚未发出的阶段。修改源表或中间规格会被拒绝。

这是二维工程图接口。原生三维文件、测井专有格式、自动孔间对比和完整原厂地质模块仍未完成。

## 多页与分幅

`mapgis_prepare_map_series` 把同一份已保存工程的多个指定范围按顺序输出到一份 PDF。每页声明原生坐标范围和纸张毫米尺寸，调用现有 MapGIS 原生渲染器；PDF 合成使用现有 Ghostscript，保留单页 EPS，可选输出 TIFF。它同时复制原生 MPJ、依赖和工程系统库，便于继续编辑。

`pages.xlsx` 的“图幅”表和 `pages.csv` 保存页序、名称、范围、纸张尺寸及单页文件名；`series.json` 保留完整视图和摘要。页名写在索引中，需要印在图上的标题、页码等仍由源工程图形承担。原生 MPJ 本身没有因此变成专有多页版面文件。

实际验证在旧报告的八层窗口上输出全幅参考和两张分幅，纸张分别为 200×200 mm、100×200 mm、100×200 mm。16 次真实 MCP 调用核对了三页 PDF、EPS、300 dpi TIFF、Excel 索引、八层工程和迁移后的工程重开；各页 PDF 预览与原生 EPS 像素一致，图上比例相同。示例沿用原有示意局部坐标，不增加地面精度结论。

首张原生图完成、合成 PDF 提交、全部文件封存后三处实际中断，经 15 次 MCP 调用恢复；已完成原生页的 PID、PS、日志、摘要和修改时间保持，已提交 PDF 未重写。恢复后的 PNG/TIFF 与不中断结果逐像素一致。源文件、索引或中间 TIFF 被改动时会拒绝发布。

调用顺序见 [cartography-series.json](../workflows/cartography-series.json)。完整页面对象、覆盖图层驱动的图册规则和各打印设备路径仍在缺口表中。

## 系统库中的普通新色号

`mapgis_prepare_system_library` 支持 `{"kind":"color","action":"append","kcmy":[K,C,M,Y]}`，四个分量均为 0–255 整数。接口返回分配的新色号；不指定编号、不改动旧编号，0 号仍保留。新增记录为普通印刷四色，专色定义与字体编辑尚未接入。各图形参数原有的位宽限制仍有效，例如字节字段不能直接使用大于 255 的色号。

本机实际新增了 1576、1577、1578 三个色号。24 次 MCP 调用核对了原生文件保存重开、携库工程迁移以及 EPS、PDF、300 dpi TIFF；新色号与同 KCMY 的既有对照色号出图像素一致。父进程在库封存后退出，经 10 次 MCP 调用恢复，颜色数仍为 1579，没有重复追加。旧库及未修改记录、头部其他字段和不透明尾部保留。

## 地球化学实验

原始 XLSX → 数据核对 → 背景值与异常下限 → 原始采样点 WT → MapGIS 原生 IDW 格网 → 等值线与色带 → 可编辑 MPJ → EPS、PDF、TIFF、JPEG。

- `mapgis_table_info` 核对工作表、字段和行数。
- `mapgis_prepare_geochemistry` 逐轮剔除上侧 `Q3 + 1.5IQR` 异常值，按样本标准差计算 `C = 均值 + 2SD`。结果保留原始值、剔除轮次和行号。
- `mapgis_prepare_table_points` 使用全部原始观测值建点。用于统计背景值的筛选结果不替代插值输入。
- `mapgis_prepare_terrain` 的 `operation=native.idw` 调用本机 MapGIS DTM 库，保存 GRD，再由 GDAL 和另一个原生进程逐格核对。
- `mapgis_prepare_grid_vectors` 直接从该格网提取等值线、分带；可显式裁到采样点凸包，不重新插值。

实测数据包含 1,611 个样点、19 个元素。W、Sn 均生成了 `C/2C/4C` 与 `C/4C/16C` 两种分级的原生工程和实际输出。

原生 IDW 当前接入的是全点搜索、整数幂、不平滑的配置。`search_radius/1e8` 在该原生入口中表示重合点判断容差，不表示截断搜索半径。格网范围、节点数和各向异性须显式设置。统计筛选不证明数据服从正态分布。

调用顺序与检查项见 [geochemistry-course.json](../workflows/geochemistry-course.json)。

## 扫描地质图

扫描件 → 有界窗口 → 控制点/图像套合 → 原生 MSI → 色块与线条跟踪 → 剪断和清理悬挂线 → 造区 → 符号与注记 → 原生工程和输出。

- 大图通过 libvips 窗口读取。预览分辨率与原始像素坐标分别记录；需要细节时使用更高分辨率窗口和明确的分块核心区。
- `mapgis_match_scan_windows` 用 OpenCV SIFT/RANSAC 将局部扫描件与已配准参考图套合。其检查点参与过同一图像模型筛选，不能当作独立地面精度检查。
- `mapgis_trace_scan` 接受颜色、线宽、引导点和接断线参数，在路口或等长路径处暂停并返回选择。更改引导点使用新请求。真实曲线中的像素阶梯已避免被误判为路口。
- `topology.trim_dangles` 只处理明确标为造区边界的短叶枝，保留断层、岩脉和保护线；`topology.polygonize` 单独造区。
- 矿产符号等遮挡位置可以显式排除，按指定距离填补背景，并记录推断像素数；不由颜色推断地层时代或矿种。

实测原始扫描件为 47,958 × 47,903 像素。示例在有界窗口中生成了 54 个区、边界、10 个矿产点和 4 个产状符号。实际原生线文件重新造区后，区覆盖差为 0。该示例使用获准的示意局部坐标，未验证测量坐标或地面精度。

调用顺序与检查项见 [scan-geology.json](../workflows/scan-geology.json)。

## 外部表关联

`mapgis_prepare_table_join` 将 XLSX 字段关联到已有 WT、WL、WP 的新分支。关联保留全部物理记录和原字段，支持多个要素使用同一个属性键；Excel 键须唯一。字符串键严格匹配，数值键须显式选择数值比较。缺失键和空单元格默认报错，填充值由调用者指定。

该接口复用现有字段结构和逐记录编辑器，几何、样式、注记及所有非属性数据段保持字节一致。原始工作簿和原生文件的摘要一直检查到发布。实测包括 1,611 个原始采样点的逐值对照，以及地层表关联后 54 个区按“地层编号 + 100”设色、原生保存、工程重开和实际出图。

## 空间连接与分区统计

`mapgis_prepare_spatial_summary` 按空间关系给已有 WT、WL、WP 的新分支增加统计或关联字段。谓词方向是“左侧几何相对于右侧几何”：分区包含边界样点可选 `covers`，仅取内部可选 `contains`。一个样点归属多个区时，`unique` 报错，`all` 则明确计入每个区。

计数、总和、均值、极值、中位数、标准差、单值关联和去重字符串汇总均可使用。空组、缺失值和字段类型须明确，样本标准差默认 `ddof=1`；不会把无样品区域悄悄填成零。原字段、物理编号、几何和图形参数保持不变，双方输入的摘要检查到发布，并附物理匹配表与 CSV。

实际验证使用原来的 1,611 个样点和四个明确的演示网格。所有样点只归入一格，W、Sn 的均值、最大值和标准差与独立 NumPy 计算一致，统计结果经原生保存、工程重开和输出。演示网格没有地质分区含义。

## 颜色、符号和工程迁移

不要按固定色号猜颜色。本机 `Pcolor.lib` 可能经过编辑，组合绘制中的零号笔色也可能继承当前颜色。

`mapgis_system_library_info` 读取 `PCOLOR 3` 的 KCMY 分量。`mapgis_prepare_system_library` 在副本中替换既有色号，或用 `{kind:"color",action:"append",kcmy:[K,C,M,Y]}` 追加普通色号。分量是0..255整数，追加返回下一个活动编号，最多16384色；旧编号、额外通道和不透明尾部保持，新记录的额外通道为零。新增专色定义和字体库编辑尚未接通。

子图和图案的 `primitives` 接受折线、闭合多边形，以及 `arc`、`circle`、`circle_filled`、`circle_filled_outline`。圆类用 `center:[x,y]`、`radius`；圆弧还须给出 `0 <= start_angle < end_angle <= 360`，从正东逆时针计度。坐标和半径使用库内归一单位，`width_index` 是原生0..15线宽索引。图案中圆心须在0..1单元内，避免原生读取器自行夹取坐标。

原版两个渲染器使用不同填色字段：子图圆以 `color` 同时控制填色和边线，不接受会被忽略的 `fill_color`；不同颜色可显式组合一个 `circle_filled` 和一个 `circle`。填充图案的实心圆及带边线实心圆分别使用 `fill_color`、`color`。折线和多边形保留原有字段。查询返回已识别的圆弧参数和原始流，未知图元继续保留；简单线型仍用 `dash_fraction`。

新建 MPJ 时，可在 `parameters.system_library` 传入库目录和指纹。库文件作为兄弟文件随工程发布，`.mpj.agent.json` 将其绑定到准确的 MPJ。Agent 后续编辑、迁移和出图会自动保留并选择这份库。另一软件窗口的系统库设置需单独配置。

## 地形分析

`mapgis_prepare_grid_analysis` 对已保存的原生 GRD 计算坡度、坡向、晕渲、地形崎岖度 TRI、地形位置指数 TPI 和粗糙度。计算复用随 Rasterio 分发的 GDAL 3.10 公共接口；不会将这些算法称为 MapGIS 专有 DTM 算法。

输入须有明确的线性 XY 单位和高程单位。默认 Horn 梯度、Riley TRI、完整 3×3 邻域；边缘及缺测点周围保留缺测值。`compute_edges` 显式允许 GDAL 插补邻域。平地的坡度零值有效，坡向默认未定义；有效 TPI 值为 `-9999` 时另选缺测标记，避免丢失真实数值。

实测用 `z=100+0.1x+0.2y` 的 49 点生成原生 GRD，六类结果均转成 MSI，由新原生进程回读全部像素、掩膜及真实坐标范围，再组成 MPJ、保存重开并输出 EPS/PDF/TIFF/JPEG。原生 GCP 接口内部按地图坐标在前、图像坐标在后传参；外部接口继续使用 `pixel`、`world`，并核对实际 MSI 范围。旧版本生成的附带坐标文件不能单独证明 MSI 内部范围正确。

`mapgis_prepare_terrain` 的 `operation=native.tin` 使用固定修订的 CDT 2.0.1 做约束三角剖分，再调用 MapGIS 文件接口保存真正的 `.TIN`。约束线以原始 WT 的物理点号指定，交叉处须已有明确高程的节点；结果覆盖采样点凸包。另一个原生进程逐点、逐面读回，检查邻接顺序、重叠、覆盖和约束边。高程来自输入点，坐标系及单位绑定在 `.tin.json` 中，不能丢掉该文件后继续假定单位。现有 DogServer67 虽已运行，原专有剖分模块仍出现许可服务器异常；CDT 路线没有修改许可校验。

`mapgis_prepare_tin_edit` 支持节点高程、XY 移动、插点、删点、替换约束和交换一条内部边。高程修改保留原连接关系，移动和增删节点重新剖分并记录点号映射；约束端点不能直接删除。换边要求原两面组成凸四边形，拒绝修改受约束边。所有操作输出新原生 TIN，并由新进程检查保存结果。`mapgis_prepare_tin_vectors` 从保存后的网格导出 WT 节点、WL 网边和 WP 三角面，保留原始 Z 和点号。

`mapgis_prepare_tin_profile` 直接沿保存后的三角面取剖面，保留换边和约束对结果的影响。采样包括指定间距、路径转折和三角边交点；离开模型范围时默认报错，也可明确保留缺口。CSV/JSON 保留原单位的距离、高程和源坐标；剖面 WL 统一换算为米，垂直夸张单独记录，并附可编辑 WT 采样点。它输出的是地形剖面，不推断地层或钻孔解释。

`mapgis_prepare_viewshed` 从高程栅格计算可视域：指定投影米制坐标、观察点、垂直单位、离地观察/目标高度、距离和曲率系数。默认平面模型，观察位置取所在像元中心。输入要求北向上正方形像元、分析窗口高程完整且有限；GDAL会忽略输入NoData，因此这里明确拒绝缺测值。数值结果为0不可见、1可见、2距离范围外，另附RGB显示栅格。MSI数值回读保留像元、掩膜和范围；工程出图使用独立显示栅格。完成算法结果可在父进程退出后接管，不重算GDAL；算法未写完或缺少完成记录时使用新请求，残件保留。

`mapgis_line_of_sight` 检查两点间的平面通视，返回首个遮挡像元、净空及可编辑的平面连线和端点。默认只返回摘要；`include_samples` 返回逐像元高程与净空，`include_profile` 返回独立米制剖面坐标下的地形/视线。两种坐标不能混用。它适配固定提交的MIT版GDAL通视源码，采用上游“地面相等可见”的修复；本机3.10 DLL未改动。点位取所在像元中心，不模拟植被、建筑和地球曲率。原生点线、工程和出图继续使用现有创建、另存、工程和导出接口。

`mapgis_prepare_hydrology` 复用 PyFlwDir 0.5.12 的 D8 算法，生成填洼高程、填深、流向、上游像元数、汇水面积和流域编号。输出还可带可编辑的出口点 WT、流向线 WL 和流域区 WP。上游像元数包含当前像元；方向0是有效出口，247是缺测。原生 MSI 及点线区保存重开、工程迁移和实际出图已验证。

调用方明确选择 `depression_handling=fill`、边界出口策略和流向线阈值。缺测边界默认报错；`nodata_boundary=open` 将其作为开边界。上游算法使用float32高程，默认要求转换精确；容许舍入时填写 `max_elevation_rounding_error_m`。它输出地形排水模型，不能据此当作实测河流或降雨径流结果。

Windows版服务在启动时预加载水文科学库，再启动stdio输入读取。首次导入SciPy扩展的挂起已通过真实MCP对照复现并修复，见 [SDK上游讨论](https://github.com/modelcontextprotocol/python-sdk/issues/2832)。依赖、输入和已完成算法结果均保留摘要；中断后接管完成结果，再完成文件封存。

完整参数及检查顺序见 [terrain.json](../workflows/terrain.json)。其他地形模型和参数继续按剩余清单接入。

## 扫描件逐段跟踪

`mapgis_trace_scan` 保留锚点引导与沿方向跟踪到路口的接口。`mapgis_seed_trace` 增加可重复的分支选择：先指定窗口内种子、颜色与线宽约束，遇到分支返回选择编号；再次调用时带上图摘要和选择序列。调小 `stop_after_segments` 可以退回，修改后续选择后再续跟。所有结果仍是待接受的线稿，不自动赋予地质含义。

新接口沿用已固定的 MIT GeoScan 细化和骨架路径内核。文本或保护区可显式排除；断线只有提供两个端点及原因时才连接。闭环从中间种子出发仍保持完整环路。实际扫描件已完成退回、续跟、原生 WL 保存重开及工程出图；受控十字线另经真实 MCP 验证分支暂停、选择和改路。原有跟踪实现与测试保持原摘要。

## 原生 MSD 影像库

`mapgis_msd_source_info` 返回索引中的影像及完整依赖摘要，`mapgis_native_msd_info` 在独立进程读回原生范围，并可按外包矩形查询成员。`mapgis_prepare_native_msd` 创建库，`mapgis_prepare_msd_edit` 按一开始的位置编号删除并追加影像，结果都是新的可迁移目录。

使用前须确认成员具有共同的原生物理坐标；已知 CRS 冲突会拒绝组合，未知 CRS 不会被猜测。旧 DLL 关闭数据库可能自动保存，因此读取也只打开本项目副本。已实测空库、两成员、增删、删除全部、范围查询、目录迁移、原生工程与 EPS/PDF/TIFF/JPEG。MSD6.0 这里是保存的 MSI 影像集合，其他数据库和所有多尺度切换功能仍须分别接入。

## DXF 交换

`mapgis_prepare_dxf_export` 把指定 WT/WL/WP 写为独立 CAD 图层：注记对应中文 TEXT，线对应 LWPOLYLINE，区和孔洞对应 HATCH。必须给出共同坐标背景与绘图单位；米、毫米等已知单位明确换算，未知单位不猜测成米。导出后读回全部几何与文本，并报告原生符号、线型、图案、字体和锚点在 CAD 中的显示差异。

`mapgis_dxf_source_info` 列出模型空间的图层、实体、单位与整组依赖。`mapgis_prepare_dxf_import` 按图层和一种几何类型生成原生文件，曲线需要明确的源单位细分容差；圆弧直接用 ezdxf 的弦高细分，含弧折线/HATCH 的 Bézier 转换误差单列说明。非零高程、未支持的块和文字布局会明确拒绝。

未经修改的完整往返还带有摘要绑定的原生图层副本。核对实际 DXF 的几何和文本后，可逐字节恢复一个完整原图层，保留物理编号、未知字段及原比例/样式。这里不会把保留字节猜成某个可写图形参数。修改后的 CAD 文件可明确关闭原生元数据恢复，按标准几何和文本新建；这一路不承诺恢复原属性和专有样式。整组文件不能只拷走 DXF 后仍假定原生元数据存在。

实测旧报告7个矢量图层、6956个实体经独立 GDAL 读回、逐层原生重开与语义核对，再携带原工程系统库生成 MPJ 和 EPS/PDF/TIFF/JPEG。DWG、块、纸空间及全部 CAD 外观映射继续保留在剩余清单。

## 中断恢复

请求 ID 对应一次明确操作。返回丢失后，先查请求或恢复会话，再决定下一步。

- 完成的原生 IDW 可以从子进程回执恢复，并重新读回 GRD；不会重新计算一次。
- 完成的原生 TIN 创建和编辑也已测试真实父进程退出后的接续：重新核对原件和保存结果，沿用原生生成进程回执，不重复计算或编辑。
- 完成的原生 MSD 创建也已实测父进程退出后的接续；原生成进程、索引和日志保持不变，恢复时只重新检查并发布。
- `mapgis_prepare_plot_export` 的新版请求按 PDF、TIFF、JPEG 分别保存检查点。返回丢失后调用 `mapgis_reconcile_artifact`：完整成品保留；准确匹配的转换进程仍在写入时返回 `RUNNING`；缺失或残缺格式在独立目录补生成。`reused_formats`、`newly_converted_formats` 和 `conversions` 会说明实际处理，原生工程和 EPS 不重新编辑。
- 导出源摘要、EPS 副本、转换器及成品被改动时会拒绝接管。完整但缺少进程结束回执的 PDF 重新渲染对照；TIFF/JPEG 会与单独诊断转换逐像素核对，原成品不覆盖。已封存的旧版请求仍可恢复，缺少这些上下文的旧版未封存请求需要新的请求 ID。
- `mapgis_project_workspace` 保存多层选区、未保存缓冲区和修订号。实测在原生编辑已持久化、MCP 回复前退出服务后，选区和检查点恢复，点移动只发生一次。
- 工程保存后再次原生重开，核对几何、属性和自带库。关闭自有会话前处理 dirty 状态。

这些恢复针对 Agent 自有会话；另一个编辑器中的任意未保存内存没有相同保证。

导出流程另做了四处真实父进程退出：首份 PDF 转换完成、转换进程仍运行、全部格式完成但未封存、转换完成但未写进程回执。18 次 MCP 调用验证了接管、补齐和提交，恢复后的 TIFF 均与正常基准相同；运行中案例留下的空 PDF 保留在原目录，原进程结束后另开转换。缺少结束回执时不会宣称已知原进程退出码。

原生部分保存已接入 `mapgis_prepare_native_subset`：按选区物理编号生成新 WT/WL/WP，并返回新旧编号映射；属性 ID 可以重复。原生 CopyList 负责几何和区拓扑，主属性记录与完整169字节地图参数直接保留源字节，再由 BAS60 保存和另一进程重开核对。已知 WP 弧段表和节点表分别按弧几何/区所有者、端点/关联弧唯一映射，恢复原厂复制遗漏的节点编号后迁移原属性字节。孔洞、共享边、相邻未选要素、注记和图形参数均检查。未知非空属性段、歧义映射和未核验的节点前导格式会报错；已核验的节点前导最多65535号。真实图例14区、含非空节点属性的共享边/孔洞样本已通过MCP复制变换、原生重开、输出及中断恢复。旧报告7图层的既有部分保存证据保留。

可选 `transform` 将选区另存与原厂变换接口连起来：平移、旋转、缩放的顺序明确指定，角度以度表示，负比例可镜像；平移量用源文件的 XY 单位，后台换算为原生绘图坐标。保留原坐标元数据、注记和图形参数，不将平面图形变换当作 CRS 投影转换。真实报告54区、边框线及63条注记已完成变换、原生重开和输出，镜像请求在父进程中断后也能恢复且不重放。一次原生进程异常保留了失败记录，后续阶段日志和独立调用未能确定其根因；未完成请求不能伪装成已完成回执。

`mapgis_native_project_parameters_info` 读取已保存工程的29个地图参数，返回原生字段说明、单位与投影枚举。`mapgis_prepare_project_parameters` 将明确指定的字段保存到新工程，保留所有图层字节、图层状态、显示范围和版面。含扫描底图的8层地质工程与7层课程工程均已实测保存、迁移重开和输出；原生保存后、复制交付文件后两处父进程中断均能接续，不重复修改。它修改工程元数据；图层坐标转换仍使用单独的投影工具。

## 原生图例

`mapgis_prepare_native_legend` 保存真正的 CLN 图例目录，并通过原生版面导出器生成可编辑 WT/WL/WP。图例项明确提供点、线或区类型、名称、说明、分类码和样式；`mapgis_native_legend_info` 在另一个原生进程读取这些内容。名称绘制在图上，说明保留为 CLN 元数据。修改时读回项目后生成新目录，不改原图例。

版面参数包含非空标题、矩形范围和列数，使用原生绘图毫米、比例尺1。和实际地面坐标的图层合成前须做明确变换；CLN目录本身不等于整套页面对象，版面参数及输出摘要另存在 `.legend.json`。点符号、线型、图案和色号取决于所选项目库，不能按固定色号猜颜色。

实际验收包括混合点线区和旧报告14个地层类别，原生MPJ重开、EPS/PDF/TIFF输出，以及图例修订后原件不变。已验证原生生成后、交付文件部分复制后两处MCP父进程退出，恢复沿用原生成PID、CLN和WT/WL/WP，不重新生成。原厂COM图例项的Add/Type/PntInfo及部分SaveAs路径存在实测缺陷，因此复用原厂mapleg60导出API和正常参数转换；没有修改安装DLL或许可校验。

图例可直接按目标绘图范围生成，再加入地图工程；也可通过部分保存的显式变换移动其点线区，已知附属属性会按原生身份映射核对。文字位置参与变换，字高、角度等图形参数保持原值。实际旧报告的8层工程加入图例后已形成11层，加入标题、箭头和比例尺后为16层，均完成保存重开和输出。

WP 的弧段、节点属性也使用 `mapgis_prepare_attribute_schema`。`table` 默认为 `primary`，设为 `arc` 或 `node` 时编辑对应的辅助表。`fields` 仍是完整目标字段列表；空表初始化时，每个字段都明确给出类型和默认值。`attribute_updates` 使用物理编号字符串，为目标字段逐条赋值，也可以给改名或新增后的字段赋值：

```json
{
  "table": "arc",
  "fields": [
    {"name": "ID", "source": "ID"},
    {"name": "审核", "type": "string", "length": 20, "default": "待复核"}
  ],
  "attribute_updates": {"1": {"审核": "首项已核"}}
}
```

调用时另带工作副本的 `file`、当前 `expected_sha256` 和稳定 `request_id`。`mapgis_read_wp_arcs` 返回弧段/节点的属性值及 `attribute_tables` 字段列表，缺少存储行时为 `null`。闭合的原生图例弧可以没有端点节点编号，这类弧列在 `unassigned_closed_arc_ids`，不能凭坐标虚构节点属性。

实际验证已通过MCP从空表建立弧段/节点字段，赋中文、数值、日期和时间，保留重复属性ID，原生保存重开后复制变换；另编辑真实14地层图例的弧段审核字段，前后原生出图的300dpi TIFF像素完全一致。普通原生 `resave` 也检查非空辅助表的字段描述和原始记录字节。

`mapgis_prepare_creation` 的 WT 还支持原生参数圆和圆弧。GeoJSON `Point` 给出圆心，`style` 中的 `point_type=2` 表示圆，`point_type=3` 表示圆弧；它们直接保存原生参数，没有用折线逼近：

```json
{
  "type": "Feature",
  "id": "arc-1",
  "geometry": {"type": "Point", "coordinates": [80, 20]},
  "properties": {"ID": 7},
  "style": {"point_type": 3, "radius_mm": 10, "start_angle": 30, "end_angle": 150, "pen_width": 0.3, "color": 1}
}
```

半径和笔宽使用绘图毫米；圆心使用模板声明的地图坐标。角度从正东起逆时针，当前支持 `0 <= start_angle < end_angle <= 360`。圆可指定 `filled=0/1`、`border_color`；颜色仍按实际项目库解释。模板须为投影坐标或明确的本地绘图坐标，地理坐标下的圆及跨零度角暂不在此配置内。

`mapgis_read_features` 返回圆心和这些参数，并明确 `geometry_semantics`。`mapgis_prepare_patch` 可改圆的边色、填充、笔宽，或圆弧的起止角、笔宽，以及两者的颜色/层。半径使用 `mapgis_prepare_native_operation` 的 `operation=point.radius`，参数为 `{"updates":[{"record_id":1,"radius_mm":5}]}`：BAS60 更新图元和边界框，保存后逐项核对圆心、属性、其他参数和新范围。该操作也能在持续原生缓冲区中编辑、保存和恢复。字节补丁仍拒绝只改半径而留下旧边界框。

原生部分保存可按明确变换移动圆心并保持图形尺寸。GEOS 文件操作会拒绝将这些圆心误作完整曲线；任意曲线/椭圆及其他格式交换仍需各自的表示转换。

本配置已有原厂SDK图元记录的字节比对、BAS60保存与新进程重开、31次实际MCP创建/编辑/复制/工程输出和父进程退出85后不重放的验证。320×80mm页面上，标称40mm的圆在300dpi TIFF中按两侧笔画中心测得40.042mm，两像素容差0.169mm；绘图毫米也在EPSG:4549、1:50000的非单位比例文件中核对。

半径编辑另有19次实际MCP操作：新范围、圆/弧参数、未保存状态、保存关闭和原生输出均核对；改为5mm半径后，图上的标称20mm直径测得20.063mm。原生保存完成后实际退出MCP父进程（90），重启通过6次成功调用恢复，原原生日志/候选/PID保持，未重复编辑。半径与既有会话/几何接口的相关回归62项通过。

## 线和区边界平滑

在 `mapgis_prepare_native_operation` 中使用 `line.smooth` 或 `arc.smooth`：

```json
{"operation":"line.smooth","parameters":{"record_ids":[1,2],"max_chord_error":0.01}}
```

WP 使用 `arc.smooth`，将 `record_ids` 换成 `arc_ids`。另带工作副本 `file`、当前摘要 `expected_sha256` 和稳定 `request_id`；准备后用原有 `mapgis_apply_patch` 发布新文件。持续缓冲区的 `edit` 接受相同操作和参数。

算法复用 [SciPy 的三次 B 样条插值](https://docs.scipy.org/doc/scipy-1.16.2/reference/generated/scipy.interpolate.make_interp_spline.html)：以弦长为参数，开线采用自然边界，闭合线采用周期边界。输出为可编辑的原生折线顶点，BAS60 完成更新、保存和独立进程重开。原始插值点和两端坐标保留，WP 的共享节点、弧段号、左右区、孔洞及未选边界保持一致。

`max_chord_error` 是曲线转折线的误差限，单位与输入地图坐标相同；它不限制平滑曲线相对原折线的位移。采样依据每个三次区间的二阶导数上界，弦误差不超过 `max(||C''||) * h² / 8`，另预留坐标舍入量。回执保存边界条件、输入/输出顶点数和误差上界。当前接受投影或明确的局部坐标；不把这一路线说成原厂 GUI 的同一种平滑算法，也不生成专有参数样条对象。

几何编辑期间使用原厂 `_SetAutoCalDefAttFlag` 关闭默认属性自动计算，随后恢复原开关。几何、样式、主属性与非空弧段/节点属性都分别检查，避免旧版默认字段计算改写用户的附属属性。零字段表加载后留下的空数组则在自有进程关闭前规范化，并使用原分配器释放；此修复绑定 BAS60/BASROOT 的已核对版本，不改安装 DLL。

已有42次实际MCP验证：自然开线、周期闭合线、共享区边界和孔洞、非空附属属性、未保存/保存关闭，以及两组MPJ/EPS/PDF/TIFF，其中一组来自旧报告的54个区。另实际退出两个MCP父进程（91/92），各用6次成功调用恢复原候选，日志、原生PID和候选摘要不变，没有重新插值或重做编辑。相关74项回归通过；原厂参数曲线、椭圆和其他表示仍列在缺口记录。

## 按长度延长或缩短线端

`mapgis_prepare_native_operation` 的 `line.resize` 使用源地图单位的弧长：

```json
{"operation":"line.resize","parameters":{"updates":[{"record_id":1,"start_delta":2,"end_delta":-1}]}}
```

上例将线的起点向外延长2个单位，将终点向内缩短1个单位。正值向外、负值向内；范围内裁线复用已有的 [Shapely substring](https://shapely.readthedocs.io/en/stable/manual.html#substring)，范围外沿第一个或最后一个非零线段的方向延伸。允许结果完全处于原线范围之外，但沿延伸后的线，终点必须仍在起点之后。当前处理明确的平面坐标，不自动吸附其他边界。

结果通过BAS60更新已有线的顶点，保留物理编号、属性、样式和未选线。持续原生缓冲区接受相同的 `edit` 参数；原有应用、保存、恢复接口不变。14项解析用例覆盖本地/投影比例尺、两端延长缩短、越过原始范围、源字节恢复和未保存缓冲区丢失。18次实际MCP验证包含原生MPJ/EPS/PDF/TIFF和dirty保存关闭；实际退出父进程93后，6次MCP恢复原候选且不重复编辑。相关45项回归通过。

按明确目标延伸时使用 `line.extend_to`：

```json
{"operation":"line.extend_to","parameters":{"updates":[{"record_id":1,"end":"end","max_distance":10,"boundary":{"type":"LineString","coordinates":[[0,7],[6,7]]}}]}}
```

`end` 选择 `start` 或 `end`。目标可以是二维点、线、多线或区，多边形按边界处理；后台选择端点前方、给定距离内的最近交点。已经接到目标时保持不动，找不到交点时在原生编辑前报错。这里的目标由调用方明确给出，没有根据相邻图形猜测连接关系。已有18次实际MCP、原生工程输出、dirty保存关闭、父进程退出94后的6次不重放恢复，以及55项相关回归。

## 图形参数和属性互转

`mapgis_prepare_patch` 的 `style` 对选中记录统一赋值，`style_updates` 则按物理编号分别赋值：

```json
{"record_ids":[1,3],"style_updates":{"1":{"color":3},"3":{"color":5}}}
```

仍需传入文件、当前摘要和请求编号。逐记录映射的键必须是选中的规范物理编号字符串，属性中的 `ID` 可以重复。统一样式与逐记录样式不能覆盖同一字段；属性更新与图形更新可以在同一个字节账本里提交。WT 圆/弧按每条完整的合并配置核对，半径继续走原生 `point.radius`。

参数转属性可先用 `mapgis_read_features` 读取 `style`，再将相同物理编号的参数值送入 `mapgis_prepare_attribute_schema.attribute_updates`，给新字段赋值。反向则将属性值送入 `style_updates`。当前可写字段以现有验证配置为准；没有把尚未核验的字体、字号或所有原厂字典字段一并宣称可写。

真实54区报告已通过29次MCP互转：保留原字段和几何，将原色号存入属性，按另一属性逐区设色，BAS60保存重开及MPJ/EPS/PDF/TIFF出图，再由原色号属性恢复。恢复后的300dpi TIFF与原图逐像素一致。演示设色使用明确的编号规则，不产生新的地层解释。另有11项新增用例及97项相关回归，覆盖WT/WL/WP、重复属性ID、未选记录保持、圆弧配置、冲突和撤销。

## 面积加权空间汇总

`mapgis_prepare_spatial_summary` 可以按区与区的相交面积统计。传入两个原生WP及摘要，明确共同平面框架、谓词和归属规则，设置 `weighting="intersection_area"`，再选择新增字段：

```json
{"weighting":"intersection_area","columns":[{"name":"均值","operation":"weighted_mean","field":"浓度","empty_value":-999},{"name":"分摊量","operation":"prorated_sum","field":"总量","empty_value":0},{"name":"覆盖面积","operation":"coverage_area"}]}
```

完整调用仍需 `predicate`（通常为 `intersects`）、`match_policy`、必要的 `crs` 和请求编号。加权均值为 `Σ(值×相交面积)/Σ相交面积`；按面积分摊总量为 `Σ(总量×相交面积/源区面积)`，后者明确假设源区内总量均匀分布。`coverage_area` 对交集求并集，源区重叠处只计一次；统计权重仍逐源要素计算。目标区彼此重叠时，每个目标分别接受分摊，不能把它们的和当作全局守恒量。

纯边界接触面积为0，不计入面积模式的匹配数或归属。`nulls="skip"` 会从加权均值的分子和分母同时移除缺失值；空组的均值、分摊量需要明确的 `empty_value`，空覆盖面积为0。侧车保留每对物理编号的交集面积和源面积比例，以及公式、单位和剔除接触数。

计算复用 [Shapely 相交](https://shapely.readthedocs.io/en/stable/reference/shapely.intersection.html)及[并集](https://shapely.readthedocs.io/en/stable/reference/shapely.union_all.html)，不隐式修复或吸附几何。面积单位为源平面坐标单位的平方；[地理经纬度不能直接作平面面积权重](https://geopandas.org/en/stable/docs/reference/api/geopandas.GeoSeries.area.html)。明确的示意本地坐标可验证算法，不能据此认证实际地面面积。

实际54区/4目标已通过24次MCP，与独立GeoPandas叠加及NumPy逐区核对，示意总量6885在分摊后守恒；原生WP、MPJ和EPS/PDF/TIFF保存重开及输出均通过。另有69项相关回归。父进程在完成清单封存、账本提升前实际退出95后，以7次MCP恢复，原候选和清单的摘要、时间戳不变，没有重算。

## 固定版面的标题、方向和比例尺

`mapgis_prepare_map_frame` 增加 `page.title`、`page.north_arrow`、`page.north_label`、`page.scale_bar` 和 `page.scale_labels`，分别输出可编辑 WT 或 WL。位置以纸张左下角毫米指定；提供与出图相同的 `view` 后，后台按相同的等比缩放和居中关系换算坐标、字高和线宽。图层包含稳定的元素编号和类别，可用现有工程编辑接口添加、替换、排序或删除，并原生保存重开。

这组接口使用固定出图范围和恒等全局工程变换。模板为明确命名的原生绘图局部框架，单位声明为 unknown、比例参数为1，参考摘要标识对应底图；没有把这些页面坐标当成 Earth CRS。比例尺必须给出 `metres_per_native_unit`，方向箭头只接受明确声明的角度，不推测真实北向。旧报告的演示坐标可用于验证操作能力，不能据此认证真实地面距离。

五类元素已通过实际MCP、BAS60保存重开、16层原生工程及EPS/PDF/TIFF输出。300dpi文件中，25mm箭头的含笔画范围约25.23mm；几何长度44.33mm的比例尺，含笔画范围约44.62mm。两项误差均在0.2mm笔画加两个输出像素的范围内。原地图及项目库保持不变；新的版面内容在原生图层中保留，不是叠加到截图上的图片。

## 仍在接入

两条流程通过不等于所有 MapGIS 模块已完成。其他 TIN 记录类型和地形模型、地图库/影像库、三维场景和飞行、原生网络与动态分段、地质剖面/柱状图、测量地籍、原生报表/多尺度文件，以及字体、专色和其他设备路径仍有未实现或未验证项。能力目录中的 `DISCOVERED` 项仅表示找到了入口。

## 混合工程的窗口裁剪

先用 `mapgis_native_project_info` 核对已保存的工程及依赖，MSI 经 `mapgis_prepare_native_raster(operation="to_tiff")` 导出。`mapgis_prepare_raster_crop` 接受 `window=[列,行,宽,高]` 和 `template={name:"crop.tif"}`，按整数像素裁剪所有波段，不重采样、不猜坐标。旋转仿射平移像素原点，GCP 平移像素位置而保留世界控制和 CRS；NoData、各波段掩膜、调色板、比例/偏移、单位及波段说明保留。外部依赖需传 `raster_source_info` 返回的完整摘要；整图统计标记被移除。实现复用 [Rasterio Window](https://rasterio.readthedocs.io/en/stable/topics/windowed-rw.html) 和[原生掩膜](https://rasterio.readthedocs.io/en/stable/topics/masks.html)。

GCP 可能落到新像素矩形外，裁剪接口不会编造新控制点。原生 MSI 需要范围内控制时，调用者必须明确提供经已验证仿射拟合得到的角点；这不增加真实测量精度。按同一裁剪足迹调用 `mapgis_prepare_file_operation(operation_id="overlay.clip")` 处理 WT/WL/WP，保留主属性、支持的图形参数、文字和来源映射。边界接触只保留原几何维数；源文件摘要检查持续到发布。新物理编号可变，区拓扑结构索引重建；辅助弧/节点属性没有自动迁移。WT 选取以锚点为准，精确圆弧不能当成中心点裁剪。

用 `mapgis_prepare_project_edit` 的 `{source_index,replacement:{file,expected_sha256}}` 替换新分支内的图层，保留项目元数据。外部便携工程可先用工程参数复制接口对一个既有值作同值赋值来建立自有副本；原29项参数、层序/状态/显示限值/环境均需再次读回。全空的层用正常原生删除保留空层，不伪造空文件。

原报告八层 MSI/WT/WL/WP 已完成63次实际MCP调用：900×900原像素与掩膜相同，矢量层分别为21区、2399线、6符号、1产状、23文字及两个空层；主属性、支持图形、文字和独立交集几何核对，原生保存重开、EPS/PDF/TIFF通过。29项工程参数及图层布局保持，预览已查看。实际父进程退出96/97后15次MCP接管原候选，全部清单/文件摘要与时间戳不变；相关91项回归。此范围不等于任意原生层类、未知附属表或全部图形足迹的裁剪。

## 注记、字体与符号参数

修改已有普通注记或符号，调用 `mapgis_prepare_native_operation`，再以原有 `mapgis_apply_patch` 发布。`operation="point.style"` 接受 `parameters={updates:[{record_id:1,style:{height:6,width:4,font_zh:2,font_en:1,angle:15}}]}`；普通注记0支持字体、字形、间隔，符号1支持子图号和笔宽，共同支持尺寸、角度、颜色和图层。尺寸沿用文件的原生参数，坐标不变；角度为度。字号/符号尺寸变化通过BAS60 Get/UpdatePntInfo重算显示边界，保留非目标图形字节、原文本和属性。先读当前值和项目库，再选择数字字体/符号号；接口不枚举全部可用字体。

修改文字使用 `operation="point.text"` 和 `parameters={updates:[{record_id:1,text:"灰岩T#-2#=d"}]}`。物理编号保留，中文GBK和原生格式标记原样存储，BAS60重新计算边界；不重建整层。原XYZ及62字节图形参数逐字节保持。普通注记内容须非空、可编码为GBK、无NUL；每条最多32767字节，每批最多1MiB，超限拒绝而非截断。删除注记用 `feature.delete`。其他富文本/嵌入对象子类型没有由这条接口转换。

两项都接入 `mapgis_native_workspace` 的 edit/save/recover，支持未保存状态和已完成检查点的子进程丢失恢复。旧报告字体/尺寸修改实测39次MCP，文字增长/缩短及格式标记替换实测30次MCP，均原生重开、输出并恢复；恢复后的300dpi TIFF与原图逐像素相同。实际父进程退出98/99后分别以6次MCP接管原候选，没有重放编辑。相关原生文字修订96项回归通过。

## 向已有点线区文件追加选区

`mapgis_prepare_native_subset` 不传 `target` 时仍是部分保存；传入已有文件与摘要时，将选区追加到该文件的新分支：

```json
{"file":"source.wp","expected_sha256":"源文件摘要","record_ids":[2,4],"target":{"file":"base.wp","expected_sha256":"目标文件摘要"},"template":{"name":"merged.wp"},"request_id":"append-01"}
```

源与目标必须同型，完整原生坐标参数和属性字段布局须一致。`record_ids` 是有序且唯一的活动物理编号，属性中的 `ID` 可重复。结果保留目标已有活动编号，返回追加记录的新旧编号映射；两个输入都不写入，摘要检查持续到 `apply_patch` 发布。需要旋转、平移或缩放时，先用原有部分保存变换得到新选区，再追加。

后台使用原厂 `CopyList` 向已加载的目标复制几何，按明确映射保留主属性及已知 WP 弧、结点属性原字节，再经 BAS60 保存和另一个原生进程重开。删除后保留的孤弧、空活动目标、旧区边界和节点前导偏移有专门校验；没有自动吸附、合并边界或去重。未知辅助段、歧义的重合孤弧和嵌入对象需要另行映射，不用几何相似猜测属性归属。

旧报告八层工程中，21区、2399线、6符号和23注记已拆分后追加重组，31次实际 MCP 完成 MPJ/EPS/PDF/TIFF，300dpi TIFF 与原图逐像素一致。父进程在复制落盘后实际退出85，4次 MCP 接管原结果，没有重放 `CopyList`。原85项相关回归及后续50项含删除后追加、部分保存、拓扑和工作区的检查分别留证。追加当前输出新文件分支；未将它描述为修改其他编辑器的未保存内存。

要在未保存会话中继续追加，使用 `mapgis_native_workspace` 的 `edit`：

```json
{"action":"edit","workspace_id":"buffer","expected_revision":2,"request_id":"append-02","parameters":{"operation":"feature.append","parameters":{"file":"source.wp","expected_sha256":"源文件摘要","record_ids":[2,4]}}}
```

目标是该会话的最新检查点，已有 dirty 修改和最后保存的基线都保留。原生复制结果核验后，后台正常关闭旧的自有子进程，用新子进程加载结果，返回 `buffer_restarted=true`；工作区编号不变，修订、请求回执和 dirty 状态一起提交。输入错误在停下原缓冲区前拒绝，输入摘要检查持续到追加提交。保存后可关闭重开，放弃则恢复最后保存的基线。`mapgis_project_workspace` 的 `edit` 使用相同操作与参数，并明确目标 `index`；此处 `record_ids` 属于输入文件，不能用目标图层的 `use_selection` 代替。

新图元按追加后的物理顺序绘制。旧报告八层工程经36次实际 MCP 完成四层多选、删除再追加、保存重开及原生输出；相对原顺序有297个边缘像素变化。另用14次 MCP 将原始数据安排为相同的 `[2..n,1]` 顺序，TIFF 与追加结果逐像素一致，确认了绘制顺序的影响。实际父进程在复制完成、dirty 状态尚未提交时退出85，7次成功调用恢复了追加及先前修改，原复制文件、日志、旧 dirty 检查点的摘要和时间戳不变。

实图验证还定位并修复了自定义属性表的原生删除问题：部分加载后的 `CATT_STRU` 记录起点仍对应旧字段布局，删除标记会误写未选记录。独立后端核对原始模块、实际属性流头、字段数、记录宽度和预留记录后，仅在调用期间修正自有结构的起点，结束后还原。原 DLL 不变，未删记录仍逐字节核验。59项相关回归覆盖点线区、扩展字段、零字段、dirty 会话、请求重试和故障恢复。

## 由控制点求变换参数

`mapgis_fit_control_transform` 只读求解，不修改文件。`translation2d` 为二维平移，`similarity2d` 为四参数，`affine2d` 为完整六参数仿射；`translation3d` 为三维三参数平移，`similarity3d` 为含正比例和正规旋转的精确七自由度模型。输入必须明确是共同单位的平面或三维直角坐标，经纬度不直接参加这种拟合。

```json
{"model":"similarity2d","controls":[{"source":[0,0],"target":[100,200]},{"source":[10,0],"target":[110,200]}],"coordinate_frame":{"kind":"planar","unit":"unknown","axis_order":"xy"},"max_fit_error":0.001,"checkpoints":[{"source":[5,5],"target":[105,205]}],"max_checkpoint_error":0.001}
```

控制点最少分别为1、2、3、1、3个，必须唯一且足以确定所选模型。拟合残差与未参与拟合的检查点分别报告；不能把同一个控制位置再当独立检查点。误差阈值沿用明确的坐标单位，没有真实测量资料时不转成地面精度。可另传 `points` 和 `direction="forward"|"inverse"` 批量正反算。

返回的齐次矩阵按列向量使用，二维 `affine_matrix` 遵循 Shapely 的 `[a,b,d,e,xoff,yoff]` 顺序。`native_subset_transforms` 可接已有的 `mapgis_prepare_native_subset(transform=...)`：平移/四参数为一次调用，完整仿射经 SVD 分解为两次原生旋转、带符号缩放和平移，包含剪切或镜像。按返回次序执行，每一步使用上一步的新文件、摘要和物理编号映射。保留原有坐标元数据、图形参数和属性；它是所声明平面框架中的几何变换，改变地理基准须另用明确的坐标转换流程。参数曲线作为 WT 锚点和原图形参数处理，未将非等比缩放后的圆伪称为原厂椭圆对象。

数学部分复用 [NumPy 最小二乘](https://numpy.org/doc/stable/reference/generated/numpy.linalg.lstsq.html)、已有仿射求解与 [SciPy 1.16.2 Kabsch 旋转拟合](https://docs.scipy.org/doc/scipy-1.16.2/reference/generated/scipy.spatial.transform.Rotation.align_vectors.html)。米制相似模型返回 [PROJ Helmert 表达式](https://proj.org/en/stable/operations/transformations/helmert.html)；三维使用 `+exact`，并用当前 PROJ 运行时的基向量检验欧拉次序。`rotation_vector_radians` 是轴角向量，不能当作 PROJ 的 `rx/ry/rz`。

原报告54区、1条框线和63条注记已完成42次实际 MCP：四参数和两步完整仿射、原生保存重开、MPJ/EPS/PDF/TIFF，独立矩阵对照的最大几何差约 `1.63e-11` 源坐标单位。三维三/七参数用独立 PROJ 已知算例核对；这部分是数值求解，未宣称已编辑原生三维文件、写出原厂转换系数文件或获得真实测量精度。

### 对齐影像拼接与范围掩膜

`mapgis_prepare_raster_mosaic` 接收有摘要的影像清单、共同坐标框架、输出 bounds/resolution 和 `first`、`last` 或 `mean`。输入必须先纠正为分辨率及像元起点对齐的北向上格网；该接口不会暗中重投影或重采样。有效零值和未覆盖像元通过独立覆盖通道区分。均值默认 float64；其他取样仅允许显式无损扩宽。

`mapgis_prepare_raster_mask` 接收同一框架内的 Polygon/MultiPolygon，保留孔洞；`crop` 控制是否保留整个原格网，`all_touched` 控制边缘取样。输出真实掩膜，有限 NoData 必须明确指定且不能和有效样本碰撞。独立各波段有效性及分数透明度尚未接入，接口会明确拒绝。

数值保存和出图分开核对：非 byte 多波段 MSI 的默认原生显示可能是灰度。需要彩色出图时，复用 `mapgis_prepare_raster_processing` 的 `stretch`，显式选择三个波段及百分位范围，生成0..254的 byte RGB 显示副本并以255保留 NoData；原始数值 MSI 留作分析。该显示映射有明确量化，不冒充数值原件。实际旧报告的单影像输出检查及孔洞/颜色像素读回见 `work/automation-20261030/raster-composite-2/evidence/display-acceptance.json`。

### 图幅编号与原生图框

`mapgis_map_sheets` 为只读接口。`index` 将明确的经纬度及比例尺转换为图号，`decode` 返回图号的经纬度范围，`cover` 查询Polygon/MultiPolygon覆盖的图幅。当前实现GB/T13989-2012的国内非合幅行列编号，范围72≤经度<138、0≤纬度<56，比例尺1∶1000000至1∶500。边界点按西南包含、东北不包含归属；检索默认要求有面积相交，可明确选择仅边界接触也计入。经纬度坐标系由调用者提供，图号不能代替基准说明。

将返回的`geographic_bounds`交给现有`mapgis_prepare_map_frame`生成border/grid/labels，再执行原生保存、工程组装和出图。原生编码仍有已记录的投影参数限制，不能因图号正确而默认任意CRS可写。实际Fuzhou G50E012021和11个标准示例见`work/automation-20261030/map-sheets-fixed-bounds/evidence/acceptance.json`。这条链同时修复新WL的辅助范围框比例尺遗漏；读回几何正确不足以证明原生范围和显示正常。
# 原生三维地形视图

`mapgis_prepare_native_scene` 已接通原厂 `DemArea` 的 DEM 保存、逐单元重开检查，以及 `NewTin3DFly` 的地形透视输出。`operation=create` 接收数值高程栅格；必须给出垂直单位和明确的米制源坐标对象，例如 `{"definition":"EPSG:4549","axis_order":"xy"}`。原生 DEM 存储 float32，默认不允许高程舍入；需要转换时，明确提供 `max_elevation_rounding_error_m`。

原生三角网使用 `mapgis_prepare_native_tin_scene`。`create` 直接接收 WTIN，要求明确的米制 XY/Z；不转换为 DEM。原始 `model.tin`、同名元数据和完整拓扑保留，原厂 W60_Tin 在独立显示区平移 XY 后交给 Tin3DView 渲染，避免大坐标被旧版 float 显示运算量化。产物包含 `camera.bin`、原生 MSI、RGB TIFF/PNG 和 `.scene.json` Agent 清单。相机保存 double 世界原点及 float 局部向量，返回的世界 Z 除以真实垂直比例；不直接采用原厂 `GetEyePos` 中带比例的 Z。

`render` 读取整个已保存目录，`parameters={}` 恢复精确视角；可设置 `width/height`（32..2048）、`fov_degrees`（30..120）、正值三分量 `scales`，或成对世界 XYZ `eye/target`。同一视角的大坐标平移、隐藏旧源后的换目录重开均逐像素一致。25 次实际 MCP 和四种真实中断后 12 次恢复已验证，完成的原生绘制不会重放。其他模型尚在接入；该清单不是原厂专有三维场景格式。

TIN飞行使用同一工具的 `operation=flight`，输入完整场景及 `flight={points:[[世界X,世界Y,世界Z],...],speed:50.0,fps:24,frame_start:0,frame_count:12}`。路径2..100点；speed是原厂PAT空间参数，不是米每秒。省略frame_count录整条；原厂路径容量和录制步最多1000，AVI小于1GiB。`max_coordinate_error_m` 默认0，明确限制原厂坐标量化误差。使用保存的精确相机；eye/target/scales改动先render保存。复用原厂插值、计时与TIN渲染，不调用有缺陷的原厂路径保存函数。VFW无压缩AVI在新进程逐帧读回，原TIN、纹理和WL保持。保存flight.avi/flight.pat/flight.txt/initial-camera.bin；render恢复末帧，flight重录恢复初始相机。

2026-11-01：25次实际MCP验证114帧全部独立OpenCV读回、末帧静态重开、片段、重复请求及隐藏旧目录后的迁移和重录；真实exit120/122/123/121后12次恢复不重放，另3次接管旧失败回执。较长路径4次调用验证原厂容量740/有效586帧，录制3帧独立像素一致。证据在 `work/automation-20261030/recovery-20261101/`；相关86项及最后接口19项分别通过。

TIN贴图使用可选 `texture={file,expected_sha256,expected_members?}`，要求同框架、全覆盖、无缺失的三通道uint8 RGB，每轴2..1024像素。复用原生MSI导入和像素回读，生成世界坐标不变的 `texture.tif`，以及单独平移到局部显示框架的 `texture.msi`。原厂纹理提取器按自身规则生成128..1024像素的显示纹理，不调用参数对话框。`render` 默认继承，可传入新贴图替换，或 `remove_texture=true` 移除。20次实际MCP和四种真实中断后12次恢复已验证；迁移后MSI原字节保持，移除贴图后与无贴图视图逐像素一致。

TIN线叠加使用 `line_overlay={file,expected_sha256,coordinate_frame:"native_metres",source_crs?,height_offset_m?}`。原WL的坐标和字节保持；沿用已验证的TIN剖面算法，在原有顶点及每个三角面交点计算XYZ，由原厂BAS60写入独立三维线区，再交给Tin3DView绘制。不会重新三角化，不会跨越缺失面；`height_offset_m`默认0，可明确设置显示抬高量。保留`overlay.wl`和可重建的`lines.xyz`，最多20万采样顶点。render可继承、替换或`remove_line_overlay=true`移除；原厂三维线渲染使用线色，不承诺全部二维复杂线型外观。19次实际MCP及四种中断后12次恢复已验证，包括物理编号1、3的文件、贴图并用、移目录重开、替换移除和伪造XYZ拒绝。

三维绘制和 MSI 编码分进程执行：原厂 NewTin3DFly 绘制透视视图，Windows OpenGL 读回该进程自己的隐藏窗口帧，再由原厂 MsiBase 编码为 MSI；新原生进程逐像素读回，必须与渲染帧完全一致。这样避免旧版三维模块和影像编码器共处一个32位进程时出现的内存分配失败。读回的是自有渲染上下文，不读取用户桌面。

恢复测试实际终止过服务进程：原生渲染结束后退出120、MSI 编码结束后退出122、原生编码确实失败后退出123，以及产物目录封存后、数据库记录提交前退出121。`mapgis_reconcile_artifact` 均接回已完成渲染，保留原渲染 PID、文件和日志的 SHA/mtime。已完成的编码直接接管；失败的编码保留残件，在新目录重做编码，不重新绘制地形。发布时原生 DEM 头文件与整个数据子目录一起原子落盘；不要只发送 `.dem` 头文件。

`operation=render` 读取已保存的 `.scene.json`，更改 `mode`、`width/height`、`quality` 或成对的 `eye/target`，另存一个新视图。原 DEM 保持原字节；返回的 `render.eye` 是原厂接口实际采用的位置。输出的 MSI、TIFF、PNG 是透视图，使用像素坐标，不承载高程格网的地面坐标系。

可选 `texture={file,expected_sha256,expected_members?}` 附加同坐标框架、覆盖整个地形的配准 RGB 底图。当前支持每轴2..1024像素、无缺失值的 uint8 三波段影像；更大的底图需先明确裁剪或重采样。原厂 TexDbs 创建单基瓦片纹理，保留底图 TIFF、原生 MSI、TEX_INFO 和数据子目录。重开时默认继承底图，传入新 `texture` 替换，`remove_texture=true` 移除。附加、替换、移除、隐藏旧目录与源图后的迁移重开，以及四种中断恢复均已通过实际 MCP。有效黑色像素保留。旧版纹理缓存使用原厂分配器在本项目子进程内调整；原厂的初次大额分配尝试仍会发生，不能将后续缓存上限等同于进程峰值内存。

`line_overlay={file,expected_sha256,coordinate_frame:"native_metres",source_crs?:明确源框架}` 附加一个保持原字节的原生 WL；原厂逐顶点取地形高程，保留物理编号和原生样式，不自动加密线。当前至多20000条线、200000个顶点，原生存储 XY 必须为米，全部顶点须位于 DEM 中心范围内；投影坐标常用 `map_scale=1000` 满足此要求。其他比例或越界数据先在新分支转换、裁剪。默认核对文件坐标头；旧格式坐标头不能完整保存已知源框架时，显式传入匹配场景的 `source_crs` 赋值，坐标不会重投影，也不将赋值当成测量精度证明。

附加、替换、`remove_line_overlay=true` 移除和旧目录、原始 WL、底图同时隐藏后的重开共23次实际MCP已通过，搬迁后像素相同，移除后回到原底图像素。准备后源线变化会拒绝发布，恢复原源文件后接管候选。底图与线同时存在时，四种真实中断后12次恢复通过，已完成原生渲染不重放。

`operation=flight` 对已保存场景录制路线飞行。`flight` 提供源XY控制点、原厂空间步长 `speed`，以及 `fps`（默认24）；速度范围由 `render.fly_speed_range` 返回，空间步长不等于米每秒。默认录制完整插值路线，可用 `frame_start`、`frame_count` 明确选取片段，最多1000个推进步骤、AVI小于1GiB。路线使用原厂 float32 接口，XY 舍入默认不允许，确有需要时明确设置 `max_xy_rounding_error_m`。原厂相机高度取决于前面的路径，选片段时先推进前缀，保证与整片对应帧一致。

飞行沿用原厂插值与三维绘制，使用 Windows VFW 标准接口生成无压缩24位DIB AVI，新原生进程逐帧读取并核对 SHA256，MSI/TIFF/PNG 为影片末帧。原厂 AVI 接口会调用编码器选择对话框，因此这里不调用该带交互的录制入口。实际15次MCP已验证100帧整片、隐藏旧源后的搬迁重录、35..46片段及末帧，全部像素经已有OpenCV独立解码核对；另3次小数速度调用及四种中断后的12次恢复通过。输入参数保存在 `flight.input`，用于重复录制；末帧姿态在原厂停止飞行恢复初始相机之前捕获；`render` 默认恢复此末帧，显式 `eye/target` 或全景模式另选视角。20次MCP已验证静态重开、重复保存、迁移和覆盖新视点，四种退出后的12次恢复均与末帧逐像素一致；AVI 字节未变。保持返回的 `native_camera` 不变，旧实验版没有末帧姿态的清单先按 `flight.input` 重新录制。

`annotations` 载入一个普通文本 WT，与线文件一样，必须明确 `coordinate_frame: "native_metres"`，必要时显式赋予已知的 `source_crs`。原生坐标为米，所有锚点在 DEM 内；原 WT 的文字、物理 ID、属性与字节保持。`font` 指定场景统一字体，例如 `{"face":"SimSun","points":14,"bold":false,"italic":false}`，字号为 1..30；原 WT 的文字尺寸与角度不直接充当三维字体参数。`height_offset_m` 默认 5，`visible` 控制显示，`max_view_distance_m` 控制漫游中的可见距离。重新渲染时默认继承，提供新 `annotations` 替换，`remove_annotations: true` 移除。

文字由原厂 GDI 字形与原厂三维注记接口绘制。原厂字形会把黑色当作透明背景，后端在自己的显示工作区生成非黑字形掩码，再调制成真实黑色；源 WT 和原厂 DLL 保持。混色注记按物理记录顺序绘制，绘制前后保存、恢复 OpenGL 服务端及客户端状态。当前接收普通 GB2312 文字，至多 20000 条活动记录、200000 个物理槽、总计 1MiB 文字；逐条检查实际字体字形及原厂图集容量。缺失字体、缺字或过长标签返回具体错误，便于更换字体或拆分标签。实际 MCP 已验证黑字可见、关闭后的背景逐像素不变、混色叠放、粗斜体、替换/移除、隐藏旧源后的迁移重开、飞行及末帧静态重开；四种真实中断后的恢复保留原渲染和编码结果。

`region_overlay` 支持一个 WP 的实色区域，包括孔洞和多部件。输入包含文件摘要、`coordinate_frame: "native_metres"` 和必要的 `source_crs`；源 WP 保持原字节。`opacity` 为 0..1，`visible` 控制显示，`height_offset_m` 默认 0，`max_view_distance_m` 控制漫游中的显示距离。重新渲染默认继承，也可替换或用 `remove_region_overlay: true` 移除。

这条区域路径复用项目已有的未修改 CDT 2.0.1：按 DEM 步长加密边界，加入区内的 DEM 网格点，再进行约束三角化并排除孔洞。高程取自原厂地形的双线性查询，实色取自原厂 WP 调色接口；标准 OpenGL 在原厂线和注记之前绘制区域，沿用原厂区显示偏移。原厂区域载入在本机出现过许可服务器和内存提示，因此这里直接使用独立网格计算，不调用或修改该原厂三角化器。`regions.mesh` 是可重建的 Agent 网格输入，随原 WP 一起保存；当前至多 10 万网格顶点。二维填充图案和每区影像纹理尚未接入。

实际 MCP 已核对孔洞的原背景、半透明颜色、关闭与移除后的整图一致性、旧工程和原 WP 隐藏后的迁移重开、距离隐藏、源文件变化拒绝与恢复、飞行及末帧静态重开。新原生进程核对物理 ID、填色、网格与原区域面积，MSI 像素仍须完全等于实际渲染帧。四种父进程中断后的恢复保留完成的原渲染和编码。

共享时保留整个产物目录；`.scene.json` 是绑定原生文件、坐标和视图参数的 Agent 清单。`texture_DAT/Dat0.py` 是原厂纹理二进制数据，不是 Python 脚本，不执行，也不按扩展名清理。符号点、其他模型和完整高分辨率纹理仍在接入，三维模块整体尚未完成。
