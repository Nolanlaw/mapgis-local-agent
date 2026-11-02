# 原生布尔、多媒体字段与资源

当前源码正确读写原厂字段代码8布尔，以及9文本、10图像、11地图、12声音、13视频。类型和存储分别来自本机AttCom/MapGisBasCom1类型库、安装帮助及BASROOT字段函数：布尔1字节，多媒体为256字节GBK零结尾引用，并保留原厂0x10字段标记。它们不是改名的普通字符串字段。类型14及更复杂的嵌入数据仍明确拒绝。

`mapgis_prepare_attribute_schema`可创建这些原生字段；`mapgis_prepare_patch`可按物理图元编号修改布尔和引用，其他字段字节保持。布尔接受JSON true/false或精确整数0/1，不把缺失值默认为false。引用只允许明确的相对文件路径，拒绝越界、绝对盘符和非法Windows组件。NULL日期、微秒TIME和17位双精度属性均单独核验；无效的非零日期/时间不能冒充NULL。

`mapgis_prepare_multimedia`同时准备字段、链接和真实资源字节，生成可搬移的完整成果包：

```json
{
  "file":"E:/task/work/points.wt",
  "expected_sha256":"SOURCE_SHA256",
  "request_id":"field-media",
  "parameters":{
    "fields":[{"name":"现场照片","type":"multimedia_image"}],
    "assets":[{"id":"outcrop","file":"E:/task/work/outcrop.bmp","expected_sha256":"ASSET_SHA256"}],
    "links":[{"record_id":1,"field":"现场照片","asset":"outcrop"}],
    "native_resave":true
  }
}
```

`fields/links`声明本次分支完整的主表多媒体结构与链接；普通字段保留，所有旧多媒体字段须显式列出，未列出的单元格链接成为本分支空值。`record_id`是物理编号，属性里的ID可以重复。资源摘要在准备/复制/发布时核对，只有被引用的资源进入成果。沿用`mapgis_apply_patch`发布、请求对账和原子目录改名，原始输入不写入。

成果将资源放在`media/`并清空原厂主属性表的两个128字节旧目录槽，使引用按成果文件所在目录解析。原生MPJ新建/编辑和工程出图均携带依赖；同一相对资源名对应不同字节时拒绝合并。固定字段编辑或单独原生另存只返回图层文件；需要独立搬移时组合本工具建立完整资源包。

## 运行证据

`work/full-implementation-20261101/mcp-multimedia-01/result.json`记录71次累计真实MCP。WT/WL/WP各三条物理记录，重复属性键、布尔、微秒时间、NULL日期、双精度及五类资源均保留；90项原厂字段值读取、保存/新进程重开、异目录搬移、三层MPJ新建/编辑及EPS/PDF/TIFF已验，预览已直接检查。`fixed-patch-result.json`另记录6次实际MCP：改变普通文本、布尔和一个媒体引用，原厂另存/读回并按原始字节恢复，未改字段保留。

原厂SDK加载自定义属性时沿用旧的数据起点问题已由既有`NativeAttributeDataOrigin`在自有进程内临时修正并还原；没有补丁原厂二进制。读取字段和保存引用不能证明原生界面的双击关联、查看器/播放器、音视频解码或外部表已执行，这些保留未验证。原厂字段类型、文件资源与展示输出分别记录，不把截图当作媒体执行证据。

相关测试见`multimedia-project-related-tests-08.log`（53项）及`extended-fixed-patch-related-11.log`（46项），按各自修订复用，不能相加冒充整仓回归。具体源资料、原厂反编译定位和字段读回日志在`native-mm-01/`。辅助表媒体、嵌入二进制/表格和原生外部数据库激活仍有缺口；这些限制不扩成其他MapGIS版本或产品的任务。
