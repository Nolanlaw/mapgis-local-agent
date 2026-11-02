# 本机6.7的原厂SDK调用

当前目标见[本机调用契约](LOCAL67-CALLING-CONTRACT.md)。此入口复用已安装MapGIS6.7的类型库和原厂COM对象，在已有自有x86私有桌面进程中调用；不使用用户的鼠标、键盘或前台，不安装/注册COM，不改变原厂DLL、许可或系统DEP。独立进程退出后释放对象，调用回执、候选及成果保存在任务目录。

`mapgis_describe_sdk`只读发现四个已绑定摘要的原厂类型库。先不传参数查看库，再按`library`和`class_name`或`interface`读取方法、类型、输入/输出方向、默认值及枚举；可按`member`过滤、按`offset/limit`分页。完整路径`LoadTypeLib`只读类型信息，源摘要在前后核对。64位元数据不被用于32位虚表偏移。发现结果的`execution_verified=false`始终保留；实际可调用范围另见`current_call_policy`。

`mapgis_prepare_sdk_flow`把明确的一组原厂对象和调用步骤接成同一后台会话。原厂`GetIDsOfNames`重新核对DISPID，`IDispatch::Invoke`按原生签名接收标量、前序对象和输出参数。`expect`为精确标量断言；断言失败后不执行后续步骤。`Load/Save`自动要求原厂返回true，文件参数只能绑定明确的任务输入和新输出，数据库/登录参数保持空值。没有任意代码、地址或函数偏移调用接口。

## 调用示例

以下在已知WT副本上读一个物理编号的坐标，将x改为明确值并保存。`WT_SHA256`和路径由Agent实际读取填写；坐标是原厂工作区/图上坐标，不自动转换成文件Reader的源坐标或地理坐标。输出名是新文件。`GetPos`的状态和坐标由原厂返回，可再增加`expect`；不凭空假定成功状态数值。此示例已通过实际MCP验证。

```json
{
  "parameters": {
    "inputs": [{"id":"source","file":"E:/task/work/input.wt","expected_sha256":"WT_SHA256"}],
    "outputs": [{"id":"edited","name":"edited.wt","source":"source"}],
    "objects": [{"id":"area","class":"PntArea"}],
    "steps": [
      {"id":"loaded","target":"area","member":"Load","kind":"method","arguments":[{"input":"source"}]},
      {"id":"read_status","target":"area","member":"GetPos","kind":"method","arguments":[{"value":1},{"out":"position"}]},
      {"id":"old_x","target":"position","member":"x","kind":"get"},
      {"id":"set_x","target":"position","member":"x","kind":"put","arguments":[{"value":500112.5}]},
      {"id":"updated","target":"area","member":"UpdatePos","kind":"method","arguments":[{"value":1},{"ref":"position"}],"expect":true},
      {"id":"saved","target":"area","member":"Save","kind":"method","arguments":[{"output":"edited"}]}
    ]
  },
  "request_id":"move-one-point"
}
```

随后用`mapgis_apply_patch`发布，主成果`sdk-flow.json`记录实际方法返回值和原厂重开证据，几何文件与相对多媒体资源放同一目录。`mapgis_get_request`/`mapgis_reconcile_artifact`用于对账。重用相同请求不会重放原厂变换；失败或结果未知时先读取已有回执。

当前策略开放坐标/矩形/图形参数等值对象及点、线、区工作区的只读和保留物理编号的几何操作。原厂Save可能重算属性或遗漏地图参数，输出复用原始主/弧/节点完整属性段和169字节地图参数，并检查物理编号、WP拓扑和原厂独立进程重开。此策略明确不修改属性表或地图参数；对应修改使用已有专用工具。原厂`count`为物理槽位数，本次三个有效图元的样本返回4，包含零号槽位；有效数量以`native_file_info`和物理编号列表为准。

## 已实际验证与边界

`work/full-implementation-20261101/mcp-sdk-flow-02/result.json`记录21次真实MCP调用，其中一次为预期失败断言。三种WT/WL/WP均由原厂`Tran_Par`及工作区`Transform`完成(12.5,-7.25)平移；点对象`GetPos`输出接口及其x/y已实际读取。独立几何核对、原厂保存/不同进程重开、异目录搬移重开、原字节多媒体资源、重复属性键、布尔/微秒时间/NULL日期/17位双精度属性均保留。失败断言之后没有生成保存文件。自有stdio进程结束已核实，私有内存从178,647,040到178,974,720字节。

最初验收将`count`误当有效图元数而断言失败，原厂回执保留在`mcp-sdk-flow-01`；读原厂文档并观察零号槽位后修正验收输入，没有改写原厂返回值。针对性验证17项通过，见`local67-calling/sdk-tests-02.log`。所列原生执行只证明实际调用的这些方法；其他允许的方法仍需按具体任务核对结果，不据类型库数量宣称全部6.7功能已完成。

`mcp-sdk-flow-02/edge-result.json`另记录23次累计实际MCP，包含一次缺少生成要素ID的验收输入错误及随后对账复用。文档中的`GetPos → 修改返回坐标对象 → UpdatePos → Save`已实际执行；删除后物理编号为[1,3]的WT/WL均保存并原厂重开，空位及属性保留。1:50000样本中Reader源x=500000，而原厂GetPos返回10000；原厂x增加0.25后源坐标增加12.5。应先用outputs=[]只读取得原厂坐标，或使用已有高层源单位变换工具，不能把地理坐标直接塞进原厂图上坐标参数。

共享宿主、原生复制变换、字段、多媒体和操作发现的78项针对性回归通过，12条既有PROJ序列化提示，见`local67-calling/sdk-related-tests-03.log`；不称为最新整仓回归。

这个通用Automation适配器不直接开放复杂数组/原始二进制、in-out参数、原生数据库和物理打印。已有专用工具继续复用。用户随后已批准本机非服务器模块的私有Win32方式，原厂对话框、菜单和画布通过[私有进程入口](PRIVATE-CALLS-PROPOSAL.md)调用，不占用用户工作桌面。完整本机对应见[调用范围表](LOCAL67-ROUTES.md)。

Automation参数逆序、PROPERTYPUT命名参数和错误索引按[Microsoft IDispatch::Invoke](https://learn.microsoft.com/en-us/windows/win32/api/oaidl/nf-oaidl-idispatch-invoke)实现；名称与DISPID核对按[GetIDsOfNames](https://learn.microsoft.com/en-us/windows/win32/api/oaidl/nf-oaidl-idispatch-getidsofnames)。这些是调用协议资料，MapGIS方法合同来自本机原厂DLL，执行证据来自上述本机回执。
