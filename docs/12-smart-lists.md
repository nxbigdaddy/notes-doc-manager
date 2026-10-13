# 12 — 智能列表（0.8.0 实现契约）

> 日期：2026-09-17。目标：保存常用组合筛选；不是移动文件、静态文档集合或任意SQL。实现/验证状态以09为准。

## 1. 有限范围

最多100个命名列表。可保存搜索词、搜索模式、单一文档库及相对文件夹、最多32个标签AND、收藏、最近打开、包含缺失记录。保存后动态查询当前符合条件的文档。提供新建、编辑、删除与导航；删除列表不删除文档或标签。

失效的库/标签引用保留原ID，列表显示不可用并拒绝查询，不丢弃条件变成更大范围。停用库拒绝查询；离线库仅在include_unavailable=True时可读管理记录。folder非空必须指定库；库内文件夹后来消失时默认得到空结果，显式包含缺失记录可看到原路径管理记录，均不退回根目录，不为此访问文件系统。

## 2. DTO及纯域API（主agent负责）

`SmartList(id: str, name: str, query: SearchQuery, available: bool=True, warning: str="")`冻结DTO。AppState追加`smart_lists: tuple[SmartList,...]=()`、`smart_lists_warning: str=""`。

`domain/smart_lists.py`公开：

- `MAX_SMART_LISTS=100`、`MAX_SMART_TAGS=32`。
- `validate_name(value) -> str`：trim标签名，1..80字符，拒绝控制/代理/BOM/显式双向控制。
- `validate_query(value: dict) -> SearchQuery`：只接受以下8个字段，可省略使用SearchQuery默认；禁止未知字段/分页/SQL；严格bool、canonical UUID引用；text最多256有效字符；folder纯词法库内相对路径，不去掉前导空格；tags去重但不放宽语义。
- `query_payload(query: SearchQuery) -> dict`：只导出8字段，tag_ids为list。
- `encode_query(value: dict|SearchQuery) -> str`：规范JSON `{"version":1,"query":{...}}`，UTF-8最多8192字节。
- `decode_query(value: str) -> SearchQuery`：拒绝重复JSON key、未知envelope字段/版本/非法值。

8个字段：`text, mode, library_id, folder, tag_ids, favorites_only, recent_only, include_unavailable`。mode只接受all/name/content。text保留字面且不可含NUL/代理字符；SQL仍使用既有参数化搜索。folder最大1024字符，组件≤255个UTF-16单元，拒绝绝对/drive/空组件/dot/traversal/非法Windows名称；反斜杠转正斜杠。

## 3. 持久化与旧版保护（storage agent负责store.py）

不将可携带的智能列表混入会被忽略的机器settings。不对所有旧库无条件升级：

- 旧metadata schema0/1初始化后仍为1；未使用功能不创建smart_lists表。
- 第一次成功保存前做保护快照，在同一个事务内创建表、插入及`PRAGMA user_version=2`。失败回滚，不留下半升级。版本2以后不降级，即使最后一个列表删除。
- 常量`SMART_LIST_SCHEMA`是一条SQL：`CREATE TABLE IF NOT EXISTS smart_lists (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, query_json TEXT NOT NULL)`。
- v2必须存在合法表；未来schema>2拒绝。0.7及更老Store拒绝v2，避免旧版恢复/导出静默丢失列表。
- 名称唯一key使用既有normalize_text，ID canonical UUID；引用存在性在创建/编辑时检查。删除标签不改JSON，导致列表不可用。

Store公共API：

`list_smart_lists() -> tuple[SmartList,...]`；`create_smart_list(name, query:dict) -> SmartList`；
`update_smart_list(id,name,query:dict) -> SmartList`；`delete_smart_list(id) -> None`；
`search_smart_list(id, page=0, page_size=100, cancel=None) -> SearchResult`。

列表读取不调用文件IO。查询在Store同一门锁内重读保存条件、验证当前引用、调用既有search，不能靠GUI旧快照验证。无效列表/引用明确NotesError，不变成全部文档。不接受客户端附加条件覆盖保存范围。

## 4. 备份和恢复（store/recovery agents协调）

- schema1导出原format_version1，无smart_lists字段；schema2导出format_version2且必须有smart_lists数组，即使为空。每项四个原始表列，校验ID/名字/name_key/query_json及集合数量/唯一性。
- v1携带非空smart_lists应拒绝，不能静默忽略。v2缺字段/未知查询格式拒绝。失效引用可以导入保留，但查询必须不可用；不可因删除标签使整个备份不可恢复。
- Store._validate_backup和inspect/restore支持1/2。导入v1到现有v2时明确替换、清空列表但保留schema2；保护快照在先。v2导入v1事务升级到2。自动dirty包含列表内容；保护/自动SQLite快照自然包含表。
- RecoveryManager支持SQLite schema1/2及JSON format1/2。v2要求smart_lists完整表；v1不允许该表含记录。白名单安全检查仍拒绝未知表/触发器/生成列等。
- 清洁恢复建库只导入校验后的列表，仍不恢复机器settings/编辑器/热键/派生身份；按源format选择schema1/2。clean验证允许1/2但重复验证格式与表契约。
- 既有恢复journal阶段、指纹、原件保护及未知状态拒绝不变；补JSON/SQLite包含列表的恢复和中断回归。

## 5. 服务与GUI（root / GUI agent）

服务命令：

- `create_smart_list` / `update_smart_list`：`name, query`，更新另带`id`；成功SmartList。
- `delete_smart_list`：`id, confirmed=True`（字面bool）；返回None。
- `query`可只带`smart_list_id, page, page_size, channel`；走当前查询执行器/取消通道，服务转Store.search_smart_list。禁止与text/library/tag等混用，避免范围歧义。
- AppState含完整列表DTO。读取损坏列表时保留文档导航，smart_lists_warning明确显示错误，不把坏数据清空/覆盖。

上述警告是运行中读取错误的降级，不削弱下一次启动preflight：若启动完整校验确认智能列表元数据损坏，仍走既有安全恢复界面，不将损坏库标记healthy。候选、导出与恢复始终严格校验。

GUI树增加智能列表；可保存当前筛选/新建完整组合、编辑、明确确认删除。新对话框支持名字、搜索词/mode、库、相对folder、多选标签及三个bool。编辑失效引用要显示保留项，不默默取消选择；修复或删除由用户明确操作。

选中智能列表后查询只传ID/分页，显示保存条件；若用户修改当前搜索等控件，必须明确退出保存视图成为临时筛选，或禁用控件直到选择其他导航，不能显示已修改控件却继续查旧条件。已有主/quick取消隔离不变。

库/标签/列表变化不得通过原来的过滤清理逻辑放宽选中的智能列表；不可用/删除/错误时清空旧结果与预览并明示原因，不退回所有文档。关闭与迟到回调由代际守护。

## 6. 文件所有权

- root：models.py、domain/smart_lists.py、application/service.py、版本/文档/入口自测、tests/test_smart_list_domain.py及服务集成测试。
- storage：infrastructure/store.py、tests/test_smart_lists_store.py；必要既有store测试修正先沟通。
- filesystem：infrastructure/recovery.py、tests/test_smart_lists_recovery.py；不得改store.py。
- GUI：ui/main_frame.py、新ui/smart_list_dialog.py、ui/recovery_dialog.py（只显示新数量）、tests/test_ui_smart_lists.py；不改其他agent文件。

仅隔离生成数据，不接触用户库/Notepad3/实际系统快捷键；测试数量、冻结包与未验收外部条件如实登记。

## 7. 集成审查修正

- 在线逻辑恢复原先信任备份的派生`in_scope`，与冷恢复按库规则重算不一致。0.8在事务内规范化扩展名/排除规则并重算范围；排除目录伪造为1不生效，合法文件旧0也不被永久隐藏。普通和智能查询使用同一修复，不依赖后续扫描补正。
- 保存完成的异步状态回调使用独立导航代际；用户提交后已切换视图时不自动抢回旧列表，普通后台状态刷新不误伤保存意图。
- 在保存视图中新建文档只取当前可用列表的库/文件夹作为可见初值，不沿用旧普通导航，也不自动赋予标签/收藏等筛选条件；无效或全库条件使用空默认。
- 查询结果与库状态属于独立异步通道。旧快速打开测试修正为同时等待状态映射和结果显示，不把先到的结果误作所有显示信息已就绪；未放宽实际断言或更改生产快速打开逻辑。
