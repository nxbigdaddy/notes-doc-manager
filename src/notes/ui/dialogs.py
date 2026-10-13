"""Small modal forms. Forms select values only; never perform persistence IO."""
from pathlib import Path
import ntpath

import wx

from notes.domain.models import NotesError, TemplateInfo
from notes.ui.formatting import parse_rules


class FormDialog(wx.Dialog):
    def __init__(self, parent, title, size=(540, 350)):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.body = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.body)
        self.SetMinSize(self.FromDIP(size))

    def field(self, label, value=""):
        self.body.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        control = wx.TextCtrl(self, value=value)
        self.body.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        return control

    def note(self, text):
        control = wx.StaticText(self, label=text)
        control.Wrap(self.FromDIP(490))
        self.body.Add(control, 0, wx.EXPAND | wx.ALL, 14)
        return control

    def finish(self):
        self.body.AddStretchSpacer()
        self.body.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0,
                      wx.ALIGN_RIGHT | wx.ALL, 14)
        self.Layout()
        self.Fit()
        self.CentreOnParent()


class LibraryDialog(FormDialog):
    def __init__(self, parent, library=None):
        super().__init__(parent, "文档库规则" if library else "添加文档库", (540, 410))
        self.library = library
        self.root = self.field("文档库目录", library.root_path if library else "")
        browse = wx.Button(self, label="选择文件夹…")
        self.body.Add(browse, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        browse.Bind(wx.EVT_BUTTON, self._browse)
        self.name = self.field("显示名称（留空使用目录名）", library.name if library else "")
        self.extensions = self.field("扩展名白名单，以逗号分隔", ", ".join(
            library.extensions if library else (".txt", ".md")))
        self.excluded = self.field("排除目录名称，以逗号分隔", ", ".join(
            library.excluded_dirs if library else (".git", ".venv", "__pycache__")))
        if library:
            self.root.SetEditable(False)
            self.name.SetEditable(False)
            browse.Disable()
        self.note("只登记文件，不移动或改写正文。规则更改会触发重新扫描。")
        self.finish()

    def _browse(self, event):
        with wx.DirDialog(self, "选择文档库根目录") as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.root.SetValue(dialog.GetPath())

    def payload(self):
        rules = {"extensions": parse_rules(self.extensions.GetValue()),
                 "excluded_dirs": parse_rules(self.excluded.GetValue())}
        if self.library:
            return {"library_id": self.library.id, **rules}
        return {"root_path": self.root.GetValue().strip(),
                "name": self.name.GetValue().strip(), **rules}


class NewDocumentDialog(FormDialog):
    def __init__(self, parent, libraries, selected_library=None, folder="", request_folders=None,
                 request_templates=None, request_preview=None):
        super().__init__(parent, "新建文档", (640, 700))
        self.libraries = list(libraries)
        self._alive = True
        self._folder_request = 0
        self._request_folders = request_folders
        self._request_templates = request_templates
        self._request_preview = request_preview
        self._templates = (TemplateInfo("blank", "空白文档", "创建空白文本文件。", ".txt"),)
        self._templates_loaded = request_templates is None
        self._preview_generation = 0
        self._preview_valid = False
        self._filename_user_edited = False
        self.body.Add(wx.StaticText(self, label="文档库"), 0, wx.ALL, 14)
        self.library = wx.Choice(self, choices=[lib.name for lib in self.libraries])
        self.body.Add(self.library, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self.library.SetSelection(next((i for i, lib in enumerate(self.libraries)
                                       if lib.id == selected_library), 0))
        self.body.Add(wx.StaticText(self, label="库内现有文件夹（留空为根目录）"), 0,
                      wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.directory = wx.ComboBox(self, value=folder, choices=[""])
        self.body.Add(self.directory, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        lib = self.libraries[self.library.GetSelection()]
        initial_ext = ".txt" if ".txt" in lib.extensions else (
            lib.extensions[0] if lib.extensions else ".txt")
        self.filename = self.field("文件名（包含当前库允许的扩展名）", "未命名" + initial_ext)
        self.target = self.note("")
        self.body.Add(wx.StaticText(self, label="内置模板（纯文本，不执行脚本）"), 0,
                      wx.LEFT | wx.RIGHT, 14)
        self.template = wx.Choice(self, choices=["空白文档"])
        self.template.SetSelection(0)
        self.body.Add(self.template, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.template_description = self.note("创建空白文本文件。")
        self.template_preview = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.template_preview.SetMinSize(self.FromDIP((540, 140)))
        self.body.Add(self.template_preview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self.preview_note = self.note(
            "只读预览；日期/时间以实际创建时为准。\n"
            "文件采用 UTF-8 创建，同名文件不会覆盖。创建后请求 Notepad3 打开。")
        self.validation = wx.StaticText(self, label="", style=wx.ST_ELLIPSIZE_END)
        self.body.Add(self.validation, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self._preview_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._load_preview(), self._preview_timer)
        self.directory.Bind(wx.EVT_TEXT, self._target)
        self.filename.Bind(wx.EVT_TEXT, self._filename_changed)
        self.template.Bind(wx.EVT_CHOICE, self._template_changed)
        self.library.Bind(wx.EVT_CHOICE, self._library_changed)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed, id=self.GetId())
        self._target()
        self._load_folders()
        self.finish()
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self._load_templates()
        self._schedule_preview()
        self.filename.SetFocus()
        self.filename.SetSelection(0, len("未命名"))

    def _destroyed(self, event):
        self._alive = False
        self._preview_generation += 1
        self._preview_timer.Stop()
        event.Skip()

    def _library_changed(self, event):
        self.directory.SetValue("")
        if not self._filename_user_edited:
            self._suggest_filename()
        self._target()
        self._load_folders()
        self._schedule_preview()

    def _load_folders(self):
        if not self._request_folders:
            return
        self._folder_request += 1
        generation = self._folder_request
        lib = self.libraries[self.library.GetSelection()]

        def ready(folders):
            if not self._alive or not self or not self.directory or generation != self._folder_request:
                return
            value = self.directory.GetValue()
            self.directory.SetItems(list(folders))
            self.directory.SetValue(value)
        self._request_folders(lib.id, ready)

    def Destroy(self):
        self._alive = False
        self._preview_generation += 1
        self._preview_timer.Stop()
        return super().Destroy()

    def _current_template(self):
        index = self.template.GetSelection()
        return self._templates[index] if 0 <= index < len(self._templates) else None

    def _filename_error(self):
        filename = self.filename.GetValue()
        invalid = '<>:"/\\|?*'
        if (not filename or filename in {".", ".."} or filename.endswith((" ", "."))
                or any(ord(char) < 32 or char in invalid for char in filename)):
            return "请输入合法的单个文件名，不能包含路径或 Windows 非法字符。"
        stem = filename.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or (
                len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"):
            return "文件名不能使用 Windows 保留设备名称。"
        lib = self.libraries[self.library.GetSelection()]
        if ntpath.splitext(filename)[1].lower() not in {ext.lower() for ext in lib.extensions}:
            return "当前文档库仅允许：" + "、".join(lib.extensions)
        return ""

    def _can_create(self):
        item = self._current_template()
        return (self._templates_loaded and item is not None
                and item.id in {"blank", "meeting", "daily", "troubleshooting"}
                and not self._filename_error()
                and (self._request_preview is None and item.id == "blank" or self._preview_valid))

    def _update_create(self):
        button = self.FindWindow(wx.ID_OK)
        if button:
            button.Enable(self._can_create())

    def _set_validation(self, message):
        self.validation.SetLabel(message)
        self.validation.SetToolTip(message)

    def _filename_changed(self, event):
        self._filename_user_edited = True
        self._target()
        self._schedule_preview()

    def _suggest_filename(self):
        item = self._current_template()
        if item is None or self._filename_user_edited:
            return
        lib = self.libraries[self.library.GetSelection()]
        extension = item.suggested_extension if item.suggested_extension in lib.extensions else (
            ".txt" if ".txt" in lib.extensions else lib.extensions[0] if lib.extensions else ".txt")
        title = "未命名" if item.id == "blank" else item.name
        self.filename.ChangeValue(title + extension)

    def _template_changed(self, event=None):
        item = self._current_template()
        self.template_description.SetLabel(item.description if item else "未知模板")
        self._suggest_filename()
        self._target()
        self._schedule_preview()

    def _load_templates(self):
        if self._request_templates is None:
            return
        self._set_validation("正在读取内置模板…")

        def ready(items):
            if not self._alive or not self:
                return
            self._templates = tuple(items)
            self._templates_loaded = True
            self.template.SetItems([item.name for item in self._templates])
            blank = next((index for index, item in enumerate(self._templates)
                          if item.id == "blank"), wx.NOT_FOUND)
            self.template.SetSelection(blank)
            self._schedule_preview()

        def failed(error):
            if self._alive and self:
                self._templates_loaded = False
                self._set_validation("模板列表未加载：" + getattr(error, "message", str(error)))
                self._update_create()
        try:
            self._request_templates(ready, failed)
        except Exception as error:
            failed(error)

    def _schedule_preview(self):
        if not self._alive:
            return
        self._preview_generation += 1
        self._preview_valid = False
        self.template_preview.SetValue("")
        self._preview_timer.Stop()
        error = self._filename_error()
        if error:
            self._set_validation(error)
        elif not self._templates_loaded:
            self._set_validation("等待内置模板列表加载完成。")
        else:
            self._set_validation("正在准备只读预览…" if self._request_preview else "空白文档")
            self._preview_timer.StartOnce(200)
        self._update_create()

    def _load_preview(self):
        if not self._alive or not self or not self._templates_loaded:
            return
        self._preview_timer.Stop()
        generation = self._preview_generation
        item = self._current_template()
        if item is None or item.id not in {"blank", "meeting", "daily", "troubleshooting"}:
            self._set_validation("未知模板，不能创建文档。")
            self._update_create()
            return
        if self._filename_error():
            self._update_create()
            return
        if self._request_preview is None:
            self._preview_valid = item.id == "blank"
            self._update_create()
            return
        filename = self.filename.GetValue()

        def ready(preview):
            if not self._alive or not self or generation != self._preview_generation:
                return
            if preview.template_id != item.id:
                self._preview_valid = False
                self._set_validation("预览模板与当前选择不一致，请重新选择。")
            else:
                self.template_preview.SetValue(preview.text)
                self._preview_valid = True
                self._set_validation("预览已更新；日期/时间以实际创建时为准。")
            self._update_create()

        def failed(error):
            if self._alive and self and generation == self._preview_generation:
                self._preview_valid = False
                self.template_preview.SetValue("")
                self._set_validation("预览未完成：" + getattr(error, "message", str(error)))
                self._update_create()
        try:
            self._request_preview(item.id, filename, ready, failed)
        except Exception as error:
            failed(error)

    def _accept(self, event=None):
        if not self._can_create():
            self._set_validation(self._filename_error() or "请先选择并成功预览有效模板。")
            return
        if self.IsModal():
            self.EndModal(wx.ID_OK)
        else:
            self.SetReturnCode(wx.ID_OK)
            self.Hide()

    def _target(self, event=None):
        lib = self.libraries[self.library.GetSelection()]
        self.target.SetLabel("目标路径：" + str(Path(lib.root_path) /
                                                self.directory.GetValue() /
                                                self.filename.GetValue()))
        self.target.Wrap(self.FromDIP(490))
        self.Layout()

    def payload(self):
        if not self._can_create():
            raise NotesError("invalid_document", self._filename_error() or "模板尚未通过预览校验。")
        payload = {"library_id": self.libraries[self.library.GetSelection()].id,
                   "relative_dir": self.directory.GetValue(),
                   "filename": self.filename.GetValue()}
        item = self._current_template()
        if item.id != "blank":
            payload["template_id"] = item.id
        return payload
