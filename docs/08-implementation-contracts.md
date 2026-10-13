# 08 — 首轮实现协作契约

> 2026-09-15：用户授权开始自主、多 agent 并行实现。此文件记录首轮接口协调，不替代产品验收。

## 工作边界

- 主 agent：工程、共享 domain、ApplicationService、入口、整合、打包和端到端验证。
- 数据 agent：`infrastructure/store.py`、数据/搜索/备份测试。
- 文件 agent：`infrastructure/files.py`、`editor.py`、`monitor.py` 及对应测试。
- GUI agent：`ui/`、界面测试；通过异步命令调用服务，不直接执行文件 IO 或数据库操作。
- 公共 DTO 以 `domain/models.py` 为准；签名变更先沟通，不覆盖其他 agent 的文件。

## ApplicationService 与 GUI 契约

`submit(command: str, payload: dict, on_success: Callable, on_error: Callable) -> Future`

回调经注入的 GUI dispatcher 投递到主线程；错误为 `NotesError`。服务另提供 `subscribe(callback)`，callback 接受 `AppState`；UI 仍需校验本地查询/预览代际与关闭状态。

| command | payload | 成功结果 |
|---|---|---|
| state | {} | AppState |
| query | SearchQuery 的字段组成的 dict | SearchResult |
| folders | library_id | 包含根目录空字符串的相对子目录 tuple，含空目录 |
| add_library | root_path, name（可选）, extensions/excluded_dirs（可选） | Library；自动安排扫描 |
| scan | library_id（可选，缺省所有启用库） | 扫描统计 dict |
| cancel_scan / cancel_query | {} | 即时设置当前任务取消令牌，不排在被取消操作后面 |
| preview | document_id | Preview |
| list_templates | {} | 不可变 TemplateInfo tuple；固定四个内置模板 |
| preview_template | template_id, filename | TemplatePreview；纯渲染，不创建文件 |
| create_document | library_id, relative_dir, filename, template_id（可选，默认blank） | Document；编辑须另发 open_document |
| open_document | document_id | None |
| set_favorite | document_id, value | None |
| set_document_tags | document_id, tag_ids | None |
| create_tag | name | Tag |
| rename_tag / delete_tag | tag_id, name（rename） | None |
| set_library_enabled | library_id, enabled | None |
| update_library_rules | library_id, extensions, excluded_dirs | None；安排扫描 |
| set_editor | path | None；只配置，不启动 |
| set_encoding | document_id, encoding（空值恢复自动） | None；触发该文档重新索引 |
| export_backup | path | None |
| inspect_backup | path | 校验后的统计 dict及signature/current_signature |
| restore_backup | path, confirmed=True, expected_backup, expected_current | None；双指纹复验后保护/替换，不恢复正文 |
| rebuild_index | {} | 扫描统计 dict |
| backup_status | {} | 自动快照状态 dict：status/message/last_success/due/dirty/path |
| automatic_backup | force（可选） | 同上；UTC自然日有变化才自动备份，force可提前但不制造重复副本 |
| inspect_relocation | document_id, target_path（绝对路径） | 原/目标相对路径、目标记录ID、requires_merge_confirmation及提示 |
| relocate_document | document_id, target_path, confirmed=True, merge_unannotated（显式bool） | 保留原ID与人工信息的Document；只更新关联，不移动文件 |
| set_quick_open_hotkey | enabled（严格bool） | None；保存本机偏好，不执行系统注册，也不从备份激活 |
| list_smart_lists | {} | SmartList tuple；保留失效引用并说明不可用 |
| create_smart_list | name, query（8字段白名单） | SmartList |
| update_smart_list | id, name, query | SmartList |
| delete_smart_list | id, confirmed=True（严格bool） | None；只删除列表定义 |

`close()` 是阻塞收尾方法，由 GUI 的关闭协调线程调用，结束后再销毁窗口。UI 不在事件中阻塞等待 Future。

`AppState.data_revision` 在数据变更/扫描完成后递增，查询和读取 state 不递增；GUI 据此刷新当前筛选，避免订阅状态与查询互相触发死循环。

0.2.0 增加 `AppState.backup_status`（可展示中文状态）、`last_backup_at`（Unix秒或None）；`ReadResult.error_code` 区分共享冲突、访问拒绝、读取不稳定、元信息落后、取消及永久错误。GUI不能靠本地化错误文字判断是否重试。

### 0.2.0 内部维护契约

- `RetryQueue` 不读取文件、不 sleep；按文档ID/代际/token租出任务，最多5次0.5/1/2/4/8秒退避，队列及耗尽标记有界。
- `retry_index` 为内部actor命令，不是任意文件读取接口；执行前后核验租约，取消/关闭后的结果不能提交。
- `maintenance` 为内部写任务，自动备份与到期内容核验排队去重；失败备份有5分钟退避。
- `__maintenance.hash_verified.<library_id>` 持久化完整内容核验成功时间。启动时检查到期/损坏/未来时间，只有完整、未取消且无未处理读取错误的核验才推进时间。
- 停止扫描同时清除待执行的读取重试，延迟自动完整核验；显式新扫描和新文件事件仍可启动新任务。

### 重新定位的窄范围确认合并

原设计拒绝全部目标记录冲突。实际使用中外部移动后的新路径常已被扫描器自动登记，因此0.2.0允许**用户明确二次确认**后吸收“无标签、收藏、打开记录、编码覆盖”的自动登记占位。目标已有任一人工信息、旧路径仍存在、库外/重解析点目标一律拒绝。检查在提交事务中重复，不依赖之前对话框检查的旧结果，不复制/删除/改写正文。

### 0.4.0 可验证直接文件移动契约（2026-09-16）

- `FileEntry.file_identity` 默认空串，只有当前本地固定NTFS普通单链接文件具备可用字段时填入设备、文件编号和出生时间组合。缺字段/硬链接不给身份，不使用mtime、文件名或内容hash猜测同一对象。
- `MoveHint` 保存源/目标相对路径、单调时钟接收时间、目录/synthetic标志。`ChangeBatch` 原子携带路径集合、移动提示和可靠性；未知、溢出、订阅重建或异常清除自动关联机会。
- 监控API `take_change_batch(library_id)` 替代分开消费的路径/移动队列；兼容 `take_changed_paths` 时也清理移动机会，防止过期提示泄漏到下一次扫描。
- 内部 `scan` 可以携带 `moves`、`moves_reliable`。服务仅处理300秒内的完整直接文件事件；相同pair去重，链、循环、多源/多目标冲突、目录与synthetic事件不推断。
- `Store.apply_observed_move(library_id, source_relative, target_relative, target_entry)` 在事务中重复检查现存目标身份、旧路径真实缺失及目标人工信息。只有联合证据充分才更新源记录并保留ID/人工信息；安全拒绝返回None，随后正常扫描及手动确认流程兜底。
- 目标无人工信息且派生身份相同的自动占位可被可信事件吸收。这不是把0.2.0手动确认省略给任意目标：无可信事件、未知身份或有人为信息的目标仍不能自动合并。
- 独立证明的移动可先提交；后续无关子目录枚举不完整或用户取消不回滚已完成工作，也不会清算未成功扫描范围的旧记录。

### 0.6.0 快速打开契约

- `query`及`cancel_query`可带`channel="main"|"quick"`，默认main；服务先消费该字段，不进入SearchQuery。每通道独立取消令牌，关闭服务取消所有通道。仍使用一个查询执行器，不宣称SQL并行。
- quick查询固定文件名模式、最多20条；纯空白使用最近打开；范围为所有启用在线文档库，不继承主列表筛选。
- 后台命令容量128、quick查询32、用户短动作8分别限额，防后台排队占满所有快捷操作。
- `open_document`、`set_editor`、`set_quick_open_hotkey`走独立短动作线程；数据库操作仍由Store门锁串行，不共享跨线程连接。
- 打开文档最终在`document_action_gate`下重新读取当前路径、库规则和编辑器配置，验证、发出请求和记录最近打开；不等待外部编辑器退出，不在该门锁内扫描或分发GUI回调。
- Quick关闭取消未执行的打开Future；已执行的请求不终止用户编辑器。最近记录写入失败不把已发出的打开请求误报为启动失败。
- 全局Ctrl+Alt+N仅用户选择后注册，默认不注册。注册成功后才持久化；失败回滚，冲突不注销他人注册，退出释放，不自启动或托盘常驻。

### 0.7.0 文档模板契约

- 模板ID只允许`blank`、`meeting`、`daily`、`troubleshooting`；新增冻结DTO `TemplateInfo`与`TemplatePreview`，不新增数据库表或第三方依赖。
- `list_templates`、`preview_template`走预览执行器，不排在整库写扫描后；它们不取消正文预览，不执行磁盘IO或登记。
- 文件名由文件层纯函数校验；标题取最终文件名去扩展名，正文只对白名单`{{title}}`、`{{date}}`、`{{datetime}}`做单遍替换，标题内的占位符不递归解释。
- GUI只提交模板ID，不回传可编辑模板或预览正文。服务在实际创建时用单个本地带时区时间快照重新渲染；空白模板正文严格为空，省略模板ID保持旧行为。
- 文件层`create_document(..., *, initial_text="", cancel=None, excluded_dirs=())`在排他创建前验证并编码全部内容；严格UTF-8、无开头BOM、LF、最多65536字节。文件名组件最多255个UTF-16单元；服务显式传入当前库排除目录，命中时在文件访问前拒绝。
- 一旦排他创建成功，后续写入、短写、flush/fsync、关闭、元信息读取或取消失败返回`created_partial`；保留文件，不删除、不重试创建。服务只安排补偿扫描，GUI只提示并刷新，不启动编辑器。
- 成功创建按既有流程登记、索引；编辑须另外调用`open_document`。预览晚到、模板快速切换及窗口关闭由GUI代际检查隔离。
- 四个模板均为普通文本；非空模板建议`.md`，但尊重当前库的扩展名规则，可写入`.txt`。用户已输入的文件名不被模板切换覆盖。
- 登记失败为`created_not_registered`，登记后的索引/状态异常为`created_followup_failed`；都明确告知已创建事实、保留路径、不可重复创建，并请求补扫。补扫调度失败不掩盖原先错误；界面错误分支不自动打开编辑器。
- 新建目录原值不得`strip()`，避免合法前导空格目录悄悄变为另一个目标；不合法路径仍由文件层拒绝。

### 0.8.0 智能列表契约

详见12。`query`可只携带`smart_list_id,page,page_size,channel`，保存筛选与普通查询参数不能混用。Store同一门锁内重新读取保存条件和引用并搜索；GUI旧状态不具备授权或扩大范围的效力。查询通道取消规则不变。

AppState追加`smart_lists`及`smart_lists_warning`。运行中读取损坏列表时不清空数据库，普通导航仍可用；下次启动preflight仍完整校验，不降低已有恢复安全性。GUI活动列表失效/删除/错误时清理结果和预览，必须由用户显式切换才离开该范围。

## 实际运行时取舍

本机系统 Python 为 CPython 3.14.0 x64，SQLite 3.50.4；已在内存库探测 FTS5 trigram 成功。PyPI 存在 wxPython 4.2.5 的 cp314/win_amd64 wheel。首轮使用此本机解释器创建项目独立 `.venv`，不依赖其他项目目录中的 Python。相对于 3.13 候选基线，这是原型阶段的可验证调整，不代表最新补丁或正式发布的最终运行时选择。
