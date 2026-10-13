"""Hidden template form tests; callbacks never write user files or launch editors."""
from concurrent.futures import Future
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden template form tests")


@pytest.fixture
def gui():
    import wx
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    windows = []

    def pump(predicate=lambda: True, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail("Template GUI timed out")
    yield windows, pump
    for window in reversed(windows):
        if window:
            window.Destroy()
    pump()
    del activator
    app.Destroy()


def catalog():
    from notes.domain.models import TemplateInfo
    return (TemplateInfo("blank", "空白文档", "空白文本", ".txt"),
            TemplateInfo("meeting", "会议记录", "会议标题与行动项", ".md"),
            TemplateInfo("daily", "日记", "日期和当日记录", ".md"),
            TemplateInfo("troubleshooting", "问题排查", "现象与处理过程", ".md"))


class Requests:
    def __init__(self):
        self.catalog = []
        self.previews = []

    def list_templates(self, ready, failed):
        self.catalog.append((ready, failed))

    def preview(self, template_id, filename, ready, failed):
        self.previews.append((template_id, filename, ready, failed))


@pytest.fixture
def form(gui):
    from notes.domain.models import Library
    from notes.ui.dialogs import NewDocumentDialog
    windows, pump = gui
    requests = Requests()
    dialog = NewDocumentDialog(None, [Library("lib", "文档库", "C:/test")],
                               request_templates=requests.list_templates,
                               request_preview=requests.preview)
    windows.append(dialog)
    requests.catalog[0][0](catalog())
    return dialog, requests, pump


def choose(dialog, identifier):
    dialog.template.SetSelection(next(index for index, item in enumerate(dialog._templates)
                                       if item.id == identifier))
    dialog._template_changed()
    dialog._load_preview()


def answer(request, text="预览内容"):
    from notes.domain.models import TemplatePreview
    request[2](TemplatePreview(request[0], "标题", text, "2026-09-16T12:00:00+08:00"))


def test_default_blank_legacy_payload_and_readonly_preview(gui):
    from notes.domain.models import Library
    from notes.ui.dialogs import NewDocumentDialog
    windows, _ = gui
    dialog = NewDocumentDialog(None, [Library("lib", "库", "C:/test")])
    windows.append(dialog)
    assert not dialog.IsShown()
    assert dialog.payload() == {"library_id": "lib", "relative_dir": "", "filename": "未命名.txt"}
    assert dialog.template.GetStringSelection() == "空白文档"
    assert not dialog.template_preview.IsEditable()
    assert "日期/时间以实际创建时为准" in dialog.preview_note.GetLabel()


def test_provided_blank_preview_must_succeed_before_ok(form):
    import wx
    from notes.domain.models import NotesError
    dialog, requests, _ = form
    assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
    with pytest.raises(NotesError):
        dialog.payload()
    dialog._load_preview()
    assert requests.previews[-1][:2] == ("blank", "未命名.txt")
    answer(requests.previews[-1], "")
    assert dialog.FindWindow(wx.ID_OK).IsEnabled()
    assert dialog.template_preview.GetValue() == ""
    assert "template_id" not in dialog.payload()


def test_preview_payload_is_id_only_and_template_does_not_write_existing_files(form, tmp_path):
    dialog, requests, _ = form
    existing = tmp_path / "user-owned.txt"
    existing.write_bytes(b"must remain unchanged")
    before = existing.read_bytes()
    choose(dialog, "meeting")
    assert dialog.filename.GetValue() == "会议记录.md"
    answer(requests.previews[-1], "# 会议记录\n\n行动项")
    payload = dialog.payload()
    assert payload == {"library_id": "lib", "relative_dir": "", "filename": "会议记录.md",
                       "template_id": "meeting"}
    assert not {"text", "preview", "rendered_at", "title"} & payload.keys()
    assert existing.read_bytes() == before
    assert not (tmp_path / "会议记录.md").exists()


def test_template_selection_never_overwrites_user_edited_filename(form):
    dialog, requests, _ = form
    dialog.filename.SetValue("我的自定义记录.txt")
    choose(dialog, "daily")
    assert requests.previews[-1][:2] == ("daily", "我的自定义记录.txt")
    answer(requests.previews[-1])
    assert dialog.payload()["filename"] == "我的自定义记录.txt"
    choose(dialog, "troubleshooting")
    assert dialog.filename.GetValue() == "我的自定义记录.txt"


def test_template_suggestion_respects_txt_only_library(gui):
    from notes.domain.models import Library
    from notes.ui.dialogs import NewDocumentDialog
    windows, _ = gui
    requests = Requests()
    dialog = NewDocumentDialog(None, [Library("lib", "TXT库", "C:/test", extensions=(".txt",))],
                               request_templates=requests.list_templates,
                               request_preview=requests.preview)
    windows.append(dialog)
    requests.catalog[0][0](catalog())
    choose(dialog, "meeting")
    assert dialog.filename.GetValue() == "会议记录.txt"
    answer(requests.previews[-1], "# Markdown 内容仍可保存为 TXT")
    assert dialog.payload()["template_id"] == "meeting"


def test_late_preview_and_error_cannot_replace_current_selection(form):
    from notes.domain.models import NotesError
    dialog, requests, _ = form
    choose(dialog, "meeting")
    stale = requests.previews[-1]
    choose(dialog, "daily")
    current = requests.previews[-1]
    answer(current, "日记的新预览")
    answer(stale, "不允许覆盖的新旧预览")
    stale[3](NotesError("late", "旧错误"))
    assert dialog.template_preview.GetValue() == "日记的新预览"
    assert dialog.payload()["template_id"] == "daily"


def test_destroyed_form_ignores_late_template_callbacks(form):
    from notes.domain.models import NotesError
    dialog, requests, pump = form
    choose(dialog, "meeting")
    pending = requests.previews[-1]
    dialog.Destroy()
    pump(lambda: not dialog)
    answer(pending, "不得写入已销毁的控件")
    pending[3](NotesError("late", "旧错误"))
    requests.catalog[0][0](catalog())
    requests.catalog[0][1](NotesError("late", "旧列表错误"))


@pytest.mark.parametrize("name", ["../escape.txt", "CON.txt", "bad:name.txt", "bad.txt ", "bad.exe", ""])
def test_invalid_filename_does_not_request_preview_or_create(form, name):
    import wx
    from notes.domain.models import NotesError
    dialog, requests, _ = form
    count = len(requests.previews)
    dialog.filename.SetValue(name)
    dialog._load_preview()
    dialog._accept()
    assert len(requests.previews) == count
    assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
    with pytest.raises(NotesError):
        dialog.payload()


def test_unknown_template_and_backend_error_do_not_enable_creation(form):
    import wx
    from notes.domain.models import NotesError, TemplateInfo
    dialog, requests, _ = form
    choose(dialog, "meeting")
    requests.previews[-1][3](NotesError("unknown_template", "未知模板"))
    assert "未知模板" in dialog.validation.GetLabel()
    assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
    dialog._templates = (TemplateInfo("arbitrary-script", "非法", "不应执行"),)
    dialog.template.SetItems(["非法"])
    dialog.template.SetSelection(0)
    dialog._template_changed()
    dialog._load_preview()
    assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
    with pytest.raises(NotesError):
        dialog.payload()


def test_filename_changes_debounce_preview_and_invalidate_previous_confirmation(form):
    import wx
    dialog, requests, pump = form
    choose(dialog, "meeting")
    answer(requests.previews[-1])
    previous_count = len(requests.previews)
    dialog.filename.SetValue("新标题.md")
    assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
    assert dialog.template_preview.GetValue() == ""
    assert len(requests.previews) == previous_count
    pump(lambda: len(requests.previews) == previous_count + 1)
    assert requests.previews[-1][1] == "新标题.md"
    answer(requests.previews[-1], "# 新标题")
    assert dialog.FindWindow(wx.ID_OK).IsEnabled()


class FakeService:
    def __init__(self):
        self.requests = []

    def subscribe(self, callback):
        return lambda: None

    def submit(self, command, payload, success, error):
        self.requests.append((command, payload, success, error))
        return Future()

    def close(self):
        pass


def test_partial_creation_refreshes_without_editor_open_retry_or_delete(gui, monkeypatch):
    from notes.domain.models import NotesError
    from notes.ui.main_frame import MainFrame
    windows, _ = gui
    service = FakeService()
    frame = MainFrame(service)
    windows.append(frame)
    messages = []
    monkeypatch.setattr(frame, "_error", lambda error: messages.append(str(error)))
    opened = []
    monkeypatch.setattr(frame, "_open", lambda *args, **kwargs: opened.append(args))
    payload = {"library_id": "lib", "relative_dir": "", "filename": "会议.md", "template_id": "meeting"}
    queries_before = sum(item[0] == "query" for item in service.requests)
    frame._create_new_document(payload)
    request = next(item for item in reversed(service.requests) if item[0] == "create_document")
    request[3](NotesError("created_partial", "写入新文件中途失败，文件已保留"))
    assert opened == []
    assert len([item for item in service.requests if item[0] == "create_document"]) == 1
    assert sum(item[0] == "query" for item in service.requests) == queries_before + 1
    assert not any(item[0] == "scan" for item in service.requests)
    assert not any(item[0] in {"open_document", "delete_document"} for item in service.requests)
    assert "不会自动打开、重复创建或删除" in messages[0]


def test_real_template_form_creates_from_id_at_create_time_not_preview_text(gui, tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.dialogs import NewDocumentDialog
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    root = tmp_path / "documents"
    (root / "sub").mkdir(parents=True)
    before = datetime(2026, 9, 16, 12).astimezone()
    clock = [before.timestamp()]
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter,
                                 monitoring=False, wall_clock=lambda: clock[0])
    frame = dialog = None
    opened = []
    errors = []
    try:
        lib = service.call("add_library", {"root_path": str(root), "extensions": (".txt",)})
        frame = MainFrame(service)
        windows.append(frame)
        monkeypatch.setattr(frame, "_open",
                            lambda document, created=False: opened.append((document, created)))
        monkeypatch.setattr(frame, "_error", lambda error: errors.append(str(error)))
        dialog = NewDocumentDialog(
            frame, [lib], folder="sub",
            request_templates=lambda success, error: service.submit(
                "list_templates", {}, success, error),
            request_preview=lambda template_id, filename, success, error: service.submit(
                "preview_template", {"template_id": template_id, "filename": filename}, success, error))
        windows.append(dialog)
        pump(lambda: dialog._templates_loaded)
        dialog.filename.SetValue("固定标题.txt")
        choose(dialog, "meeting")
        pump(lambda: dialog._preview_valid)
        displayed = dialog.template_preview.GetValue()
        assert "# 固定标题" in displayed
        assert before.date().isoformat() in displayed
        assert not dialog.template_preview.IsEditable()
        assert "日期/时间以实际创建时为准" in dialog.preview_note.GetLabel()
        assert not (root / "sub" / "固定标题.txt").exists()
        # Even a programmatic alteration of the display cannot enter the create payload.
        dialog.template_preview.SetValue("仅改变显示，不可作为写入内容")
        after = before + timedelta(days=1)
        clock[0] = after.timestamp()
        payload = dialog.payload()
        assert "text" not in payload and payload["template_id"] == "meeting"
        frame._create_new_document(payload)
        pump(lambda: bool(opened) or bool(errors), timeout=10)
        assert not errors
        assert len(opened) == 1 and opened[0][1] is True
        raw = (root / "sub" / "固定标题.txt").read_bytes()
        body = raw.decode("utf-8")
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert "# 固定标题" in body
        assert after.date().isoformat() in body
        assert before.date().isoformat() not in body
        assert "仅改变显示" not in body
        assert service.call("query", {"mode": "content", "text": "决定与待办"}).total == 1
    finally:
        if dialog:
            dialog.Destroy()
        if frame:
            frame.Destroy()
        service.close()


def test_leading_space_directory_is_preserved_in_payload_and_actual_creation(gui, tmp_path):
    from notes.application.service import ApplicationService
    from notes.ui.dialogs import NewDocumentDialog
    windows, _ = gui
    root = tmp_path / "documents"
    (root / " folder").mkdir(parents=True)
    (root / "folder").mkdir()
    service = ApplicationService(tmp_path / "metadata", monitoring=False)
    dialog = None
    try:
        library = service.call("add_library", {"root_path": str(root)})
        dialog = NewDocumentDialog(None, [library], folder=" folder")
        windows.append(dialog)
        dialog.filename.SetValue("target.txt")
        payload = dialog.payload()
        assert payload["relative_dir"] == " folder"
        assert dialog.directory.GetValue() == " folder"
        document = service.call("create_document", payload)
        assert document.relative_path == " folder/target.txt"
        assert (root / " folder" / "target.txt").is_file()
        assert not (root / "folder" / "target.txt").exists()
        assert (root / " folder" / "target.txt").read_bytes() == b""
    finally:
        if dialog:
            dialog.Destroy()
        service.close()
