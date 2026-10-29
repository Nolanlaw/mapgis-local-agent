# 公开生态与实际复用

## 0.6.0 后台模块

- [SciPy Delaunay](https://docs.scipy.org/doc/scipy-1.16.2/reference/generated/scipy.spatial.Delaunay.html) 和 [LinearNDInterpolator](https://docs.scipy.org/doc/scipy-1.16.2/reference/generated/scipy.interpolate.LinearNDInterpolator.html)：直接调用 1.16.2，不另写三角剖分或插值器；解析平面、凸包和切填跨基面独立检查。
- [ContourPy](https://github.com/contourpy/contourpy/tree/v1.3.3)：直接调用 1.3.3 格网等值线，逐层检查全部请求层；源码与许可证随包保留。
- [NetworkX](https://networkx.org/documentation/stable/reference/algorithms/shortest_paths.html)：直接调用 3.5 的图与路径算法，补原生物理边来源、单向/平行边、可达部分边和结果 WL。没有以 Star 或案例作为正确性证据。
- [Rasterio/GDAL warp](https://rasterio.readthedocs.io/en/stable/api/rasterio.warp.html) 与 [Pyogrio](https://github.com/geopandas/pyogrio)：复用实际栅格重采样和矢量驱动，增加来源指纹、多文件提交与输出读回。GeoJSON 自定义 CRS 按 [GDAL GeoJSON 文档](https://gdal.org/en/stable/drivers/vector/geojson.html) 使用 2008 扩展，明确不是 RFC 7946。
- Windows 自带私有桌面与目录联接：按 Microsoft CreateDesktop/SetThreadDesktop 文档绑定自有进程；短路径沿用本项目 Prepare-NativeOutput.ps1 的 New-Item Junction 方法，不另造窗口驱动，不切换输入桌面。

MapGIS 原生 ABI 来自获授权的本机 REA/IDA 静态核对和最小运行试验。只把自有宿主与接口代码纳入分发，不携带原 DLL、反编译资料、课程输入或逆向工具。原生与开放后端的算法来源在各工具回执中分别记录。

以下保留早期版本的生态调查与取舍，不作为 0.6.0 未测模块的后台验收。

检索与本机核验日期：2026-10-27。搜索摘要只作为候选；采用项已读取官网正文、网页 DOM 或固定提交源码。

| 来源 | 核验内容 | 本次取舍 |
|---|---|---|
| [官方 Agent](https://gisagent.mapgis.com/gis/) | 可见应用案例；开始对话要求登录 | 没有账号任务实测，没有上传课程文件 |
| [官方自定义 MCP 文档](https://gisagent.mapgis.com/docs/custom-mcp-tutorial) | 远程 HTTP 和 Agent 容器内 local/stdio；离线部署运行实例 | 本机先使用 stdio，不把云端 local 误当成 Windows 本机 |
| [Desktop SDK](https://www.mapgis.com/index.php?a=shows&catid=257&id=253) | .NET、Java 插件式开发平台 | 对当前 6.7 不推断兼容；尚未取得匹配开发包 |
| [mapgis2shp](https://github.com/leecugb/mapgis2shp/tree/2acf993e81a1d8e1984f110aff6725672ac09149) | Reader/Writer、属性元数据、附加段与图形参数布局 | 直接复用 Reader 和已固定布局；用很小的定长字节修改层保留注记和未知附加段 |
| [GeoScan](https://github.com/keros68/geoscan/tree/6a5aa66192f17fd1d1aeb602f410aebff4d8bc92) | 静态读桥接、中心线细化、路径跟踪与简化源码 | 0.4.0 复用中心线算法；保留 MIT 来源。不采用其 UI 驱动和 WM_COMMAND 桥接 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 官方客户端、服务端和 stdio | 安装稳定 1.x 版本 1.30.0，直接使用协议实现 |

原生桌面控制复用当前宿主已提供的 Computer Use / `@oai/sky`；只加入工程身份、原生参数解析、请求保护和独立保存校验。没有复制或重新实现 UI驱动，也没有对未取得的6.7 SDK作兼容承诺。

官网指向的 smaryun 下载站在本机 IAB 实际返回 `ERR_CONNECTION_CLOSED`，网页抓取也未能读取。
没有改 TLS、代理、网络或许可配置来绕过。6.7 已能启动，此轮未下载和替换新桌面版本。

优化已经落在实现里：批量明确 record_ids 一次处理；不对整份地质数据做模型调用；不全量重写原生文件；
不推断 CRS；区分属性 ID 和物理编号；原件、实时内存、文件副本分开；结果不明先读请求与输出；恢复保留后来独立字节。

## 0.3.0 实际增加的复用

- winhlp，固定提交 cb2661ef85693799641cf7ab258e7a4074732568：未修改的解析器读取安装自带Mpedit60/Mapout60帮助，保留CP936和主题来源。仅用于本机研究，不引入第二套UI驱动。
- REA 6.7.0 + 官方Ghidra12.1.4 + Temurin JDK21：真实x86静态分析和汇编核对，为精确版本的只读状态配置提供证据。COM声明不可连接当前实例的失败也保留，未把新建COM对象冒充当前文档。
- [Ghostscript官方发布](https://www.ghostscript.com/releases/index.html) 与 [10.08.0官方二进制](https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/tag/gs10080)：实际读取发布正文/资产列表，Windows64下载SHA256为52a91b8bf09298788d7a57b9206127026c23eacd75405f0a131e26dc381dce50。便携提取后实际解析MapGIS原生EPS并输出PDF；没有自建PostScript解释器。源码在[Artifex镜像](https://github.com/ArtifexSoftware/ghostpdl)。AGPLv3二进制不进入可分发源码包。
- Windows目录联接和.NET SHA256：复用原生文件能力解决出图路径兼容，所有源/复制指纹核对，PowerShell5.1/7分别测试，不新增依赖、服务或MPJ重写器。

## 0.4.0 扫描图与模块扩展

- [Stratigraphic Amenity](https://github.com/EigenformAI/stratigraphic-amenity/tree/a389c0a299c4ee1007b2438d88d96093a3fbb994)：固定提交的 MIT 仿射变换实现，复用于控制点拟合；保留源码与出处。
- [GeoScan](https://github.com/keros68/geoscan/tree/6a5aa66192f17fd1d1aeb602f410aebff4d8bc92)：复用中心线细化、路径跟踪与简化，配合分块候选提取和接边；没有用其屏幕控制实现替换宿主 Sky。
- [libvips](https://www.libvips.org/)、[Rasterio](https://rasterio.readthedocs.io/)、[Shapely/GEOS](https://shapely.readthedocs.io/) 与 [pyproj/PROJ](https://pyproj4.github.io/pyproj/stable/)：分别处理大图分块、栅格坐标与像素多边形、拓扑和坐标转换，沿用锁定依赖，没有另写这些算法。
- [winhlp](https://github.com/bitplane/winhlp/tree/cb2661ef85693799641cf7ab258e7a4074732568)：未修改的 GPL-2.0 解析器随源码提供；由独立脚本建立本机帮助索引，MCP 按源文件指纹检索。CHM 使用 Windows 自带 hh.exe 提取为静态文本，不执行帮助中的脚本。
- MapGIS 安装自带帮助提供原生命令前提；本机实际读取 15 个模块、1001 个主题。影像校正的“先进入校正预览”要求直接用于原生实测。索引和菜单覆盖不作为全部命令验收证据。

便携包所含本机库的原始源码、许可证和构建记录见 [THIRD-PARTY.md](THIRD-PARTY.md)；工作流边界见 [CAPABILITIES.md](CAPABILITIES.md)。
