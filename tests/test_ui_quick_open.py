"""Hidden quick-open tests; native global hotkeys and editor launches are mocked."""
from concurrent.futures import Future
from dataclasses import replace
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden quick-open GUI tests")


class FakeService:
    def __init__(self):
        self.requests = []
        self.listeners = []

    def subscribe(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def submit(self, command, payload, success, error):
        future = Future()
        self.requests.append((command, payload, success, error, future))
        return future

    def close(self):
        pass


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
        pytest.fail("Quick-open GUI timed out")
    yield windows, pump
    for window in reversed(windows):
        if window:
            window.Destroy()
    pump()
    del activator
    app.Destroy()


@pytest.fixture
def quick(gui):
    from notes.ui.quick_open import QuickOpenDialog
    windows, pump = gui
    service = FakeService()
    dialog = QuickOpenDialog(None, service)
    windows.append(dialog)
    return dialog, service, pump


def result(*identifiers):
    from notes.domain.models import Document, SearchHit, SearchResult
    return SearchResult(items=tuple(SearchHit(Document(identifier, "library",
                                                      f"folder/{identifier}.txt",
                                                      f"{identifier}.txt"))
                                    for identifier in identifiers), total=len(identifiers))


def latest(service, command):
    return next(item for item in reversed(service.requests) if item[0] == command)


def test_recent_query_uses_isolated_channel_and_exact_20_name_scope(quick):
    dialog, service, _ = quick
    assert not dialog.IsShown()
    request = latest(service, "query")
    assert request[1]["channel"] == "quick"
    assert request[1]["mode"] == "name"
    assert request[1]["page_size"] == 20
    assert request[1]["recent_only"] is True
    assert request[1]["library_id"] is None
    assert request[1]["tag_ids"] == ()
    assert request[1]["include_unavailable"] is False
    assert not any(item[0] == "preview" for item in service.requests)


def test_stale_query_does_not_replace_latest_results(quick):
    dialog, service, _ = quick
    old = latest(service, "query")
    dialog.search.SetValue("new")
    dialog._query()
    new = latest(service, "query")
    assert new[1]["recent_only"] is False
    new[2](result("new"))
    old[2](result("old"))
    assert dialog.hits[0].document.id == "new"
    assert dialog.list.GetItemText(0) == "new.txt"


def test_whitespace_means_recent_and_partial_zero_is_not_reported_as_no_match(quick):
    from notes.domain.models import SearchResult
    dialog, service, _ = quick
    dialog.search.SetValue(" \t ")
    dialog._query()
    request = latest(service, "query")
    assert request[1]["recent_only"] is True
    assert request[1]["text"] == ""
    request[2](SearchResult(complete=False, warning="查询达到时间限制"))
    assert "未完整完成" in dialog.status.GetLabel()
    assert "时间限制" in dialog.status.GetLabel()
    assert "暂无最近" not in dialog.status.GetLabel()


def test_debounce_clears_old_open_target_and_keyboard_navigation(quick):
    dialog, service, pump = quick
    latest(service, "query")[2](result("one", "two", "three"))
    dialog._move_selection(1)
    assert dialog.list.GetFirstSelected() == 1
    dialog._move_selection(-1)
    assert dialog.list.GetFirstSelected() == 0
    count = len(service.requests)
    dialog.search.SetValue("hello")
    dialog._activate()
    assert dialog._query_pending
    assert not dialog.hits
    assert len(service.requests) == count
    pump(lambda: len(service.requests) > count)
    assert latest(service, "query")[1]["text"] == "hello"


def test_duplicate_enter_opens_once_and_success_closes(quick):
    dialog, service, pump = quick
    latest(service, "query")[2](result("one"))
    dialog._activate()
    dialog._activate()
    opened = [item for item in service.requests if item[0] == "open_document"]
    assert len(opened) == 1
    assert opened[0][1] == {"document_id": "one"}
    opened[0][2](None)
    pump()
    assert dialog._closed
    assert dialog.opened_document_id == "one"
    assert latest(service, "cancel_query")[1] == {"channel": "quick"}


def test_open_failure_inline_and_late_callbacks_after_close_are_ignored(quick):
    from notes.domain.models import NotesError
    dialog, service, pump = quick
    latest(service, "query")[2](result("one"))
    dialog._activate()
    request = latest(service, "open_document")
    request[3](NotesError("editor_not_configured", "请先配置 Notepad3"))
    assert "请先配置" in dialog.status.GetLabel()
    assert not dialog._opening and dialog.open_button.IsEnabled()
    dialog.Destroy()
    pump()
    request[2](None)
    request[3](NotesError("late", "不得访问已销毁控件"))
    assert dialog.opened_document_id is None
    assert service.listeners == []


def test_close_cancels_queued_editor_open_before_it_can_execute(quick):
    dialog, service, pump = quick
    latest(service, "query")[2](result("one"))
    dialog._activate()
    request = latest(service, "open_document")
    assert not request[4].running() and not request[4].done()
    assert "可能无法撤回" in dialog.status.GetLabel()
    dialog.Destroy()
    pump()
    assert request[4].cancelled()
    assert dialog.open_request_cancelled is True
    request[3](RuntimeError("queued cancellation callback after close"))
    assert dialog.opened_document_id is None


def test_close_does_not_claim_running_editor_request_was_cancelled(quick):
    dialog, service, pump = quick
    latest(service, "query")[2](result("one"))
    dialog._activate()
    request = latest(service, "open_document")
    assert request[4].set_running_or_notify_cancel()
    dialog.Destroy()
    pump()
    assert not request[4].cancelled()
    assert dialog.open_request_cancelled is False
    # Completion of an already-started action is ignored after UI close; it is
    # not a reason to kill the external editor or resurrect the dialog.
    request[4].set_result(None)
    request[2](None)
    assert dialog.opened_document_id is None


class FakeHotkey:
    instances = []
    failure = None
    support = True

    def __init__(self, frame, callback):
        self.enabled = False
        self.callback = callback
        self.calls = []
        self.closing = False
        self.instances.append(self)

    def enable(self):
        self.calls.append("enable")
        if self.failure:
            raise self.failure
        if not self.support:
            return False
        self.enabled = True
        return True

    def disable(self):
        self.calls.append("disable")
        self.enabled = False
        return True

    def close(self):
        self.calls.append("close")
        self.closing = True
        self.enabled = False
        return True


@pytest.fixture
def main(gui, monkeypatch):
    from notes.ui import hotkeys
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    FakeHotkey.instances = []
    FakeHotkey.failure = None
    FakeHotkey.support = True
    monkeypatch.setattr(hotkeys, "GlobalQuickOpenHotkey", FakeHotkey)
    service = FakeService()
    frame = MainFrame(service)
    windows.append(frame)
    return frame, service, pump


def test_global_hotkey_default_never_registers(main):
    from notes.domain.models import AppState
    frame, service, _ = main
    frame._on_state(AppState())
    assert not FakeHotkey.instances
    assert not frame.hotkey_menu.IsChecked()


def test_registration_precedes_persistence_and_failure_rolls_back(main):
    from notes.domain.models import NotesError
    frame, service, _ = main
    frame.hotkey_menu.Check(True)
    frame._toggle_quick_hotkey()
    adapter = FakeHotkey.instances[0]
    assert adapter.calls == ["enable"]
    request = latest(service, "set_quick_open_hotkey")
    assert request[1] == {"enabled": True}
    request[3](NotesError("disk_full", "无法保存设置"))
    assert adapter.calls == ["enable", "disable"]
    assert not adapter.enabled
    assert not frame.hotkey_menu.IsChecked()
    assert "已还原" in frame.GetStatusBar().GetStatusText(0)


def test_conflicting_saved_hotkey_does_not_retry_each_state_update(main):
    from notes.domain.models import AppState, NotesError
    frame, service, _ = main
    FakeHotkey.failure = NotesError("hotkey_conflict", "快捷键已被占用")
    state = AppState(quick_open_hotkey_enabled=True)
    frame._on_state(state)
    for index in range(10):
        frame._on_state(replace(state, status=f"状态{index}"))
    assert FakeHotkey.instances[0].calls == ["enable"]
    assert "被占用" in frame.GetStatusBar().GetStatusText(0)
    assert not any(item[0] == "set_quick_open_hotkey" for item in service.requests)


def test_failed_disable_persistence_reenables_and_close_unregisters_once(main):
    from notes.domain.models import AppState, NotesError
    frame, service, _ = main
    frame._on_state(AppState(quick_open_hotkey_enabled=True))
    adapter = FakeHotkey.instances[0]
    frame.hotkey_menu.Check(False)
    frame._toggle_quick_hotkey()
    latest(service, "set_quick_open_hotkey")[3](NotesError("failed", "写设置失败"))
    assert adapter.calls == ["enable", "disable", "enable"]
    assert frame.hotkey_menu.IsChecked()
    frame._close_shortcuts()
    frame._close_shortcuts()
    assert adapter.calls.count("close") == 1


def test_main_reuses_existing_quick_dialog_without_showing_windows(main, monkeypatch):
    from notes.ui.quick_open import QuickOpenDialog
    frame, _, _ = main
    focused = []
    monkeypatch.setattr(QuickOpenDialog, "Show", lambda self, *args: True)
    monkeypatch.setattr(QuickOpenDialog, "focus_search", lambda self: focused.append(self))
    frame._open_quick()
    first = frame._quick_dialog
    frame._open_quick()
    assert frame._quick_dialog is first
    assert focused == [first, first]
    assert not first.IsShown()
    frame._close_shortcuts()
    assert first._closed


def test_real_service_quick_search_and_mocked_editor_open(gui, tmp_path, monkeypatch):
    import wx
    from notes.application.service import ApplicationService
    from notes.infrastructure import editor
    from notes.ui.quick_open import QuickOpenDialog
    windows, pump = gui
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "中文速开.txt").write_text("正文不会在快速打开窗口预览", encoding="utf-8")
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter, monitoring=False)
    opens = []
    monkeypatch.setattr(editor, "open_document", lambda editor_path, path: opens.append(str(path)))
    try:
        lib = service.call("add_library", {"root_path": str(docs), "name": "临时测试库"})
        service.call("scan", {"library_id": lib.id})
        service.store.set_setting("editor_path", "C:/mock/Notepad3.exe")
        dialog = QuickOpenDialog(None, service)
        windows.append(dialog)
        dialog.search.SetValue("中文")
        dialog._query()
        # Query results and AppState travel on independent async lanes. A first
        # hit can arrive before library names; wait for the displayed state too.
        pump(lambda: len(dialog.hits) == 1 and not dialog._query_pending
             and any(item.id == lib.id for item in dialog.state.libraries)
             and "临时测试库" in dialog.list.GetItemText(0, 1))
        assert dialog.hits[0].document.file_name == "中文速开.txt"
        assert "临时测试库" in dialog.list.GetItemText(0, 1)
        assert not dialog.IsShown()
        dialog._activate()
        pump(lambda: dialog._closed)
        assert opens == [str(docs / "中文速开.txt")]
        assert service.call("query", {"channel": "quick", "recent_only": True}).total == 1
    finally:
        for window in windows:
            if isinstance(window, QuickOpenDialog) and window and not window._closed:
                window.Destroy()
        service.close()


def test_real_main_and_quick_searches_do_not_cancel_or_inherit_each_other(gui, tmp_path):
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame
    from notes.ui.quick_open import QuickOpenDialog
    windows, pump = gui
    root = tmp_path / "documents"
    root.mkdir()
    (root / "主列表.txt").write_text("main", encoding="utf-8")
    (root / "快速打开.txt").write_text("quick", encoding="utf-8")
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter, monitoring=False)
    main = quick = None
    errors = []
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        main = MainFrame(service)
        main._error = lambda error: errors.append(str(error))
        windows.append(main)
        main._filters = {"favorites_only": True}
        main._query()
        quick = QuickOpenDialog(main, service)
        windows.append(quick)
        quick.search.SetValue("快速")
        quick._query()
        pump(lambda: len(quick.hits) == 1 and not quick._query_pending
             and "没有符合" in main.empty.GetLabel())
        assert main.list.GetItemCount() == 0  # main's favorite filter is NOT inherited
        main._filters = {}
        main.search.SetValue("主列表")
        main._query()
        quick._query()
        pump(lambda: main.list.GetItemCount() == 1 and len(quick.hits) == 1
             and not quick._query_pending)
        assert main.list.hits[0].document.file_name == "主列表.txt"
        assert quick.hits[0].document.file_name == "快速打开.txt"
        quick.Destroy()
        main._query()
        pump(lambda: main.list.GetItemCount() == 1)
        assert not errors
    finally:
        if quick and not quick._closed:
            quick.Destroy()
        if main:
            main.Destroy()
        service.close()
