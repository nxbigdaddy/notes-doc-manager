"""Small native quick-open window; query and cancellation use an isolated lane."""
from dataclasses import asdict

import wx

from notes.domain.models import AppState, SearchQuery


class QuickOpenDialog(wx.Dialog):
    def __init__(self, parent, service, on_configure_editor=None):
        super().__init__(parent, title="快速打开 · Notes",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize(self.FromDIP((680, 450)))
        self.service = service
        self.on_configure_editor = on_configure_editor
        self.state = AppState()
        self.hits = ()
        self.opened_document_id = None
        self.open_request_cancelled = None
        self._open_future = None
        self._closed = False
        self._generation = 0
        self._opening = False
        self._query_pending = False
        self._unsubscribe = None
        layout = wx.BoxSizer(wx.VERTICAL)
        self.search = wx.SearchCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.search.SetDescriptiveText("输入文件名；留空查看最近打开")
        self.search.ShowCancelButton(True)
        layout.Add(self.search, 0, wx.EXPAND | wx.ALL, 14)
        scope = wx.StaticText(self, label="搜索所有启用且在线的文档库，不继承主列表筛选。")
        layout.Add(scope, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "文档名称", width=self.FromDIP(220))
        self.list.InsertColumn(1, "文档库 / 相对路径", width=self.FromDIP(395))
        layout.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.status = wx.StaticText(self, label="正在读取最近打开的文档…",
                                    style=wx.ST_ELLIPSIZE_END)
        layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        self.configure_button = wx.Button(self, label="配置 Notepad3…")
        self.configure_button.Enable(on_configure_editor is not None)
        footer.Add(self.configure_button, 0, wx.RIGHT, 8)
        footer.AddStretchSpacer()
        self.open_button = wx.Button(self, label="打开选中文档")
        self.close_button = wx.Button(self, wx.ID_CANCEL, label="关闭")
        footer.Add(self.open_button, 0, wx.RIGHT, 8)
        footer.Add(self.close_button)
        layout.Add(footer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.SetSizer(layout)
        self.Fit()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._query(), self._timer)
        self.search.Bind(wx.EVT_TEXT, self._text_changed)
        self.search.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda event: self.search.SetValue(""))
        self.search.Bind(wx.EVT_TEXT_ENTER, self._activate)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._activate)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda event: self._update_open_button())
        self.open_button.Bind(wx.EVT_BUTTON, self._activate)
        self.close_button.Bind(wx.EVT_BUTTON, lambda event: self.Destroy())
        self.configure_button.Bind(wx.EVT_BUTTON, self._configure)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.Bind(wx.EVT_CLOSE, lambda event: self.Destroy())
        self._unsubscribe = service.subscribe(self._on_state)
        self._submit("state", {}, self._on_state)
        self._query()
        self.CentreOnParent()
        self.search.SetFocus()

    def _set_status(self, message):
        self.status.SetLabel(message)
        self.status.SetToolTip(message)

    def _submit(self, command, payload, success=None, error=None):
        if self._closed:
            return None

        def ready(value):
            if not self._closed and success:
                success(value)

        def failed(exc):
            if not self._closed:
                if error:
                    error(exc)
                else:
                    self._set_status(getattr(exc, "message", str(exc)))
        try:
            return self.service.submit(command, payload, ready, failed)
        except Exception as exc:
            failed(exc)
            return None

    def _on_state(self, state):
        if self._closed:
            return
        previous = self.state
        self.state = state
        if (state.libraries != previous.libraries
                or state.data_revision != previous.data_revision):
            if not self._opening:
                self._query()

    def _text_changed(self, event):
        if self._closed:
            return
        self._generation += 1
        self.hits = ()
        self.list.DeleteAllItems()
        self._query_pending = True
        self._update_open_button()
        self._timer.StartOnce(200)

    def _query(self):
        if self._closed:
            return
        self._timer.Stop()
        self._generation += 1
        generation = self._generation
        self._query_pending = True
        self._update_open_button()
        raw_text = self.search.GetValue()
        text = raw_text if raw_text.strip() else ""
        query = SearchQuery(text=text, mode="name", page_size=20, recent_only=not text)
        self._set_status("正在搜索文件名…" if text else "正在读取最近打开…")

        def ready(result):
            if generation != self._generation:
                return
            self._query_pending = False
            self.hits = result.items
            self.list.DeleteAllItems()
            libraries = {lib.id: lib.name for lib in self.state.libraries}
            for hit in self.hits:
                doc = hit.document
                index = self.list.InsertItem(self.list.GetItemCount(), doc.file_name)
                self.list.SetItem(index, 1, libraries.get(doc.library_id, doc.library_id)
                                  + " / " + doc.relative_path)
            incomplete = not result.complete or bool(result.warning)
            if self.hits:
                self.list.Select(0)
                self.list.Focus(0)
                self._set_status(
                    f"显示 {len(self.hits)} / {result.total} 项 · 上下选择，Enter 打开"
                    + ("；继续输入可缩小范围" if result.total > 20 else ""))
                if incomplete:
                    self._set_status(f"当前显示 {len(self.hits)} 项，结果可能不完整。"
                                     + (result.warning or "请稍后重试。"))
            elif incomplete:
                self._set_status("查询尚未完整完成，当前没有可显示的结果。"
                                 + (result.warning or "请重试；不能据此判断没有匹配文档。"))
            else:
                self._set_status("没有匹配的文件名。" if text else
                                 "暂无最近打开记录，输入文件名开始查找。")
            self._update_open_button()

        def failed(error):
            if generation == self._generation:
                self._query_pending = False
                self.hits = ()
                self.list.DeleteAllItems()
                self._set_status("查询未完成：" + getattr(error, "message", str(error)))
                self._update_open_button()
        self._submit("query", {**asdict(query), "channel": "quick"}, ready, failed)

    def _update_open_button(self):
        if self._closed:
            return
        selected = self.list.GetFirstSelected()
        self.open_button.Enable(not self._opening and not self._query_pending
                                and 0 <= selected < len(self.hits))

    def _move_selection(self, delta):
        if not self.hits or self._query_pending or self._opening:
            return
        current = self.list.GetFirstSelected()
        target = min(len(self.hits) - 1, max(0, current + delta))
        if current >= 0:
            self.list.Select(current, False)
        self.list.Select(target)
        self.list.Focus(target)
        self.list.EnsureVisible(target)
        self._update_open_button()

    def _activate(self, event=None):
        if self._closed or self._opening or self._query_pending:
            return
        index = self.list.GetFirstSelected()
        if not 0 <= index < len(self.hits):
            return
        document_id = self.hits[index].document.id
        self._opening = True
        self._timer.Stop()
        self.search.Disable()
        self.list.Disable()
        self._update_open_button()
        self._set_status("正在请求 Notepad3 打开，不代表已加载或保存；"
                         "关闭会取消尚未执行的请求，已开始的请求可能无法撤回。")

        def opened(value):
            self.opened_document_id = document_id
            self.Destroy()

        def failed(error):
            self._opening = False
            self.search.Enable()
            self.list.Enable()
            self._set_status(getattr(error, "message", str(error)))
            self._update_open_button()
            self.search.SetFocus()
        self._open_future = self._submit(
            "open_document", {"document_id": document_id}, opened, failed)

    def _configure(self, event=None):
        if self._closed or self._opening or self.on_configure_editor is None:
            return
        self.on_configure_editor()

    def _key(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.Destroy()
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._activate()
        elif key == wx.WXK_DOWN:
            self._move_selection(1)
        elif key == wx.WXK_UP:
            self._move_selection(-1)
        else:
            event.Skip()

    def focus_search(self):
        if not self._closed:
            if not self.IsShown():
                self.Show()
            self.Raise()
            self.search.SetFocus()

    def Destroy(self):
        if self._closed:
            return False
        self._closed = True
        self._generation += 1
        self._timer.Stop()
        if self._open_future is not None and not self._open_future.done():
            # A queued external-editor action can still be cancelled. An action
            # already running is not reversible; never terminate the editor.
            self.open_request_cancelled = self._open_future.cancel()
        if callable(self._unsubscribe):
            self._unsubscribe()
        try:
            self.service.submit("cancel_query", {"channel": "quick"}, lambda value: None,
                                lambda error: None)
        except Exception:
            pass  # Parent service may already be shutting down.
        return super().Destroy()
