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
| Ghostscript 10.08.0 | 独立 CLI 将原生 EPS 转为 PDF，AGPLv3 | 官方运行时、对应源码和原始许可 |
| winhlp | 单独的本机帮助索引工具，GPL-2.0；提交 `cb2661ef85693799641cf7ab258e7a4074732568` | `third_party/winhlp` 未修改解析器源码、原许可与来源；MCP 服务不导入该库 |

libvips 原生发布及 28 个组件的版本、许可说明见 `packaging/libvips-versions.json` 和 `packaging/LIBVIPS-NOTICES.md`。`pyvips-binary` 的 Python 包装许可不能替代原生库及其静态依赖的许可。

原生源码与构建脚本放在 `third_party/sources`。MXE 构建使用上游当时的分支及基础镜像；这里保存源码摘要，不承诺从今天的网络环境逐字节重建出相同 DLL。

LGPL 库可以按其许可修改和替换。包内保留 Python 应用源码及构建入口，没有加密或限制为调试修改后的库而进行的逆向。替换原生库时，使用兼容 ABI，并同步修正 `pyvips_binary` 内的 DLL 名称及加载映射；可重新安装自行构建的同版本 `pyvips-binary` wheel。变更后重新运行自检和实际图像窗口检查。

winhlp 上游完整 CLI 要求 Python 3.13+；本包只用已在 Python 3.12 验证的解析 API 子集，不调用其 TUI、WMF 或 HTML 导出模块。索引脚本通过独立进程运行。MapGIS 自带的帮助及解析结果始终保留在接收者本机。

本包不包含 MapGIS、许可工具、专有符号/颜色库、课程报告、私人图件、聊天记录或本机账号配置。第三方许可的适用范围以各组件原文为准。
