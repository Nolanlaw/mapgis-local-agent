# 来源与许可

本机连接器增量代码与受控测试位于 `src/mapgis_agent/`、`scripts/`、`tests/`。
原生格式读取和二进制布局知识复用 leecugb/mapgis2shp 2.6.0，固定提交为
`2acf993e81a1d8e1984f110aff6725672ac09149`，Apache-2.0。
上游许可证保存在 `vendor/mapgis2shp/LICENSE`，作者声明和源文件 SPDX 保留。
没有修改上游生产源码；局部写入层在本项目中实现。

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
