"""Native three-pane shell. Background work is exclusively service-owned."""
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from threading import Thread

import wx
import wx.stc as stc

from notes.domain.models import AppState, SearchQuery
from notes.ui.dialogs import LibraryDialog, NewDocumentDialog
from notes.ui.formatting import format_size, format_time, highlight_ranges


STATUS = {"ready": "已索引", "pending": "待索引", "missing": "文件缺失",
          "offline": "离线", "error": "读取失败", "too_large": "超出索引上限",
          "decode_error": "请选择编码", "skipped": "已跳过", "present": "可用",
          "skipped_large": "超出索引上限", "skipped_binary": "跳过二进制",
          "needs_encoding": "请选择编码", "unknown": "待检查"}


class DocumentList(wx.ListCtrl):
    """A cached page, not a database cursor. wx virtual callbacks do no IO."""
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL | wx.LC_SINGLE_SEL)
        self.hits = ()
        self.library_names = {}
        for label, width in (("文档", 200), ("文档库 / 文件夹", 190), ("修改时间", 140),
                             ("大小", 75), ("标签", 110), ("状态", 100)):
            self.InsertColumn(self.GetColumnCount(), label, width=self.FromDIP(width))
        self.SetName("文档列表")

    def OnGetItemText(self, item, column):
        if not 0 <= item < len(self.hits):
            return ""
        doc = self.hits[item].document
        folder = str(PurePosixPath(doc.relative_path.replace("\\", "/")).parent)
        values = (
            ("★ " if doc.is_favorite else "") + doc.file_name,
            self.library_names.get(doc.library_id, "") + (f" / {folder}" if folder != "." else ""),
            format_time(doc.modified_at), format_size(doc.size_bytes),
            "、".join(tag.name for tag in doc.tags),
            STATUS.get(doc.availability if doc.availability != "present" else doc.index_status,
                       doc.index_status),
        )
        return values[column] if column < len(values) else ""

    def replace(self, hits, libraries):
        self.hits = hits
        self.library_names = {lib.id: lib.name for lib in libraries}
        self.SetItemCount(len(hits))
        self.Refresh()


class MainFrame(wx.Frame):
    def __init__(self, service):
        super().__init__(None, title="Notes · 本地文档管理", size=wx.Size(1320, 820))
        self.SetMinSize(self.FromDIP((1000, 620)))
        self.service = service
        self.state = AppState()
        self._closing = False
        self._query_generation = 0
        self._preview_generation = 0
        self._folders_generation = 0
        self._relocation_generation = 0
        self._backup_generation = 0
        self._restore_generation = 0
        self._page = 0
        self._filters = {}
        self._active_smart_list_id = None
        self._navigation_generation = 0
        self._normal_search_state = None
        self._selected = None
        self._preview = None
        self._folders = {}
        self._tree_building = False
        self._refresh_after_busy = False
        self._query_future = None
        self._quick_dialog = None
        self._hotkey = None
        self._hotkey_preference_seen = None
        self._hotkey_changing = False
        self._hotkey_notice = ""
        self._ui_shortcuts_closed = False
        self._build_menu()
        self._build_ui()
        self._debounce = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._query(), self._debounce)
        self._activation_requested = None
        self._activation_timer = wx.Timer(self)
        self._frame_window_id = self.GetId()
        self.Bind(wx.EVT_TIMER, self._poll_activation, self._activation_timer)
        self.Bind(wx.EVT_CLOSE, self._close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_window_destroy)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.service.subscribe(self._on_state)
        self._submit("state", {}, self._on_state)
        self._query()
        self.Centre()

    def start_activation_poll(self, activation_requested):
        """Poll single-instance activation only while this frame is alive."""
        if self._closing or not self or self.IsBeingDeleted():
            return False
        self._activation_timer.Stop()
        self._activation_requested = activation_requested
        if not self._activation_timer.Start(500):
            self._activation_requested = None
            return False
        return True

    def _stop_activation_poll(self):
        self._activation_requested = None
        if hasattr(self, "_activation_timer"):
            self._activation_timer.Stop()

    def _on_window_destroy(self, event):
        # Native/base-class destruction can bypass our Python Destroy override.
        # Child destruction also propagates here and must not close this frame.
        if event.GetId() == self._frame_window_id:
            self._closing = True
            self._stop_activation_poll()
            self._debounce.Stop()
        event.Skip()

    def _poll_activation(self, _event):
        if self._closing or not self or self.IsBeingDeleted():
            return
        requested = self._activation_requested
        if requested is None or not requested():
            return
        # The callback can initiate shutdown; never resurrect a closing frame.
        if self._closing or not self or self.IsBeingDeleted():
            return
        if self.IsIconized():
            self.Iconize(False)
        self.Show()
        self.Raise()

    def _build_menu(self):
        bar = wx.MenuBar()
        file_menu = wx.Menu()
        tools = wx.Menu()
        actions = [
            (file_menu, "快速打开…\tCtrl+P", self._open_quick),
            (file_menu, "新建文档\tCtrl+N", self._new_document),
            (file_menu, "添加文档库…", self._add_library),
            (file_menu, "导出管理信息…", self._export),
            (file_menu, "立即备份管理信息", self._automatic_backup),
            (file_menu, "自动备份状态…", self._backup_details),
            (file_menu, "恢复管理信息…", self._restore),
            (file_menu, "退出", lambda event: self.Close()),
            (tools, "配置 Notepad3…", self._configure_editor),
            (tools, "管理标签…", self._manage_tags),
            (tools, "保存当前筛选为智能列表…", self._save_current_smart_list),
            (tools, "新建智能列表…", self._new_smart_list),
            (tools, "编辑当前智能列表…", self._edit_smart_list),
            (tools, "删除当前智能列表…", self._delete_smart_list),
            (tools, "当前文档库规则…", self._library_rules),
            (tools, "停用 / 启用当前文档库", self._toggle_library),
            (tools, "刷新\tF5", self._scan),
            (tools, "停止当前扫描", self._cancel_scan),
            (tools, "重建全文索引…", self._rebuild),
        ]
        for menu, label, handler in actions:
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, handler, item)
        tools.AppendSeparator()
        self.hotkey_menu = tools.AppendCheckItem(
            wx.ID_ANY, "启用全局 Ctrl+Alt+N（仅程序运行时）")
        self.Bind(wx.EVT_MENU, self._toggle_quick_hotkey, self.hotkey_menu)
        bar.Append(file_menu, "文件")
        bar.Append(tools, "工具")
        self.SetMenuBar(bar)

    def _build_ui(self):
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        top = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(panel, label="NOTES")
        title.SetFont(title.GetFont().Bold().Larger())
        top.Add(title, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 22)
        self.search = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search.SetDescriptiveText("搜索文档标题或已保存正文…")
        self.search.ShowCancelButton(True)
        top.Add(self.search, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.mode = wx.Choice(panel, choices=["全部", "文件名", "正文"])
        self.mode.SetSelection(0)
        top.Add(self.mode, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self._button(panel, top, "新建", self._new_document)
        self._button(panel, top, "添加文档库", self._add_library)
        self._button(panel, top, "刷新", self._scan)
        self.stop_scan = self._button(panel, top, "停止扫描", self._cancel_scan)
        self.stop_scan.Disable()
        layout.Add(top, 0, wx.EXPAND | wx.ALL, 16)
        self.search.Bind(wx.EVT_TEXT, self._search_changed)
        self.search.Bind(wx.EVT_TEXT_ENTER, lambda event: self._query(reset=True))
        self.search.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda event: self.search.SetValue(""))
        self.mode.Bind(wx.EVT_CHOICE, lambda event: self._query(reset=True))

        self.outer = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.outer.SetMinimumPaneSize(self.FromDIP(170))
        side = wx.Panel(self.outer)
        side_sizer = wx.BoxSizer(wx.VERTICAL)
        side_sizer.Add(wx.StaticText(side, label="文档导航"), 0, wx.ALL, 10)
        self.tree = wx.TreeCtrl(side, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS |
                               wx.TR_LINES_AT_ROOT | wx.TR_SINGLE)
        side_sizer.Add(self.tree, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        self.unavailable = wx.CheckBox(side, label="包含缺失 / 停用记录")
        side_sizer.Add(self.unavailable, 0, wx.ALL, 10)
        side.SetSizer(side_sizer)
        self.unavailable.Bind(wx.EVT_CHECKBOX, lambda event: self._query(reset=True))
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._navigate)
        self.tree.Bind(wx.EVT_TREE_ITEM_RIGHT_CLICK, self._tree_context)
        self.inner = wx.SplitterWindow(self.outer, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.inner.SetMinimumPaneSize(self.FromDIP(280))
        middle = wx.Panel(self.inner)
        center = wx.BoxSizer(wx.VERTICAL)
        self.summary = wx.StaticText(middle, label="所有文档", style=wx.ST_ELLIPSIZE_END)
        self.summary.SetFont(self.summary.GetFont().Bold())
        center.Add(self.summary, 0, wx.ALL, 12)
        self.scope_detail = wx.StaticText(middle, label="", style=wx.ST_ELLIPSIZE_END)
        center.Add(self.scope_detail, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.list = DocumentList(middle)
        center.Add(self.list, 1, wx.EXPAND)
        self.empty = wx.StaticText(middle, label="添加一个文件夹开始管理，文件不会被搬动。")
        center.Add(self.empty, 0, wx.EXPAND | wx.ALL, 12)
        paging = wx.BoxSizer(wx.HORIZONTAL)
        self.previous = self._button(middle, paging, "上一页", lambda event: self._change_page(-1))
        self.page_label = wx.StaticText(middle, label="第 1 页")
        paging.Add(self.page_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.next = self._button(middle, paging, "下一页", lambda event: self._change_page(1))
        center.Add(paging, 0, wx.EXPAND | wx.ALL, 8)
        middle.SetSizer(center)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._select)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._open_selected)
        self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._document_context)

        right = wx.Panel(self.inner)
        preview_layout = wx.BoxSizer(wx.VERTICAL)
        heading = wx.BoxSizer(wx.HORIZONTAL)
        self.preview_title = wx.StaticText(right, label="文档预览", style=wx.ST_ELLIPSIZE_END)
        self.preview_title.SetFont(self.preview_title.GetFont().Bold())
        heading.Add(self.preview_title, 1, wx.ALIGN_CENTER_VERTICAL)
        self.open_button = self._button(right, heading, "Notepad3 编辑", self._open_selected)
        preview_layout.Add(heading, 0, wx.EXPAND | wx.ALL, 12)
        self.path_label = wx.TextCtrl(right, style=wx.TE_READONLY | wx.BORDER_NONE)
        self.path_label.SetHint("选中文档后显示完整路径")
        preview_layout.Add(self.path_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.favorite = self._button(right, row, "☆ 收藏", self._favorite)
        self.tags_button = self._button(right, row, "设置标签", self._assign_tags)
        self.encoding_button = self._button(right, row, "选择编码", self._encoding)
        preview_layout.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        self.tags_label = wx.StaticText(right, label="仅预览已保存内容，不会修改原文件。",
                                       style=wx.ST_ELLIPSIZE_END)
        preview_layout.Add(self.tags_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.preview_control = stc.StyledTextCtrl(right)
        self.preview_control.SetReadOnly(True)
        self.preview_control.SetWrapMode(stc.STC_WRAP_WORD)
        self.preview_control.SetMarginWidth(0, 42)
        self.preview_control.SetMarginType(0, stc.STC_MARGIN_NUMBER)
        font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        self.preview_control.StyleSetFont(stc.STC_STYLE_DEFAULT, font)
        self.preview_control.StyleClearAll()
        self.preview_control.IndicatorSetStyle(0, stc.STC_INDIC_ROUNDBOX)
        self.preview_control.IndicatorSetForeground(0, wx.Colour(232, 172, 35))
        preview_layout.Add(self.preview_control, 1, wx.EXPAND)
        self.preview_status = wx.StaticText(right, label="请选择左侧列表中的文档。")
        preview_layout.Add(self.preview_status, 0, wx.EXPAND | wx.ALL, 10)
        self.wrap = wx.CheckBox(right, label="自动换行")
        self.wrap.SetValue(True)
        self.wrap.Bind(wx.EVT_CHECKBOX, lambda event: self.preview_control.SetWrapMode(
            stc.STC_WRAP_WORD if self.wrap.GetValue() else stc.STC_WRAP_NONE))
        preview_bottom = wx.BoxSizer(wx.HORIZONTAL)
        preview_bottom.Add(self.wrap, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.relocate_button = self._button(right, preview_bottom, "重新定位文件…",
                                            self._relocate_selected)
        preview_layout.Add(preview_bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        right.SetSizer(preview_layout)
        self.inner.SplitVertically(middle, right, self.FromDIP(545))
        self.inner.SetSashGravity(0.55)
        self.outer.SplitVertically(side, self.inner, self.FromDIP(210))
        layout.Add(self.outer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        protection = wx.BoxSizer(wx.HORIZONTAL)
        self.backup_label = wx.StaticText(panel, label="管理信息保护：正在读取备份状态…",
                                         style=wx.ST_ELLIPSIZE_END)
        protection.Add(self.backup_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.backup_button = self._button(panel, protection, "立即备份", self._automatic_backup)
        self._button(panel, protection, "备份状态…", self._backup_details)
        layout.Add(protection, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(layout)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(panel, 1, wx.EXPAND)
        self.SetSizer(root)
        self.CreateStatusBar(2)
        self.SetStatusWidths([-2, -3])
        self.SetStatusText("本地优先 · 正文保持普通文件", 0)
        self._build_tree()
        self._selection_controls(False)

    def _button(self, parent, sizer, label, handler):
        button = wx.Button(parent, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        sizer.Add(button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        return button

    def _submit(self, command, payload, success=None, error=None):
        if self._closing:
            return None

        def done(value):
            if not self._closing and success:
                success(value)

        def failed(exc):
            if not self._closing:
                (error or self._error)(exc)
        try:
            return self.service.submit(command, payload, done, failed)
        except Exception as exc:
            failed(exc)
            return None

    def _error(self, error):
        message = getattr(error, "message", str(error))
        self.SetStatusText(message, 0)
        wx.MessageBox(message, "操作未完成", wx.OK | wx.ICON_WARNING, self)

    def _on_state(self, state):
        if self._closing:
            return
        previous = self.state
        self.state = state
        self.SetStatusText(state.status, 0)
        self._sync_quick_hotkey(getattr(state, "quick_open_hotkey_enabled", False))
        if self._hotkey_notice:
            self.SetStatusText(self._hotkey_notice, 0)
        self.stop_scan.Enable(state.busy)
        self._render_backup(getattr(state, "backup_status", ""),
                            getattr(state, "last_backup_at", None))
        references_changed = previous.libraries != state.libraries or previous.tags != state.tags
        smart_changed = (getattr(previous, "smart_lists", ()) != getattr(state, "smart_lists", ())
                         or getattr(previous, "smart_lists_warning", "") !=
                         getattr(state, "smart_lists_warning", ""))
        changed = references_changed or smart_changed
        if references_changed and self._active_smart_list_id is None:
            library_ids = {lib.id for lib in state.libraries}
            if self._filters.get("library_id") not in library_ids:
                self._filters.pop("library_id", None)
                self._filters.pop("folder", None)
            if "tag_ids" in self._filters:
                tags = {tag.id for tag in state.tags}
                remaining = tuple(tag_id for tag_id in self._filters["tag_ids"] if tag_id in tags)
                if remaining:
                    self._filters["tag_ids"] = remaining
                else:
                    self._filters.pop("tag_ids")
        if changed:
            self._build_tree()
        if self._active_smart_list_id:
            self._sync_smart_controls()
        # Revision advances only for persisted changes, never reads. Watcher
        # completion therefore refreshes results without query/state feedback.
        revision_changed = getattr(previous, "data_revision", 0) != getattr(state, "data_revision", 0)
        if revision_changed:
            self._refresh_after_busy = True
        if changed or (self._refresh_after_busy and not state.busy):
            self._refresh_after_busy = False
            self._query()

    def _build_tree(self):
        self._tree_building = True
        self.tree.Freeze()
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("导航")
        nodes = [("所有文档", {}), ("收藏", {"favorites_only": True}),
                 ("最近打开", {"recent_only": True})]
        selection = None
        for label, data in nodes:
            item = self.tree.AppendItem(root, label, data=data)
            if self._active_smart_list_id is None and self._filters == data:
                selection = item
        smart_warning = getattr(self.state, "smart_lists_warning", "")
        smart_root = self.tree.AppendItem(root, "智能列表" + (" · 读取异常" if smart_warning else ""),
                                          data=None)
        if smart_warning:
            self.tree.AppendItem(smart_root, smart_warning, data=None)
        found_active = False
        for smart in getattr(self.state, "smart_lists", ()):
            item = self.tree.AppendItem(smart_root, smart.name + ("" if smart.available else " · 不可用"),
                                       data={"smart_list_id": smart.id})
            if smart.id == self._active_smart_list_id:
                selection = item
                found_active = True
        if self._active_smart_list_id and not found_active:
            selection = self.tree.AppendItem(
                smart_root, "当前智能列表已删除或无法读取",
                data={"smart_list_id": self._active_smart_list_id})
        self.tree.Expand(smart_root)
        libs = self.tree.AppendItem(root, "文档库", data=None)
        for lib in self.state.libraries:
            suffix = " · 已停用" if not lib.enabled else (
                " · 离线" if lib.availability not in ("online", "unknown") else "")
            data = {"library_id": lib.id}
            item = self.tree.AppendItem(libs, lib.name + suffix, data=data)
            if self._active_smart_list_id is None and self._filters == data:
                selection = item
            folder_nodes = {"": item}
            for folder in sorted(self._folders.get(lib.id, ())):
                if not folder:
                    continue
                path = ""
                for name in PurePosixPath(folder.replace("\\", "/")).parts:
                    parent_path = path
                    path = f"{path}/{name}" if path else name
                    folder_data = {"library_id": lib.id, "folder": path}
                    if path not in folder_nodes:
                        folder_nodes[path] = self.tree.AppendItem(folder_nodes[parent_path],
                                                                 name, data=folder_data)
                    if self._active_smart_list_id is None and self._filters == folder_data:
                        selection = folder_nodes[path]
            self.tree.Expand(item)
        tags = self.tree.AppendItem(root, "标签", data=None)
        for tag in self.state.tags:
            data = {"tag_ids": (tag.id,)}
            item = self.tree.AppendItem(tags, tag.name, data=data)
            if self._active_smart_list_id is None and self._filters == data:
                selection = item
        self.tree.Expand(libs)
        self.tree.Expand(tags)
        if selection:
            self.tree.SelectItem(selection)
            self.tree.EnsureVisible(selection)
        self.tree.Thaw()
        self._tree_building = False

    def _navigate(self, event):
        if self._closing or not self or self.IsBeingDeleted() or self._tree_building:
            return
        data = self.tree.GetItemData(event.GetItem())
        if data is None:
            return
        if "smart_list_id" in data:
            self._activate_smart_list(data["smart_list_id"])
            return
        self._leave_smart_list()
        self._filters = dict(data)
        self.summary.SetLabel(self.tree.GetItemText(event.GetItem()))
        self._query(reset=True)
        lib_id = data.get("library_id")
        if lib_id and not data.get("folder"):
            self._folders_generation += 1
            generation = self._folders_generation

            def folders_done(folders):
                if generation == self._folders_generation:
                    self._folders[lib_id] = folders
                    self._build_tree()
            self._submit("folders", {"library_id": lib_id}, folders_done)

    def _search_changed(self, event):
        if self._closing or self._active_smart_list_id:
            return
        self._page = 0
        self._query_generation += 1
        self._debounce.StartOnce(280)

    def _query(self, reset=False):
        if self._closing:
            return
        self._debounce.Stop()
        if reset:
            self._page = 0
        selected_before = self._selected.id if self._selected else None
        if self._active_smart_list_id:
            smart = self._active_smart_list()
            warning = getattr(self.state, "smart_lists_warning", "")
            if warning or smart is None or not smart.available:
                self._clear_smart_results(warning or (
                    smart.warning if smart else "智能列表已删除或不存在，请明确选择其他导航。")
                    or "智能列表不可用，条件未被自动移除。")
                return
            self._clear_smart_results("正在按保存的全部条件查询…", unavailable=False)
            payload = {"smart_list_id": self._active_smart_list_id,
                       "page": self._page, "page_size": 100}
        else:
            query = SearchQuery(text=self.search.GetValue(),
                                mode=("all", "name", "content")[self.mode.GetSelection()],
                                page=self._page, include_unavailable=self.unavailable.GetValue(),
                                **self._filters)
            payload = asdict(query)
            self.empty.SetLabel("正在查询…" if query.text else "正在读取文档列表…")
        self._query_generation += 1
        generation = self._query_generation
        active_id = self._active_smart_list_id

        def result_ready(result):
            if generation != self._query_generation:
                return
            selected_id = selected_before if active_id else self._selected.id if self._selected else None
            self.list.replace(result.items, self.state.libraries)
            self.previous.Enable(self._page > 0)
            self.next.Enable((self._page + 1) * 100 < result.total)
            self.page_label.SetLabel(f"第 {self._page + 1} 页 · 共 {result.total} 个")
            if result.items:
                self.empty.SetLabel(result.warning or "单击预览 · 双击编辑 · 右键管理")
            elif not self.state.libraries:
                self.empty.SetLabel("添加一个文件夹开始管理，文件不会被搬动。")
            else:
                self.empty.SetLabel(result.warning or "没有符合当前条件的文档。可清空搜索或调整筛选。")
            self.SetStatusText(
                f"正文已覆盖 {result.covered} 个 · 待处理/跳过 {result.uncovered} 个"
                + (" · 结果可能不完整" if not result.complete else ""), 1)
            index = next((i for i, hit in enumerate(result.items)
                          if hit.document.id == selected_id), None)
            if index is not None:
                self.list.Select(index)
                self._load_preview(result.items[index].document)
            else:
                self._clear_preview()
            self.Layout()

        def query_failed(error):
            if generation == self._query_generation:
                if active_id:
                    self._clear_smart_results("智能列表查询未完成：" +
                                              getattr(error, "message", str(error)))
                else:
                    self.empty.SetLabel("查询未完成，请重试；这不代表没有文档。")
                    self._error(error)
        self._query_future = self._submit("query", payload, result_ready, query_failed)

    def _active_smart_list(self):
        return next((item for item in getattr(self.state, "smart_lists", ())
                     if item.id == self._active_smart_list_id), None)

    def _sync_smart_controls(self):
        if not self._active_smart_list_id:
            return
        smart = self._active_smart_list()
        self.search.Disable()
        self.mode.Disable()
        self.unavailable.Disable()
        if smart:
            query = smart.query
            self.search.ChangeValue(query.text)
            self.mode.SetSelection(("all", "name", "content").index(query.mode))
            self.unavailable.SetValue(query.include_unavailable)
            self.summary.SetLabel("智能列表 · " + smart.name)
            libraries = {lib.id: lib.name for lib in self.state.libraries}
            tags = {tag.id: tag.name for tag in self.state.tags}
            details = (
                f"已保存条件：搜索={query.text!r}，模式={query.mode}；"
                f"文档库={libraries.get(query.library_id, query.library_id) or '所有库'}；"
                f"文件夹={query.folder or '根目录'}；"
                f"同时包含标签={', '.join(tags.get(tag, tag) for tag in query.tag_ids) or '不限'}；"
                f"收藏={query.favorites_only}，最近打开={query.recent_only}，"
                f"包含不可用={query.include_unavailable}。选择普通导航后可修改临时搜索。")
            self.summary.SetToolTip(details)
            self.search.SetToolTip(details)
            visible = ("模式：" + {"all": "全部", "name": "文件名", "content": "正文"}[query.mode]
                       + " · " + (libraries.get(query.library_id, query.library_id) or "所有库")
                       + (f" / {query.folder}" if query.folder else "")
                       + f" · {len(query.tag_ids)} 个标签 AND"
                       + (" · 仅收藏" if query.favorites_only else "")
                       + (" · 仅最近打开" if query.recent_only else "")
                       + (" · 含不可用记录" if query.include_unavailable else ""))
            self.scope_detail.SetLabel(visible)
            self.scope_detail.SetToolTip(details)
        else:
            self.summary.SetLabel("智能列表 · 已删除或不可读取")
            self.summary.SetToolTip("不会回退到全部文档；请明确选择其他导航。")
            self.scope_detail.SetLabel("不会移除条件或回退到全部文档。")

    def _activate_smart_list(self, identifier, *, explicit=True):
        if self._closing:
            return
        if explicit:
            self._navigation_generation += 1
        if self._active_smart_list_id is None:
            self._normal_search_state = (self.search.GetValue(), self.mode.GetSelection(),
                                         self.unavailable.GetValue())
        self._active_smart_list_id = identifier
        self._sync_smart_controls()
        self._build_tree()
        self._query(reset=True)

    def _leave_smart_list(self):
        # This is an explicit ordinary-navigation/filter choice, including when
        # the previous view was already ordinary. Background queries never bump it.
        self._navigation_generation += 1
        if not self._active_smart_list_id:
            return
        self._active_smart_list_id = None
        self._query_generation += 1
        if self._normal_search_state:
            text, mode, unavailable = self._normal_search_state
            self.search.ChangeValue(text)
            self.mode.SetSelection(mode)
            self.unavailable.SetValue(unavailable)
        self._normal_search_state = None
        self.search.Enable()
        self.mode.Enable()
        self.unavailable.Enable()
        self.search.UnsetToolTip()
        self.summary.UnsetToolTip()
        self.scope_detail.SetLabel("")
        self.scope_detail.UnsetToolTip()

    def _clear_smart_results(self, message, unavailable=True):
        self._query_generation += 1
        self._debounce.Stop()
        self.list.replace((), self.state.libraries)
        self._clear_preview()
        self.previous.Disable()
        self.next.Disable()
        self.page_label.SetLabel("智能列表不可用" if unavailable else "正在查询…")
        self.empty.SetLabel(message)
        self.empty.SetToolTip(message)
        self.SetStatusText("条件未被移除；不会回退到全部文档。" if unavailable else
                           "正在查询智能列表的完整保存条件。", 1)

    def _temporary_query(self):
        smart = self._active_smart_list()
        if self._active_smart_list_id:
            if smart is None:
                raise ValueError("当前智能列表不存在，不能退回普通筛选保存。")
            return smart.query
        return SearchQuery(text=self.search.GetValue(),
                           mode=("all", "name", "content")[self.mode.GetSelection()],
                           include_unavailable=self.unavailable.GetValue(), **self._filters)

    def _smart_dialog(self, smart=None, query=None):
        from notes.ui.smart_list_dialog import SmartListDialog
        with SmartListDialog(self, self.state, smart_list=smart, query=query) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            payload = dialog.payload()
        navigation_generation = self._navigation_generation

        def saved(item):
            def state_ready(state):
                self._on_state(state)
                if navigation_generation == self._navigation_generation:
                    self._activate_smart_list(item.id, explicit=False)
            self._submit("state", {}, state_ready)
        self._submit("update_smart_list" if smart else "create_smart_list", payload, saved)

    def _new_smart_list(self, event=None):
        self._smart_dialog(query=SearchQuery())

    def _save_current_smart_list(self, event=None):
        try:
            query = self._temporary_query()
        except ValueError as error:
            self._error(error)
            return
        self._smart_dialog(query=query)

    def _edit_smart_list(self, event=None):
        smart = self._active_smart_list()
        if smart is None:
            self.SetStatusText("请先选择一个仍存在的智能列表。", 0)
            return
        self._smart_dialog(smart=smart)

    def _delete_smart_list(self, event=None):
        smart = self._active_smart_list()
        if smart is None:
            self.SetStatusText("请先选择一个仍存在的智能列表。", 0)
            return
        if wx.MessageBox(f"删除智能列表“{smart.name}”？\n"
                         "仅删除保存的筛选条件，不删除文档、正文或标签。",
                         "确认删除智能列表", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                         self) == wx.YES:
            self._mutate("delete_smart_list", {"id": smart.id, "confirmed": True})

    def _change_page(self, direction):
        self._page = max(0, self._page + direction)
        self._query()

    def _select(self, event):
        if self._closing:
            return
        if 0 <= event.GetIndex() < len(self.list.hits):
            self._load_preview(self.list.hits[event.GetIndex()].document)

    def _selection_controls(self, enabled):
        for control in (self.favorite, self.tags_button, self.encoding_button):
            control.Enable(enabled)
        self.open_button.Enable(enabled and self._selected is not None and
                                self._selected.availability == "present")
        self.relocate_button.Enable(enabled and self._selected is not None and
                                    self._selected.availability == "missing")

    def _set_preview_text(self, text):
        self.preview_control.SetReadOnly(False)
        self.preview_control.SetText(text)
        self.preview_control.EmptyUndoBuffer()
        self.preview_control.SetReadOnly(True)
        self.preview_control.SetIndicatorCurrent(0)
        for start, length in highlight_ranges(text, self.search.GetValue()):
            self.preview_control.IndicatorFillRange(start, length)

    def _clear_preview(self):
        self._relocation_generation += 1
        self._preview_generation += 1
        self._selected = None
        self._preview = None
        self.preview_title.SetLabel("文档预览")
        self.path_label.SetValue("")
        self._set_preview_text("")
        self.tags_label.SetLabel("仅预览已保存内容，不会修改原文件。")
        self.preview_status.SetLabel("请选择左侧列表中的文档。")
        self._selection_controls(False)

    def _load_preview(self, document):
        if self._selected is None or self._selected.id != document.id:
            self._relocation_generation += 1
        self._preview_generation += 1
        generation = self._preview_generation
        self._selected = document
        self._preview = None
        self.preview_title.SetLabel(document.file_name)
        self.preview_title.SetToolTip(document.file_name)
        lib = next((lib for lib in self.state.libraries if lib.id == document.library_id), None)
        self.path_label.SetValue(str(Path(lib.root_path) / document.relative_path) if lib else "")
        self.favorite.SetLabel("★ 已收藏" if document.is_favorite else "☆ 收藏")
        self.tags_label.SetLabel("标签：" + ("、".join(tag.name for tag in document.tags) or "未分类"))
        self.tags_label.SetToolTip(self.tags_label.GetLabel())
        self._set_preview_text("")
        self.preview_status.SetLabel("正在读取已保存内容…")
        self._selection_controls(True)

        def preview_ready(preview):
            if generation != self._preview_generation:
                return
            self._preview = preview
            self.path_label.SetValue(preview.path)
            self._set_preview_text(preview.read.text)
            status = preview.read.message or STATUS.get(preview.read.status, preview.read.status)
            self.preview_status.SetLabel(
                f"{preview.read.encoding or '编码待确认'} · {format_size(preview.read.size_bytes)} · {status}"
                + (" · 仅预览开头部分" if preview.read.truncated else ""))
            self.preview_status.Wrap(self.inner.GetWindow2().GetClientSize().width - 24)
            self.inner.GetWindow2().Layout()

        def preview_error(error):
            if generation == self._preview_generation:
                self._set_preview_text("")
                self.preview_status.SetLabel(getattr(error, "message", str(error)))
        self._submit("preview", {"document_id": document.id}, preview_ready, preview_error)

    def _mutate(self, command, payload, success=None):
        def done(value):
            self._submit("state", {}, self._on_state)
            self._query()
            if success:
                success(value)
        return self._submit(command, payload, done)

    def _add_library(self, event=None):
        with LibraryDialog(self) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._mutate("add_library", dialog.payload())

    def _current_library(self):
        smart = self._active_smart_list()
        library_id = smart.query.library_id if smart else (
            None if self._active_smart_list_id else self._filters.get("library_id"))
        return next((lib for lib in self.state.libraries
                     if lib.id == library_id), None)

    def _library_rules(self, event=None):
        lib = self._current_library()
        if not lib:
            wx.MessageBox("请先在左侧选择一个文档库。", "文档库规则", parent=self)
            return
        with LibraryDialog(self, lib) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._mutate("update_library_rules", dialog.payload())

    def _toggle_library(self, event=None):
        lib = self._current_library()
        if not lib:
            wx.MessageBox("请先在左侧选择一个文档库。", "文档库管理", parent=self)
            return
        if lib.enabled and wx.MessageBox(
                "停用后默认搜索不再包含此库。原文件、标签和收藏均保留。\n是否继续？",
                "停用文档库", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            return
        self._mutate("set_library_enabled", {"library_id": lib.id, "enabled": not lib.enabled})

    def _new_document(self, event=None):
        libraries = tuple(lib for lib in self.state.libraries if lib.enabled)
        if not libraries:
            wx.MessageBox("请先添加或启用一个文档库。", "新建文档", parent=self)
            return
        if self._active_smart_list_id:
            smart = self._active_smart_list()
            if (smart and smart.available and not getattr(self.state, "smart_lists_warning", "")
                    and smart.query.library_id in {lib.id for lib in libraries}):
                selected_library, folder = smart.query.library_id, smart.query.folder
            else:
                selected_library, folder = None, ""
        else:
            selected_library = self._filters.get("library_id")
            folder = self._filters.get("folder", "")
        with NewDocumentDialog(self, libraries, selected_library, folder,
                               request_folders=lambda lib_id, callback: self._submit(
                                   "folders", {"library_id": lib_id}, callback),
                               request_templates=lambda success, error: self._submit(
                                   "list_templates", {}, success, error),
                               request_preview=lambda template_id, filename, success, error:
                               self._submit("preview_template",
                                            {"template_id": template_id, "filename": filename},
                                            success, error)) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._create_new_document(dialog.payload())

    def _create_new_document(self, payload):
        def created(document):
            self._submit("state", {}, self._on_state)
            self._query()
            self._load_preview(document)
            self._open(document, created=True)

        def failed(error):
            if getattr(error, "code", "") == "created_partial":
                # A newly created file exists, but its content may be incomplete.
                # Never open it automatically, retry creation, or delete it.
                # The service already schedules one repair scan. Refresh only;
                # do not enqueue a second scan or another creation request.
                self._query()
                self._error("文件已创建，但内容可能未完整写入。已保留原文件并刷新文档库；"
                            "不会自动打开、重复创建或删除它。\n" +
                            getattr(error, "message", str(error)))
            else:
                self._error(error)
        self._submit("create_document", payload, created, failed)

    def _configure_editor(self, event=None, after=None):
        with wx.FileDialog(self, "选择 Notepad3.exe", wildcard="Windows 程序 (*.exe)|*.exe",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                def configured(value):
                    def state_ready(state):
                        self._on_state(state)
                        if after:
                            after()
                    self._submit("state", {}, state_ready)
                self._submit("set_editor", {"path": dialog.GetPath()}, configured)

    def _open_selected(self, event=None):
        if self._selected:
            self._open(self._selected)

    def _open(self, document, created=False):
        if not self.state.editor_path:
            self._configure_editor(after=lambda: self._open(document, created))
            return

        def opened(value):
            self.SetStatusText("已请求 Notepad3 打开文件；不代表已加载或保存。", 0)
            self._query()

        def failed(error):
            message = getattr(error, "message", str(error))
            if created:
                message = "文件已创建，但编辑器启动失败；原文件已保留。\n" + message
            self._error(message)
        self._submit("open_document", {"document_id": document.id}, opened, failed)

    def _favorite(self, event=None):
        if self._selected:
            self._mutate("set_favorite", {"document_id": self._selected.id,
                                         "value": not self._selected.is_favorite})

    def _assign_tags(self, event=None):
        document = self._selected
        if not document:
            return
        if not self.state.tags:
            self._manage_tags()
            return
        tags = self.state.tags
        with wx.MultiChoiceDialog(self, "选择此文档的标签（可多选）", "设置标签",
                                  [tag.name for tag in tags]) as dialog:
            current = {tag.id for tag in document.tags}
            dialog.SetSelections([i for i, tag in enumerate(tags) if tag.id in current])
            if dialog.ShowModal() == wx.ID_OK:
                self._mutate("set_document_tags", {"document_id": document.id,
                             "tag_ids": tuple(tags[i].id for i in dialog.GetSelections())})

    def _manage_tags(self, event=None):
        with wx.SingleChoiceDialog(self, "请选择操作", "管理标签",
                                   ["新建标签", "重命名标签", "删除标签", "按多个标签筛选（同时包含）"]) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            action = dialog.GetSelection()
        if action == 0:
            with wx.TextEntryDialog(self, "标签名称", "新建标签") as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self._mutate("create_tag", {"name": dialog.GetValue().strip()})
            return
        tags = self.state.tags
        if not tags:
            wx.MessageBox("暂无标签，请先新建。", "管理标签", parent=self)
            return
        if action == 3:
            with wx.MultiChoiceDialog(self, "只显示同时包含全部选中标签的文档", "标签筛选",
                                      [tag.name for tag in tags]) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self._leave_smart_list()
                    self._filters["tag_ids"] = tuple(tags[i].id for i in dialog.GetSelections())
                    self.summary.SetLabel("同时包含所选标签")
                    self._query(reset=True)
            return
        with wx.SingleChoiceDialog(self, "选择标签", "管理标签", [tag.name for tag in tags]) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            tag = tags[dialog.GetSelection()]
        if action == 1:
            with wx.TextEntryDialog(self, "新的标签名称", "重命名标签", tag.name) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    self._mutate("rename_tag", {"tag_id": tag.id, "name": dialog.GetValue().strip()})
        elif wx.MessageBox(f"删除标签“{tag.name}”及其全部文档关联？\n不会删除任何文件。",
                           "删除标签", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) == wx.YES:
            self._mutate("delete_tag", {"tag_id": tag.id})

    def _encoding(self, event=None):
        if not self._selected:
            return
        options = ["自动检测", "utf-8", "utf-8-sig", "gb18030", "gbk", "utf-16",
                   "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
        with wx.SingleChoiceDialog(self, "仅影响预览与索引，不转码或改写原文件。",
                                   "选择编码", options) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                index = dialog.GetSelection()
                self._mutate("set_encoding", {"document_id": self._selected.id,
                                             "encoding": options[index] if index else None})

    def _relocate_selected(self, event=None):
        document = self._selected
        if self._closing or document is None or document.availability != "missing":
            return
        library = next((item for item in self.state.libraries
                        if item.id == document.library_id), None)
        if library is None:
            self._error("文档库记录不可用，请刷新后重试。")
            return
        with wx.FileDialog(self, "选择同一文档库内的现有文件",
                           defaultDir=library.root_path,
                           wildcard="所有文件 (*.*)|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            target_path = dialog.GetPath()
        self._inspect_relocation(document, target_path)

    def _inspect_relocation(self, document, target_path):
        """Bind the entire confirmation chain to the original selected record."""
        self._relocation_generation += 1
        generation = self._relocation_generation

        def current():
            return (not self._closing and generation == self._relocation_generation
                    and self._selected is not None and self._selected.id == document.id
                    and self._selected.availability == "missing")

        def inspected(result):
            if not current():
                return
            library = next((item for item in self.state.libraries
                            if item.id == document.library_id), None)
            old_path = str(Path(library.root_path) / document.relative_path) if library else (
                document.relative_path)
            message = (f"旧记录：{document.file_name}\n原路径：{old_path}\n"
                       f"新路径：{target_path}\n\n"
                       "只更新管理关联，不移动、覆盖或删除任何文件。\n"
                       "原记录 ID、标签和收藏继续保留。\n\n"
                       f"{result.get('message', '')}\n\n确认将该记录关联到新路径？")
            if wx.MessageBox(message, "确认重新定位",
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                return
            if not current():
                return
            merge = bool(result.get("requires_merge_confirmation"))
            if merge:
                message = ("目标路径已有自动登记的占位记录，且没有人工标签、收藏、"
                           "最近打开或编码覆盖信息。\n\n"
                           "继续将合并此占位管理记录，并保留原缺失记录的 ID 和标签。\n"
                           "只合并管理信息，不移动、覆盖或删除文件。\n\n"
                           "是否明确同意合并占位记录？")
                if wx.MessageBox(message, "再次确认：合并占位管理记录",
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
                    return
            if not current():
                return

            def relocated(updated):
                if (self._closing or generation != self._relocation_generation
                        or self._selected is None or self._selected.id != document.id):
                    return
                self._load_preview(updated)
                self.SetStatusText("文件关联已更新；未移动、覆盖或删除任何文件。", 0)
            self._mutate("relocate_document",
                         {"document_id": document.id, "target_path": target_path,
                          "merge_unannotated": merge, "confirmed": True}, relocated)

        def failed(error):
            if current():
                self._error(error)
        self._submit("inspect_relocation",
                     {"document_id": document.id, "target_path": target_path},
                     inspected, failed)

    def _render_backup(self, status, last_success=None, message=""):
        labels = {"": "等待首次备份", "never": "尚未备份", "pending": "等待自动备份",
                  "ready": "已保护", "ok": "已保护", "success": "已保护",
                  "clean": "管理信息无新增变更", "error": "备份失败，请检查",
                  "failed": "备份失败，请检查", "dirty": "有变更待备份",
                  "running": "正在备份", "disabled": "自动备份未启用",
                  "up_to_date": "管理信息已备份", "due": "等待自动备份"}
        text = "管理信息保护：" + (message or labels.get(status, status))
        if last_success:
            text += " · 最近成功 " + format_time(last_success)
        text += " · 不含正文"
        self.backup_label.SetLabel(text)
        self.backup_label.SetToolTip(text)

    def _automatic_backup(self, event=None):
        if self._closing:
            return
        self._backup_generation += 1
        generation = self._backup_generation
        self.backup_button.Disable()
        self._render_backup("running")

        def ready(result):
            if generation != self._backup_generation:
                return
            self.backup_button.Enable()
            self._render_backup(result.get("status", ""), result.get("last_success"),
                                result.get("message", ""))

        def failed(error):
            if generation != self._backup_generation:
                return
            self.backup_button.Enable()
            self._render_backup("error")
            self._error(error)
        self._submit("automatic_backup", {"force": True}, ready, failed)

    def _backup_details(self, event=None):
        def ready(result):
            self._render_backup(result.get("status", ""), result.get("last_success"),
                                result.get("message", ""))
            last = (format_time(result["last_success"]) if result.get("last_success")
                    else "尚无成功备份")
            text = (f"状态：{result.get('message') or result.get('status', '未知')}\n"
                    f"最近成功：{last}\n"
                    f"管理信息有待保存的变更：{'是' if result.get('dirty') else '否'}\n"
                    f"已到自动备份时间：{'是' if result.get('due') else '否'}\n"
                    f"备份位置：{result.get('path') or '尚无备份路径'}\n\n"
                    "自动备份仅保护文档库关联、标签、收藏及配置，不包含文档正文。\n"
                    "正文文件需要另外备份。恢复入口位于“文件 → 恢复管理信息”。")
            wx.MessageBox(text, "自动备份状态", wx.OK | wx.ICON_INFORMATION, self)
        self._submit("backup_status", {}, ready)

    def _scan(self, event=None):
        library = self._current_library()
        self._mutate("scan", {"library_id": library.id if library else None})

    def _cancel_scan(self, event=None):
        self._submit("cancel_scan", {}, lambda value:
                     self.SetStatusText("已请求停止扫描；已完成的登记和索引会保留。", 0))

    def _rebuild(self, event=None):
        if wx.MessageBox("重新扫描并重建正文搜索索引，不修改原文件或标签。\n是否继续？",
                         "重建索引", wx.YES_NO | wx.NO_DEFAULT, self) == wx.YES:
            self._mutate("rebuild_index", {})

    def _export(self, event=None):
        with wx.FileDialog(self, "导出管理信息（不含正文）", defaultFile="notes-metadata.json",
                           wildcard="JSON 文件 (*.json)|*.json",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._submit("export_backup", {"path": dialog.GetPath()}, lambda value:
                             wx.MessageBox("管理信息已导出。正文文件需要另行备份。",
                                           "导出完成", parent=self))

    def _restore(self, event=None):
        if self._closing:
            return
        # Opening a new picker is a new intent, including cancelling that picker.
        self._restore_generation += 1
        generation = self._restore_generation
        with wx.FileDialog(self, "选择管理信息备份", wildcard="JSON 文件 (*.json)|*.json",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        def inspected(summary):
            if generation != self._restore_generation:
                return
            expected_backup = summary.get("signature")
            expected_current = summary.get("current_signature")
            if not all(type(value) is str and len(value) == 64
                       and all(char in "0123456789abcdef" for char in value)
                       for value in (expected_backup, expected_current)):
                self._error("备份检查未提供有效的双重确认指纹，请重新选择并检查备份；尚未恢复。")
                return
            if wx.MessageBox(self._confirm_restore_message(summary), "确认恢复管理信息",
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) == wx.YES:
                # The modal confirmation runs an event loop: another selection
                # or shutdown may have superseded this inspection while open.
                if self._closing or generation != self._restore_generation:
                    return

                def failed(error):
                    if getattr(error, "code", "") == "restored_index_pending":
                        self._restore_committed(index_warning=getattr(error, "message", str(error)))
                    elif getattr(error, "code", "") == "restored_followup_failed":
                        self._restore_committed(followup_warning=getattr(error, "message", str(error)))
                    else:
                        # In particular backup_changed / metadata_changed never
                        # trigger an automatic reinspection, retry, or UI reset.
                        self._error(error)
                self._submit("restore_backup",
                             {"path": path, "confirmed": True,
                              "expected_backup": expected_backup,
                              "expected_current": expected_current},
                             lambda value: self._restore_committed(), failed)

        def inspect_failed(error):
            if generation == self._restore_generation:
                self._error(error)
        self._submit("inspect_backup", {"path": path}, inspected, inspect_failed)

    def _restore_committed(self, index_warning="", followup_warning=""):
        """Refresh actual committed metadata, never silently retry restoration."""
        if self._closing:
            return
        self._filters = {}
        self._folders.clear()
        self._page = 0
        self._query_generation += 1
        self.list.replace((), self.state.libraries)
        self._clear_preview()
        self.previous.Disable()
        self.next.Disable()
        if self._active_smart_list_id is None:
            self.summary.SetLabel("所有文档")
        self._build_tree()
        warning = index_warning or followup_warning
        if index_warning:
            message = "管理信息已恢复；搜索索引尚未完成，请刷新或重建索引，不要重复恢复。"
        elif followup_warning:
            message = "管理信息已恢复；后续刷新发生异常。已请求刷新，请核对结果或重新启动，不要重复恢复。"
        else:
            message = "管理信息已恢复，正在刷新；正文未修改。"
        self.SetStatusText(message, 0)

        def state_ready(state):
            self._on_state(state)
            self._query()
            if warning:
                self.SetStatusText(message, 0)
        self._submit("state", {}, state_ready)
        if warning:
            wx.MessageBox(message + "\n" + warning,
                          "管理信息已恢复，索引待处理" if index_warning else "管理信息已恢复，后续刷新待处理",
                          wx.OK | wx.ICON_WARNING, self)

    def _confirm_restore_message(self, summary):
        names = {"format_version": "备份格式版本", "libraries": "文档库数",
                 "documents": "文档记录数", "tags": "标签数", "smart_lists": "智能列表数",
                 "replaces_existing": "替换现有管理信息",
                 "contains_document_bodies": "包含正文",
                 "editor_requires_confirmation": "编辑器需要重新确认",
                 "unavailable_paths": "不可用路径", "library_paths": "文档库路径"}
        # Fingerprints are protocol evidence, not useful confirmation prose.
        values = {key: value for key, value in summary.items()
                  if key not in {"signature", "current_signature"}}
        if "smart_lists" not in values:
            values["smart_lists"] = 0 if values.get("format_version") == 1 else "未提供"
        details = "\n".join(f"{names.get(key, key)}："
                            f"{('是' if value else '否') if isinstance(value, bool) else value}"
                            for key, value in values.items())
        legacy = ("\n此格式 1 备份不含智能列表；恢复将清空当前保存的智能列表，"
                  "不会删除正文。\n" if values.get("format_version") == 1 else "")
        return ("备份校验通过：\n" + details +
                "\n\n将替换当前文档库、标签、收藏及智能列表等管理信息。\n" + legacy +
                "不会恢复、覆盖或删除正文。恢复前将保护备份当前管理信息。\n"
                "导入的编辑器路径不会自动启用。\n\n确定替换？")

    def _document_context(self, event):
        if 0 <= event.GetIndex() < len(self.list.hits):
            self.list.Select(event.GetIndex())
            self._load_preview(self.list.hits[event.GetIndex()].document)
        menu = wx.Menu()
        for label, callback in (("用 Notepad3 编辑", self._open_selected),
                                ("收藏 / 取消收藏", self._favorite),
                                ("设置标签…", self._assign_tags),
                                ("复制完整路径", self._copy_path),
                                ("选择编码…", self._encoding),
                                ("重新定位文件…", self._relocate_selected)):
            item = menu.Append(wx.ID_ANY, label)
            if callback == self._relocate_selected:
                item.Enable(self._selected is not None and self._selected.availability == "missing")
            menu.Bind(wx.EVT_MENU, callback, item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _tree_context(self, event):
        if self._closing:
            return
        self.tree.SelectItem(event.GetItem())
        data = self.tree.GetItemData(event.GetItem())
        if data is None:
            return
        if "smart_list_id" in data:
            self._navigation_generation += 1
            if self._active_smart_list_id != data["smart_list_id"]:
                self._activate_smart_list(data["smart_list_id"])
            menu = wx.Menu()
            for label, callback in (("编辑完整筛选条件…", self._edit_smart_list),
                                    ("另存为新的智能列表…", self._save_current_smart_list),
                                    ("删除智能列表…", self._delete_smart_list)):
                item = menu.Append(wx.ID_ANY, label)
                menu.Bind(wx.EVT_MENU, callback, item)
            self.PopupMenu(menu)
            menu.Destroy()
            return
        self._leave_smart_list()
        self._filters = dict(data)
        if not self._current_library():
            return
        menu = wx.Menu()
        for label, callback in (("刷新文档库", self._scan), ("修改规则…", self._library_rules),
                                ("停用 / 启用", self._toggle_library)):
            item = menu.Append(wx.ID_ANY, label)
            menu.Bind(wx.EVT_MENU, callback, item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _copy_path(self, event=None):
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self.path_label.GetValue()))
            finally:
                wx.TheClipboard.Close()

    def _open_quick(self, event=None):
        if self._closing:
            return
        if self._quick_dialog and not self._quick_dialog._closed:
            self._quick_dialog.focus_search()
            return
        from notes.ui.quick_open import QuickOpenDialog
        if self.IsIconized():
            self.Iconize(False)
        dialog = QuickOpenDialog(self, self.service, on_configure_editor=self._configure_editor)
        self._quick_dialog = dialog

        def destroyed(event):
            if not self._closing:
                if dialog.opened_document_id:
                    self.SetStatusText("已请求 Notepad3 打开文档；不代表已加载或保存。", 0)
                elif dialog.open_request_cancelled is True:
                    self.SetStatusText("已关闭快速打开，并取消尚未执行的编辑请求。", 0)
                elif dialog.open_request_cancelled is False:
                    self.SetStatusText("已关闭快速打开；编辑请求已开始，可能无法撤回。", 0)
            if self._quick_dialog is dialog:
                self._quick_dialog = None
            event.Skip()
        # wx may supply a different Python wrapper during native destruction;
        # bind this exact window ID instead of relying on object identity.
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed, id=dialog.GetId())
        dialog.Show()
        dialog.focus_search()

    def _hotkey_adapter(self):
        if self._hotkey is None:
            from notes.ui.hotkeys import GlobalQuickOpenHotkey
            self._hotkey = GlobalQuickOpenHotkey(self, self._open_quick)
        return self._hotkey

    def _hotkey_error(self, error):
        self._hotkey_notice = "全局快捷键未完成：" + getattr(error, "message", str(error))
        self.SetStatusText(self._hotkey_notice, 0)
        self.hotkey_menu.Check(bool(self._hotkey and self._hotkey.enabled))

    def _sync_quick_hotkey(self, enabled):
        if self._hotkey_changing or self._closing or enabled == self._hotkey_preference_seen:
            return
        # Persisted state is applied once per change, not on every status event.
        # A conflict must not continuously retry registration in the background.
        self._hotkey_preference_seen = enabled
        try:
            if enabled:
                if not self._hotkey_adapter().enable():
                    self._hotkey_error("当前系统不支持全局快捷键。")
                    return
            elif self._hotkey:
                self._hotkey.disable()
            self._hotkey_notice = ""
            self.hotkey_menu.Check(bool(self._hotkey and self._hotkey.enabled))
        except Exception as error:
            self._hotkey_error(error)

    def _toggle_quick_hotkey(self, event=None):
        if self._closing or self._hotkey_changing:
            return
        desired = self.hotkey_menu.IsChecked()
        try:
            adapter = self._hotkey_adapter()
            was_enabled = adapter.enabled
            if desired:
                if not adapter.enable():
                    self._hotkey_error("当前系统不支持全局快捷键。")
                    return
            else:
                adapter.disable()
        except Exception as error:
            self._hotkey_error(error)
            return
        self._hotkey_changing = True
        self.hotkey_menu.Enable(False)

        def ready(value):
            self._hotkey_changing = False
            self._hotkey_preference_seen = desired
            self.hotkey_menu.Enable(True)
            self.hotkey_menu.Check(adapter.enabled)
            self._hotkey_notice = ""
            self.SetStatusText("全局 Ctrl+Alt+N 已启用；仅本程序运行时有效，不自启动、不驻留托盘。"
                               if desired else "全局快捷键已关闭。", 0)

        def failed(error):
            self._hotkey_changing = False
            self.hotkey_menu.Enable(True)
            try:
                if was_enabled:
                    adapter.enable()
                else:
                    adapter.disable()
            except Exception as rollback_error:
                self._hotkey_error("设置未保存，快捷键还原也失败：" +
                                   getattr(rollback_error, "message", str(rollback_error)))
                return
            self._hotkey_error("设置未保存，已还原注册状态：" +
                               getattr(error, "message", str(error)))
        # Registration/unregistration succeeded before persisting the preference.
        self._submit("set_quick_open_hotkey", {"enabled": desired}, ready, failed)

    def _close_shortcuts(self):
        if self._ui_shortcuts_closed:
            return
        self._ui_shortcuts_closed = True
        if self._quick_dialog and not self._quick_dialog._closed:
            self._quick_dialog.Destroy()
        if self._hotkey:
            try:
                self._hotkey.close()
            except Exception:
                # Frame destruction also releases its HWND hotkey registration.
                # Never let adapter cleanup prevent safe service shutdown.
                pass

    def _key(self, event):
        if self._closing:
            return
        code = event.GetKeyCode()
        if event.ControlDown() and code == ord("P"):
            self._open_quick()
        elif event.ControlDown() and code == ord("F"):
            self.search.SetFocus()
            self.search.SelectAll()
        elif event.ControlDown() and code == ord("N"):
            self._new_document()
        elif code == wx.WXK_F5:
            self._scan()
        elif code == wx.WXK_ESCAPE:
            self._debounce.Stop()
            self._query_generation += 1
            if self._query_future:
                self._query_future.cancel()
            self._submit("cancel_query", {})
            self.empty.SetLabel("查询已取消，保留之前结果；可重新搜索。")
            self.SetStatusText("查询已取消，保留之前结果。", 0)
        else:
            event.Skip()

    def _close(self, event):
        if self._closing:
            if event.CanVeto():
                event.Veto()
            return
        if event.CanVeto():
            event.Veto()
        self._closing = True
        self._stop_activation_poll()
        self._debounce.Stop()
        self._close_shortcuts()
        self.Disable()
        self.SetStatusText("正在安全结束后台任务…", 0)

        def worker():
            try:
                self.service.close()
            finally:
                wx.CallAfter(self.Destroy)
        Thread(target=worker, name="notes-ui-close", daemon=False).start()

    def Destroy(self):
        # Also guard platform/test initiated destruction that bypasses EVT_CLOSE.
        self._closing = True
        self._stop_activation_poll()
        if hasattr(self, "_ui_shortcuts_closed"):
            self._close_shortcuts()
        if hasattr(self, "_debounce"):
            self._debounce.Stop()
        return super().Destroy()
