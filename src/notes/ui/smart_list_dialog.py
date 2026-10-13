"""Complete smart-list filter editor. Missing references remain selected by ID."""
import wx

from notes.domain.models import SearchQuery


class SmartListDialog(wx.Dialog):
    def __init__(self, parent, state, smart_list=None, query=None):
        super().__init__(parent, title="编辑智能列表" if smart_list else "新建智能列表",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize(self.FromDIP((650, 690)))
        self.state = state
        self.smart_list = smart_list
        original = smart_list.query if smart_list else query or SearchQuery()
        layout = wx.BoxSizer(wx.VERTICAL)

        def field(label, value):
            layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            control = wx.TextCtrl(self, value=value)
            layout.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
            return control

        self.name = field("列表名称（只保存筛选条件，不移动文件）",
                          smart_list.name if smart_list else "")
        self.text = field("搜索词（完整字面子串）", original.text)
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        mode_row.Add(wx.StaticText(self, label="搜索范围"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.mode = wx.Choice(self, choices=["全部", "文件名", "正文"])
        self.mode.SetSelection(("all", "name", "content").index(original.mode))
        mode_row.Add(self.mode, 1)
        layout.Add(mode_row, 0, wx.EXPAND | wx.ALL, 12)
        self.library_ids = [None]
        library_names = ["所有文档库"]
        for library in state.libraries:
            self.library_ids.append(library.id)
            suffix = "（已停用）" if not library.enabled else (
                "（离线）" if library.availability not in ("online", "unknown") else "")
            library_names.append(library.name + suffix)
        if original.library_id is not None and original.library_id not in self.library_ids:
            self.library_ids.append(original.library_id)
            library_names.append("失效文档库 · " + original.library_id)
        layout.Add(wx.StaticText(self, label="文档库（失效引用不会自动移除）"), 0,
                   wx.LEFT | wx.RIGHT, 12)
        self.library = wx.Choice(self, choices=library_names)
        self.library.SetSelection(self.library_ids.index(original.library_id))
        layout.Add(self.library, 0, wx.EXPAND | wx.ALL, 12)
        self.folder = field("库内相对文件夹（留空为根目录；非空时必须选择文档库）", original.folder)
        layout.Add(wx.StaticText(self, label="标签：必须同时包含所有勾选项；失效项需明确取消或修复"),
                   0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.tag_ids = [tag.id for tag in state.tags]
        tag_names = [tag.name for tag in state.tags]
        for identifier in original.tag_ids:
            if identifier not in self.tag_ids:
                self.tag_ids.append(identifier)
                tag_names.append("失效标签 · " + identifier)
        self.tags = wx.CheckListBox(self, choices=tag_names)
        self.tags.SetMinSize(self.FromDIP((550, 95)))
        for index, identifier in enumerate(self.tag_ids):
            self.tags.Check(index, identifier in original.tag_ids)
        layout.Add(self.tags, 1, wx.EXPAND | wx.ALL, 12)
        self.favorites = wx.CheckBox(self, label="仅收藏")
        self.recent = wx.CheckBox(self, label="仅最近打开")
        self.unavailable = wx.CheckBox(self, label="包含缺失 / 离线记录（停用库仍不可查询）")
        for control, value in ((self.favorites, original.favorites_only),
                               (self.recent, original.recent_only),
                               (self.unavailable, original.include_unavailable)):
            control.SetValue(value)
            layout.Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.warning = wx.StaticText(self, label=smart_list.warning if smart_list else "",
                                     style=wx.ST_ELLIPSIZE_END)
        self.warning.SetToolTip(self.warning.GetLabel())
        layout.Add(self.warning, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.compatibility_note = wx.StaticText(
            self, label="首次保存前会保护备份并升级管理格式，之后需 0.8 或更新兼容版本；"
                        "旧版不能打开升级后的库。\n删除智能列表不会删除文档。")
        self.compatibility_note.Wrap(self.FromDIP(610))
        layout.Add(self.compatibility_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        layout.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                   wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizer(layout)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.Fit()
        self.CentreOnParent()

    def payload(self):
        # Pure shared validation only; the service/store revalidate references
        # under their lock. No dropped references, filesystem reads, or SQL here.
        from notes.domain.smart_lists import query_payload, validate_name, validate_query
        query = validate_query({
            "text": self.text.GetValue(),
            "mode": ("all", "name", "content")[self.mode.GetSelection()],
            "library_id": self.library_ids[self.library.GetSelection()],
            "folder": self.folder.GetValue(),
            "tag_ids": [self.tag_ids[index] for index in self.tags.GetCheckedItems()],
            "favorites_only": self.favorites.GetValue(),
            "recent_only": self.recent.GetValue(),
            "include_unavailable": self.unavailable.GetValue(),
        })
        result = {"name": validate_name(self.name.GetValue()), "query": query_payload(query)}
        if self.smart_list:
            result["id"] = self.smart_list.id
        return result

    def _accept(self, event=None):
        try:
            self.payload()
        except Exception as error:
            message = getattr(error, "message", str(error))
            self.warning.SetLabel(message)
            self.warning.SetToolTip(message)
            return
        if self.IsModal():
            self.EndModal(wx.ID_OK)
        else:
            self.SetReturnCode(wx.ID_OK)
            self.Hide()
