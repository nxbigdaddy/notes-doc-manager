"""Explicit, metadata-only startup recovery; no UI-thread filesystem/database IO."""
from __future__ import annotations

from queue import Queue
from threading import Event, Thread

import wx

from notes.ui.formatting import format_size, format_time


class RecoveryDialog(wx.Dialog):
    """Serial recovery worker with cancellation and safe, nonblocking shutdown.

    ``result`` is the backend's successful recovery result when ShowModal returns
    wx.ID_OK. ``exit_requested`` preserves a user's choice to exit even when
    cancellation arrived after commit; callers should not open the main window
    in that case. The caller must not construct ApplicationService until then.
    No empty-store creation, automatic recovery, or implicit resume occurs here.
    """
    def __init__(self, parent, manager, initial_status: dict):
        super().__init__(parent, title="Notes · 管理信息恢复",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize(self.FromDIP((790, 670)))
        self.manager = manager
        self.initial_status = dict(initial_status)
        self.result = None
        self.exit_requested = False
        self.inspection = None
        self._candidates = ()
        self._generation = 0
        self._busy = False
        self._closing = False
        self._closed = False
        self._destroy_requested = False
        self._exit_code = wx.ID_CANCEL
        self._last_outcome = None
        self._cancel = Event()
        self._queue = Queue()
        self._build_ui()
        self._shutdown_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_shutdown, self._shutdown_timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self._worker = Thread(target=self._run, name="notes-recovery-ui", daemon=False)
        self._worker.start()
        self._render_current()
        self._update_controls()
        if self.initial_status.get("recoverable") is True:
            self._load_backups()
        self.CentreOnParent()

    def _build_ui(self):
        layout = wx.BoxSizer(wx.VERTICAL)
        heading = wx.StaticText(self, label="管理信息库暂时无法使用")
        heading.SetFont(heading.GetFont().Bold().Larger())
        layout.Add(heading, 0, wx.EXPAND | wx.ALL, 16)
        self.current_label = wx.StaticText(self, label="", style=wx.ST_ELLIPSIZE_END)
        layout.Add(self.current_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        explanation = wx.StaticText(
            self, label="这不代表正文文件损坏。恢复只替换文档库关联、标签、收藏等管理信息，"
                        "不会移动、删除或覆盖正文。\n不创建空库，也不会悄悄覆盖当前数据。")
        explanation.Wrap(self.FromDIP(740))
        layout.Add(explanation, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        layout.Add(wx.StaticText(self, label="可用的自动备份 / 恢复前保护快照"), 0,
                   wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.backup_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for name, width in (("备份名称", 355), ("修改时间", 175), ("大小", 95)):
            self.backup_list.InsertColumn(self.backup_list.GetColumnCount(), name,
                                          width=self.FromDIP(width))
        self.backup_list.SetMinSize(self.FromDIP((600, 135)))
        layout.Add(self.backup_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.backup_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.browse_button = wx.Button(self, label="选择本地 .json / .db…")
        self.recheck_button = wx.Button(self, label="重新检查当前库")
        self.inspect_button = wx.Button(self, label="重新校验备份")
        for button in (self.browse_button, self.recheck_button, self.inspect_button):
            actions.Add(button, 0, wx.RIGHT, 8)
        self.browse_button.Bind(wx.EVT_BUTTON, self._browse)
        self.recheck_button.Bind(wx.EVT_BUTTON, self._recheck)
        self.inspect_button.Bind(wx.EVT_BUTTON, lambda event: self._inspect(self.path.GetValue()))
        layout.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.path = wx.TextCtrl(self, style=wx.TE_READONLY)
        self.path.SetHint("选择并校验备份后才能确认恢复")
        layout.Add(self.path, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.details.SetMinSize(self.FromDIP((600, 105)))
        layout.Add(self.details, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.consent = wx.CheckBox(
            self, label="我确认替换当前管理信息；备份不含正文，编辑器配置需要重新确认。")
        self.consent.Bind(wx.EVT_CHECKBOX, lambda event: self._update_controls())
        layout.Add(self.consent, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.status_label = wx.StaticText(self, label="请选择备份；不会自动执行恢复。",
                                         style=wx.ST_ELLIPSIZE_END)
        layout.Add(self.status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        footer.AddStretchSpacer()
        self.recover_button = wx.Button(self, label="确认替换并恢复管理信息")
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, label="退出，不恢复")
        footer.Add(self.recover_button, 0, wx.RIGHT, 8)
        footer.Add(self.cancel_button)
        self.recover_button.Bind(wx.EVT_BUTTON, self._recover)
        self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        layout.Add(footer, 0, wx.EXPAND | wx.ALL, 16)
        self.SetSizer(layout)
        self.Fit()
        self.cancel_button.SetDefault()

    def _set_status(self, message):
        self.status_label.SetLabel(message)
        self.status_label.SetToolTip(message)

    def _render_current(self):
        message = self.initial_status.get("message") or self.initial_status.get("status", "未知状态")
        version = self.initial_status.get("version")
        text = f"当前状态：{message}" + (f" · 数据版本 {version}" if version is not None else "")
        if self.initial_status.get("recoverable") is not True:
            text += " · 此状态禁止覆盖恢复"
        self.current_label.SetLabel(text)
        self.current_label.SetToolTip(text)

    def _update_controls(self):
        active = not self._busy and not self._closing
        allowed = self.initial_status.get("recoverable") is True
        self.browse_button.Enable(active and allowed)
        self.backup_list.Enable(active and allowed)
        self.recheck_button.Enable(active)
        self.inspect_button.Enable(active and allowed and bool(self.path.GetValue()))
        self.consent.Enable(active and allowed and self.inspection is not None)
        self.recover_button.Enable(active and allowed and self.inspection is not None
                                   and self.consent.GetValue())
        self.cancel_button.Enable(not self._closing)
        if self._busy:
            self.cancel_button.SetLabel("取消 / 安全退出")
        elif not self._closing:
            self.cancel_button.SetLabel("退出，不恢复")

    def _submit(self, operation, function):
        if self._busy or self._closing:
            return False
        self._generation += 1
        self._busy = True
        self._cancel.clear()
        self._update_controls()
        self._queue.put((self._generation, operation, function))
        return True

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                # Also wake shutdown coordination when hidden test/startup
                # windows have no native timer dispatch yet.
                wx.CallAfter(self._poll_shutdown)
                return
            generation, operation, function = item
            value = error = None
            try:
                if self._cancel.is_set():
                    raise RuntimeError("操作已取消，尚未提交恢复")
                value = function()
            except Exception as exc:
                error = exc
            self._last_outcome = (generation, operation, value, error)
            wx.CallAfter(self._completed, generation, operation, value, error)

    def _completed(self, generation, operation, value, error):
        if self._closed or self._closing or generation != self._generation:
            return
        self._busy = False
        if error is not None:
            if operation == "inspect":
                self.inspection = None
                self.consent.SetValue(False)
            self._set_status("操作未完成：" + getattr(error, "message", str(error)))
            self._update_controls()
            return
        if operation == "list":
            self._candidates = tuple(value)
            self.backup_list.DeleteAllItems()
            for item in self._candidates:
                index = self.backup_list.InsertItem(self.backup_list.GetItemCount(),
                                                    item.get("name") or item.get("path", "备份"))
                self.backup_list.SetItem(index, 1, format_time(item.get("mtime") or 0))
                self.backup_list.SetItem(index, 2, format_size(item.get("size") or 0))
            self._set_status("请选择一个备份进行校验。" if self._candidates else
                             "未发现候选快照；可以选择本地备份。不会自动创建空库。")
        elif operation == "inspect":
            self.inspection = dict(value)
            self.consent.SetValue(False)
            self.details.SetValue(self._inspection_text(value))
            self._set_status("校验通过。请阅读恢复范围，勾选确认后才能替换管理信息。")
        elif operation == "current":
            self.initial_status = dict(value)
            self.inspection = None
            self.consent.SetValue(False)
            self._render_current()
            self.details.SetValue("当前库已重新检查；此前的备份确认已失效，请重新选择并校验。")
            self._set_status("当前状态允许选择备份。" if value.get("recoverable") is True else
                             "当前状态不允许覆盖恢复。请处理版本、占用或权限问题后重试，或退出。")
            self._update_controls()
            if value.get("recoverable") is True:
                self._load_backups()
                return
        elif operation == "recover":
            if value.get("recovered") is True:
                self.result = dict(value)
                self._set_status(value.get("message") or "管理信息已恢复；正文未修改。")
                self._begin_close(wx.ID_OK)
                return
            self._set_status(value.get("message") or "恢复未提交；可以重新检查或选择其他备份。")
        self._update_controls()

    def _inspection_text(self, value):
        candidate = next((item for item in self._candidates
                          if item.get("path") == value.get("path")), {})
        stamp = value.get("mtime") or value.get("created_at") or candidate.get("mtime")
        if isinstance(stamp, (int, float)):
            timestamp = format_time(stamp)
        else:
            timestamp = str(stamp) if stamp else "备份未提供时间"
        return (f"类型：{value.get('kind', '已校验备份')}    时间：{timestamp}\n"
                f"文档库：{value.get('libraries', 0)}    文档记录：{value.get('documents', 0)}"
                f"    标签：{value.get('tags', 0)}    智能列表：{value.get('smart_lists', 0)}\n"
                "不包含文档正文；原正文不移动、不删除、不覆盖。\n"
                "当前管理信息将先保留隔离副本。恢复后编辑器配置需要重新确认。")

    def _load_backups(self):
        if self.initial_status.get("recoverable") is not True:
            return
        self._set_status("正在后台查找候选备份…")
        self._submit("list", self.manager.list_backups)

    def _inspect(self, path):
        if self._busy or self._closing or self.initial_status.get("recoverable") is not True or not path:
            return
        self.inspection = None
        self.consent.SetValue(False)
        self.path.SetValue(str(path))
        self.details.SetValue("正在校验备份完整性与兼容性，不会修改当前库。")
        self._set_status("正在后台校验备份…")
        self._submit("inspect", lambda: self.manager.inspect_backup(str(path)))

    def _on_select(self, event):
        if self._closing or self._busy:
            return
        if 0 <= event.GetIndex() < len(self._candidates):
            self._inspect(self._candidates[event.GetIndex()]["path"])

    def _browse(self, event=None):
        if self._busy or self._closing or self.initial_status.get("recoverable") is not True:
            return
        with wx.FileDialog(self, "选择管理信息备份",
                           wildcard="管理信息备份 (*.json;*.db)|*.json;*.db",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as picker:
            if picker.ShowModal() == wx.ID_OK:
                self._inspect(picker.GetPath())

    def _recheck(self, event=None):
        if not self._busy and not self._closing:
            self._set_status("正在后台检查当前库；不会自动覆盖或重建。")
            self._submit("current", self.manager.inspect_current)

    def _recover(self, event=None):
        if (self._busy or self._closing or self.initial_status.get("recoverable") is not True
                or self.inspection is None or not self.consent.GetValue()):
            return
        backup = dict(self.inspection)
        path = str(backup.get("path") or self.path.GetValue())
        expected_current = self.initial_status.get("signature")
        expected_backup = backup.get("signature")
        self._set_status("正在安全恢复。可请求取消；若已进入提交阶段，会先安全完成再退出。")
        self._submit("recover", lambda: self.manager.recover(
            path, confirmed=True, expected_current=expected_current,
            expected_backup=expected_backup, cancel=self._cancel))

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._on_cancel()
        else:
            event.Skip()

    def _on_cancel(self, event=None):
        self.exit_requested = True
        self._begin_close(wx.ID_CANCEL)

    def _on_close(self, event):
        if event.CanVeto():
            event.Veto()
        self.exit_requested = True
        self._begin_close(wx.ID_CANCEL)

    def _begin_close(self, code):
        if self._closed or self._closing:
            return
        self._closing = True
        self._exit_code = code
        self._cancel.set()
        self._set_status("正在等待后台任务安全退出；若恢复已提交，将保留已完成的恢复。")
        self.cancel_button.SetLabel("正在安全退出…")
        self._update_controls()
        self._queue.put(None)
        self._shutdown_timer.Start(15)

    def _poll_shutdown(self, event=None):
        if self._closed:
            return
        if self._worker.is_alive():
            if event is None and self._closing:
                # The worker posted this immediately before returning. Retry
                # asynchronously rather than joining or touching its task.
                wx.CallAfter(self._poll_shutdown)
            return
        self._shutdown_timer.Stop()
        self._worker.join(timeout=0)
        # A cancellation request may arrive after the backend's commit point.
        # Report that actual successful outcome instead of claiming cancellation.
        outcome = self._last_outcome
        if outcome and outcome[1] == "recover" and outcome[3] is None:
            value = outcome[2]
            if isinstance(value, dict) and value.get("recovered") is True:
                self.result = dict(value)
                self._exit_code = wx.ID_OK
        self._closed = True
        if self.IsModal():
            self.EndModal(self._exit_code)
        else:
            self.SetReturnCode(self._exit_code)
            self.Hide()
        if self._destroy_requested:
            super().Destroy()

    def Destroy(self):
        # Native controls outlive every worker callback, even for callers that
        # directly destroy a nonmodal test/startup dialog.
        if hasattr(self, "_worker") and self._worker.is_alive():
            self._destroy_requested = True
            self._begin_close(wx.ID_CANCEL)
            return True
        self._closed = True
        if hasattr(self, "_shutdown_timer"):
            self._shutdown_timer.Stop()
        return super().Destroy()
