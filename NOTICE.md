## Scope

Apache License 2.0 covers the original project-authored MapGIS MCP connector
additions. Third-party code, data and binaries retain their respective licences
and notices as identified in NOTICE.md and THIRD-PARTY.json. This licence grants
no rights in the separately installed MapGIS product or its proprietary assets.

# 来源与许可

本机连接器增量代码与受控测试位于 `src/mapgis_agent/`、`scripts/`、`tests/`。
原生格式读取和二进制布局知识复用 leecugb/mapgis2shp 2.6.0，固定提交为
`2acf993e81a1d8e1984f110aff6725672ac09149`，Apache-2.0。
上游许可证保存在 `vendor/mapgis2shp/LICENSE`，作者声明和源文件 SPDX 保留。
本地增量修改了 `writer.py` 的同侧重复区边界登记，避免覆盖已有区编号；相邻异侧区域仍共享弧段。
新建线文件的辅助范围框使用与坐标相同的比例尺换算，修复非单位比例尺下的原生范围与裁切问题；原始文件及原始段透传保持不变。
作者声明和 Apache-2.0 许可保留，变更范围、原始摘要及验证见 `vendor/mapgis2shp/AGENT_PATCH.md`。
其余局部写入层在本项目中实现。

MCP 协议使用官方 `modelcontextprotocol/python-sdk` 1.30.0（MIT），由锁定依赖安装。
扫描仿射数学复用 Stratigraphic Amenity 固定提交
`a389c0a299c4ee1007b2438d88d96093a3fbb994` 的五个 MIT 源文件；
中心线细化、路径跟踪与简化复用 GeoScan 固定提交
`6a5aa66192f17fd1d1aeb602f410aebff4d8bc92` 的 MIT 实现。
原始源码及许可证保存在 vendor/；运行副本在 src/mapgis_agent/_vendor/。
分块解码使用 pyvips 3.0.0 / libvips 8.18.7，像素多边形使用 Rasterio/GDAL，
共享边界简化、矢量计算和坐标转换使用已有 GEOS/PROJ；均保留上游许可与来源。
本机帮助索引使用 winhlp 固定提交 cb2661ef85693799641cf7ab258e7a4074732568（GPL-2.0）；
未修改的源码及许可证随包保存在 third_party/winhlp，由独立索引脚本调用，不链接进 MCP 服务。
索引只读取接收者自己的安装，MapGIS 帮助正文及生成的索引不分发。
REA/Ghidra/JDK 沿用各自原许可证，配置及二进制不进入源码包。
本机 EPS 验收转换复用官方 Ghostscript 10.08.0（GNU AGPLv3），只调用独立命令行程序。
源码包不附带 Ghostscript 二进制，也不重新实现 PostScript 解析或渲染器。
离线分享包另附官方未修改的Ghostscript运行时及对应源码，GEOS对应源码也随包保留；
Python使用Astral python-build-standalone的CPython 3.12.16，原始许可证及依赖许可证均随包保留，详见THIRD-PARTY.json。
MapGIS 软件、许可程序、课程原件、私有测试输出及本机配置不随源码包分发。

工具保持原始数据的含义边界。测试中的样式变更为技术验证，不是新的地质解释或正式制图成果。

图幅编号复用 good9527/ovobj-converter 的 MIT 内核，固定提交
`ef5a338f7c45d22c48c841bff8ae059cf75a1cd9`，原始 `grids.py` Git blob
`57ae9495173494842e28b0d7c12b44ecfa601c9f`。提取的运行副本、MIT 许可和修改说明
见 `src/mapgis_agent/_vendor/ovobj_grids.py`、`ovobj-grids.LICENSE` 和 `ovobj-grids.PATCH.md`。
修正上游1:2000分幅表、增加1:1000/500、严格校验图号和边界；未引入其无关转换器及入口。

钻孔轨迹复用 Zabamund/wellpathpy 0.5.2，固定提交
`ccbb7cd803485a74d2de83ef4dd5912bb7f9c2bd`。三份数值模块保持原字节，
作为可替换的普通 Python 源码放在 `src/mapgis_agent/_vendor/wellpathpy/`，
同时保留 LGPL-3.0、其引用的 GPL-3.0 文本及逐文件 Git blob/SHA-256 来源记录。
原作者 Robert Leckenby、Brendon Hall、Jorgen Kvalsvik 的署名保留。
输入约定、Excel 映射、SciPy 球面方向插值和原生二维制图由本项目适配，
未复制其绘图或文件应用层，也未新增 pip 依赖。轨迹是测斜点之间的模型，
不作为中间位置实测或孔间地层解释。Striplog 公开柱状图仅用于版式参考，未复制代码或数据。

GDAL 通视算法的 Python 适配来自 MIT 许可的 GDAL v3.13.0 `alg/los.cpp`，保留作者 Ryan Friedman 及上游许可。原件、许可证、提交和摘要见 `third_party/gdal_los/`；适配文件为 `src/mapgis_agent/_los.py`，不是本机 GDAL 二进制升级。

D8水文分析使用MIT许可的Deltares PyFlwDir0.5.12，固定Numba0.68.1和llvmlite0.50.0。原始sdist/wheel摘要与五份完整上游许可见`third_party/hydrology/SOURCE.json`及相邻目录；没有修改上游源码或替换原有NumPy/SciPy运行时。

The MCP distribution icon is Google's Material Design `map` icon under Apache-2.0.
Its upstream source is `google/material-design-icons`, `src/maps/map/materialicons/24px.svg`.
