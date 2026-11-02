# 第三方组件

分享包保留依赖原始许可、源代码和构建来源。组件版本清单在 `THIRD-PARTY.json`，原生依赖源码的 URL、大小和 SHA256 在 `packaging/native-sources.json`。生成的包清单逐文件记录实际负载。

| 组件 | 用途与来源 | 随包材料 |
|---|---|---|
| mapgis2shp 2.6.0 | WT/WL/WP Reader/Writer，Apache-2.0 | `vendor/mapgis2shp` 原始源码与许可 |
| GeoScan | MIT 中心线提取与路径追踪，提交 `6a5aa66192f17fd1d1aeb602f410aebff4d8bc92` | `vendor/geoscan` 与运行副本 |
| Stratigraphic Amenity | MIT 仿射/GCP 数学，提交 `a389c0a299c4ee1007b2438d88d96093a3fbb994` | `vendor/stratigraphic-amenity` 与运行副本 |
| MCP Python SDK | MIT stdio 协议 | 依赖包许可，版本锁定文件 |
| CPython | Astral python-build-standalone 20261026，CPython 3.12.16 | 原运行时许可证与构建发布摘要 |
| libvips / pyvips | 分块图像处理；原生库 LGPL-3.0-or-later，Python 包各自许可 | 原生库源码、LGPL/GPL 文本、MXE 与上游打包脚本、依赖源码及 notices |
| GEOS / PROJ / GDAL | 拓扑、投影与栅格，分别保留各自许可 | GEOS 对应源码、Python 发行包内的许可与第三方声明 |
| OpenCV、NumPy、Pandas 等 | 确定性图像与几何数据处理 | 各 `dist-info/licenses` 或包内原始许可 |
| SciPy 1.16.2 | BSD-3-Clause，Delaunay、线性插值与曲面积分；wheel 内含 OpenBLAS/LAPACK 和各自声明 | 对应 sdist、wheel 原始 LICENSE.txt、包内第三方许可与 `packaging/backend-sources.json` |
| ContourPy 1.3.3 | BSD-3-Clause，格网等值线 | 对应 sdist、wheel 许可和 SHA256 |
| NetworkX 3.5 | BSD-3-Clause，路径与连通性 | 对应 sdist、wheel 许可和 SHA256 |
| ezdxf 1.4.5 | MIT，DXF 图层、实体、中文文本和曲线交换 | PyPI 固定版源码与摘要、wheel 原始许可证；字体文件仅引用，不随包分发 |
| fontTools 4.68.0 | MIT，ezdxf 的字体处理依赖 | 对应 PyPI 源码、原始许可证和摘要 |
| CDT 2.0.1 | MPL-2.0，提交 `53eff288a4e77ad1c0afe1a10f60bd2b57f877b8`；约束三角剖分，之后由 MapGIS 接口写入原生 TIN | `vendor/cdt` 未修改头文件、MPL、谓词的 BSD-3-Clause、Apache-2.0 WITH LLVM-exception 原始声明及逐文件摘要 |
| Pyogrio 0.13.0 | MIT，GDAL 矢量交换；本轮实测 GDAL 3.12.4、GEOS 3.14.1 | 对应 sdist、wheel 原始许可及 GDAL/依赖声明；GEOS 3.14.1 源码沿用已固定版本 |
| Ghostscript 10.08.0 | 独立 CLI 将原生 EPS 转为 PDF，AGPLv3 | 官方运行时、对应源码和原始许可 |
| winhlp | 单独的本机帮助索引工具，GPL-2.0；提交 `cb2661ef85693799641cf7ab258e7a4074732568` | `third_party/winhlp` 未修改解析器源码、原许可与来源；MCP 服务不导入该库 |

libvips 原生发布及 28 个组件的版本、许可说明见 `packaging/libvips-versions.json` 和 `packaging/LIBVIPS-NOTICES.md`。`pyvips-binary` 的 Python 包装许可不能替代原生库及其静态依赖的许可。

原生源码与构建脚本放在 `third_party/sources`。MXE 构建使用上游当时的分支及基础镜像；这里保存源码摘要，不承诺从今天的网络环境逐字节重建出相同 DLL。

0.6.0 新增开放后端的 sdist 直接取自对应版本的 PyPI 官方发布，逐个核对官方 SHA256 和字节长度，清单在 `packaging/backend-sources.json`。SciPy 的 wheel 使用 scipy-openblas 0.3.29.dev；完整原始二进制许可保留在 dist-info/LICENSE.txt，包括 OpenBLAS、LAPACK 与运行时条款。sdist 是对应 Python 项目的源码，不据此声称逐字节重建出同一编译器/BLAS DLL。分析用 pefile、REA 和 IDA 不进入运行包。

LGPL 库可以按其许可修改和替换。包内保留 Python 应用源码及构建入口，没有加密或限制为调试修改后的库而进行的逆向。替换原生库时，使用兼容 ABI，并同步修正 `pyvips_binary` 内的 DLL 名称及加载映射；可重新安装自行构建的同版本 `pyvips-binary` wheel。变更后重新运行自检和实际图像窗口检查。

winhlp 上游完整 CLI 要求 Python 3.13+；本包只用已在 Python 3.12 验证的解析 API 子集，不调用其 TUI、WMF 或 HTML 导出模块。索引脚本通过独立进程运行。MapGIS 自带的帮助及解析结果始终保留在接收者本机。

本包不包含 MapGIS、许可工具、专有符号/颜色库、课程报告、私人图件、聊天记录或本机账号配置。第三方许可的适用范围以各组件原文为准。

### GDAL 两点通视算法适配

`src/mapgis_agent/_los.py` 复用 GDAL v3.13.0 `alg/los.cpp` 的 Bresenham 取样、直线高度插值和地面相等可见的上游修复。提交 `130cb4675a0b42bd32acad56e57022cca44fc008`；作者 Ryan Friedman；该文件 SPDX 为 MIT。原始 C++、完整上游 LICENSE.TXT 和 SHA256 存于 `third_party/gdal_los/`，Python 文件内也保留 MIT 许可文字。适配返回完整采样表和按观察点顺序的第一个遮挡像元，新增显式单位、NoData 和输入摘要检查；不修改本机 GDAL 3.10.3 DLL。

### D8水文依赖

新增PyFlwDir0.5.12（MIT，Copyright Deltares），实际复用`fill_depressions`、D8拓扑、`upstream_area`、`basins`和`vectorize`；固定Numba0.68.1与llvmlite0.50.0（两条款BSD及随附第三方条款）。五份原始许可完整保留在`third_party/hydrology/`，官方wheel/sdist URL和SHA256见该目录`SOURCE.json`。源码归档保留在项目下载目录，可据摘要重取。原有NumPy2.5.4、SciPy1.16.2、Rasterio1.4.4、Affine3.0.1保持。

项目仅添加单位/标定、舍入许可、缺测边界、汇流守恒检查，以及GeoTIFF/原生WT/WL/WP和请求账本的衔接；未修改这些包的源码。上游返回的零长度出口线段保存为出口点。Windows运行时在stdio输入线程启动前导入科学库，避免实测到的SciPy首次加载挂起。
