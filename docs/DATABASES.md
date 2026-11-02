# 外部数据库与原生属性

本轮接口可从离线 SQLite、Access 和经典 DBF 表读取数据，关联到新的 WT/WL/WP 主属性表，再把选定原生字段写回新的数据库分支。输入原件保留，调用不修改原数据库，也不启动 Access 应用或后台同步器。当前源码含 119 个工具、155 项操作说明；数量不表示原 196 条能力目标的完成率。

`mapgis_database_info(database, limit=20)` 返回字段类型、表行数和预览。`database` 包含 `file`、`expected_sha256`、`format`；SQLite/Access 还需 `table`，DBF 需 `table=null` 和明确 `encoding`。Access 使用本机已有 ACE 16.0，亦可明确选择 12.0；其他机器的驱动可用性按实际调用检查。每次只在任务目录打开文件快照。

预览只解码请求的行，并限制文本/二进制返回大小；它不证明未读行的全部类型都有效。超过 JavaScript 精确整数范围的值返回带十进制文字的类型对象，二进制返回明确的十六进制对象，长值预览有截断标记。准备操作会读取并验证全部选定行。

## 关联和空值

`mapgis_prepare_database_join(file, expected_sha256, database, parameters, request_id)` 接受现有原生文件。`parameters` 指定 `native_key`、`table_key`、`key_mode=text|numeric`、`unmatched=error|fill` 和新增字段清单。数据库键必须唯一且非空；原生重复属性键按各自物理图元保留。

每个字段声明 `source`、`name`、原生 `type`，字符串另声明 GBK 字节容量 `length`。存在源 NULL 时须另给 `null_flag` 字段名，并明确 `null_value`；允许未匹配行时明确 `missing_value`。状态字段编码为：

|状态|编码|说明|
|---|---:|---|
|有匹配行且值不是 NULL|0|包括实测零、空文本、空二进制|
|有匹配行且值为 NULL|1|属性中的替代值只服务于原生存储|
|没有匹配表行|-1|与源 NULL 分开|

`value_mode=identity` 保留普通标量；二进制明确用 `hex`，布尔用 `boolean01`。`datetime_iso` 保留完整 civil 日期时间，`date_component`、`time_component` 才提取分量；含时区值不会被自动剥去时区。计算直接使用数据库值，不经 Excel 数字序列化。输出的 literal XLSX 是人工核查表，JSON 保留选定原值、真实来源行/键和原生物理编号。

关联复用已有属性结构编码器，保持所有原图元和非属性区段；原厂保存重开、工程和出图继续使用已有工具。原 `table.join` 的 XLSX 路线及历史请求保持独立。

## 写回分支

`mapgis_prepare_database_writeback` 指定 `native_key`、`table_key`、键模式、`unmatched=error|ignore`、`native_duplicates=error|identical` 和 `{native,database,null_flag?,value_mode?}` 字段映射。目标键不能改；选择 `identical` 时，同键物理记录的所有值和状态必须相同。

写回时状态 1 恢复真正 NULL，状态 0 写值；匹配行中的状态 -1 必须先解决。源表、原生文件和 SQLite/Access 附属文件条件持续绑定到发布；准备之后出现 WAL/锁文件会阻止新结果发布。已发布的完整结果仍可按原摘要沿用。

- SQLite 输出完整任务副本，遵守约束并用事务更新；声明表/字段以外的写入不执行。批次失败会回滚已改的前序记录，发布前逐值核对整个目标表。
- DBF 支持经典 dBASE III 的 C/N/F/D/L 固定字段。所有新字节先检查宽度和小数精度，拒绝截断或自动舍入；删除槽、原头部和未改字段字节保持。明确的字符字节规则把空格填充空文本与全 NUL 标记分开；不宣称所有旧 DBF 都共享一种 NULL 约定。输出独立 DBF 和声明的 CPG。
- Access 必须明确 `access_output=isolated_table_copy`。输出新 ACCDB，仅含本表标量数据、字段类型和大小；查询、宏、默认值、索引、关系及其他字段属性不复制。原完整数据库仍保留。当前路线用普通表和类型化参数，不使用本机不支持的跨库 IN 或 ADOX 复杂表附加。Access DATETIME 写入须明确 `datetime_iso`；COM 参数采用已验证的传输方式保留引擎 civil 时间。

MCP 默认子进程环境不继承 `CommonProgramFiles`，会导致本机 ACE 元数据组件报类未注册。适配器只在本工具进程缺少该变量时，通过 Windows 目录 API 取得真实系统目录；保留调用方的明确设置，不复制整套宿主环境，也不改注册表或其他进程。

## 本轮证据与范围

`work/full-implementation-20261101/mcp-database-01/result.json` 记录 319 次累计实际 MCP，包括预期拒绝、恢复沿用和环境对照。3 份原生输入、9 份关联、9 份显示副本、3 份注记及额外 1 份双精度文件通过原厂保存重开。SQLite/GDAL/ACE 独立核对三类回写；17 位双精度数值在原厂重开后按二进制一致。SQLite 后生 WAL、真实事务回滚、DBF 精度失败前不改副本均有单独证据。

3 套 MPJ/EPS/PDF/TIFF 已直接查看，并在全分辨率 TIFF 核对 30 个点位置/区中心及原生图例。46 项相关检查及 1 项最小 MCP 环境检查分别留证，不相加冒充整仓最新回归。服务提交内存实测为调用前 178810880 B、后 209408000 B，自有 stdio 验收进程已结束。

外部 SQL 服务器与真实连接资源、原生弧/结点表的外部关联、复杂或带 memo 的 DBF、整个 Access 对象库的同步仍有范围；多媒体资源关联另属 C028。C027 只关闭上述已验证的子范围，总目标继续。
