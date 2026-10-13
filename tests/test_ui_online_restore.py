"""Two-phase online restore confirmation; hidden windows and generated data only."""
from concurrent.futures import Future
from dataclasses import replace
import os
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden online restore tests")

BACKUP = "a" * 64
CURRENT = "b" * 64


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


class Picker:
    selections = []

    def __init__(self, *args, **kwargs):
        self.path = self.selections.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def ShowModal(self):
        import wx
        return wx.ID_OK if self.path is not None else wx.ID_CANCEL

    def GetPath(self):
        return self.path


def summary(**overrides):
    return {"format_version": 1, "libraries": 1, "documents": 2, "tags": 1,
            "smart_lists": 0, "signature": BACKUP, "current_signature": CURRENT, **overrides}


def latest(service, command):
    return next(item for item in reversed(service.requests) if item[0] == command)


@pytest.fixture
def gui():
    import wx
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    windows = []

    def pump(predicate=lambda: True, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail("Online restore GUI timed out")
    yield windows, pump
    for window in reversed(windows):
        if window:
            window.Destroy()
    pump()
    del activator
    app.Destroy()


@pytest.fixture
def frame(gui, monkeypatch):
    import wx
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    service = FakeService()
    window = MainFrame(service)
    windows.append(window)
    window._filters = {"favorites_only": True}
    window._folders = {"old-library": ("original",)}
    service.requests.clear()
    errors, prompts = [], []
    monkeypatch.setattr(window, "_error", lambda error: errors.append(error))
    Picker.selections = ["C:/fixture/selected.json"]
    monkeypatch.setattr(wx, "FileDialog", Picker)

    def message(text, title, *args, **kwargs):
        prompts.append((text, title))
        return wx.YES if title == "确认恢复管理信息" else wx.OK
    monkeypatch.setattr(wx, "MessageBox", message)
    return window, service, errors, prompts, pump


def start_restore(window, service, inspected=None):
    window._restore()
    latest(service, "inspect_backup")[2](summary() if inspected is None else inspected)
    return latest(service, "restore_backup")


def test_confirm_forwards_both_fingerprints_but_hides_them_and_preserves_navigation_until_success(frame):
    from notes.domain.models import AppState
    window, service, errors, prompts, _ = frame
    request = start_restore(window, service)
    assert request[1] == {"path": "C:/fixture/selected.json", "confirmed": True,
                          "expected_backup": BACKUP, "expected_current": CURRENT}
    assert window._filters == {"favorites_only": True}
    assert window._folders == {"old-library": ("original",)}
    assert len(prompts) == 1 and "智能列表数：0" in prompts[0][0]
    assert BACKUP not in prompts[0][0] and CURRENT not in prompts[0][0]
    assert "signature" not in prompts[0][0]
    request[2](None)
    assert window._filters == {} and window._folders == {}
    latest(service, "state")[2](AppState())
    assert any(item[0] == "query" for item in service.requests)
    assert errors == []


@pytest.mark.parametrize("missing", [
    {"signature": None}, {"current_signature": None}, {"signature": "g" * 64},
    {"signature": "A" * 64}, {"current_signature": ""},
])
def test_missing_or_invalid_fingerprints_never_prompt_or_submit_restore(frame, missing):
    window, service, errors, prompts, _ = frame
    window._restore()
    latest(service, "inspect_backup")[2](summary(**missing))
    assert not any(item[0] == "restore_backup" for item in service.requests)
    assert prompts == []
    assert len(errors) == 1 and "指纹" in str(errors[0])
    assert window._filters == {"favorites_only": True}


def test_second_inspection_invalidates_first_response_and_error(frame):
    from notes.domain.models import NotesError
    window, service, errors, prompts, _ = frame
    Picker.selections = ["C:/fixture/first.json", "C:/fixture/second.json"]
    window._restore()
    first = latest(service, "inspect_backup")
    window._restore()
    second = latest(service, "inspect_backup")
    first[2](summary())
    first[3](NotesError("late", "不得弹出旧检查错误"))
    assert prompts == [] and errors == []
    second[2](summary(signature="c" * 64))
    request = latest(service, "restore_backup")
    assert request[1]["path"] == "C:/fixture/second.json"
    assert request[1]["expected_backup"] == "c" * 64


def test_cancelled_new_picker_invalidates_pending_old_inspection(frame):
    window, service, errors, prompts, _ = frame
    Picker.selections = ["C:/fixture/first.json", None]
    window._restore()
    pending = latest(service, "inspect_backup")
    window._restore()
    pending[2](summary())
    pending[3](RuntimeError("late"))
    assert errors == [] and prompts == []
    assert not any(item[0] == "restore_backup" for item in service.requests)


def test_navigation_intent_changed_during_confirmation_prevents_submission(frame, monkeypatch):
    import wx
    window, service, _, _, _ = frame
    Picker.selections = ["C:/fixture/first.json", None]
    window._restore()
    pending = latest(service, "inspect_backup")

    def superseded(*args, **kwargs):
        window._restore()  # a new picker was opened and cancelled during modal event dispatch
        return wx.YES
    monkeypatch.setattr(wx, "MessageBox", superseded)
    pending[2](summary())
    assert not any(item[0] == "restore_backup" for item in service.requests)


@pytest.mark.parametrize("code", ["backup_changed", "metadata_changed"])
def test_changed_confirmation_input_does_not_retry_or_clear_old_filters(frame, code):
    from notes.domain.models import NotesError
    window, service, errors, _, _ = frame
    request = start_restore(window, service)
    request[3](NotesError(code, "检查后发生变化；未恢复，请重新选择并确认"))
    assert len(errors) == 1 and errors[0].code == code
    assert window._filters == {"favorites_only": True}
    assert window._folders == {"old-library": ("original",)}
    assert [item[0] for item in service.requests] == ["inspect_backup", "restore_backup"]


def test_close_ignores_late_inspection_and_restore_callbacks(frame):
    window, service, errors, prompts, pump = frame
    request = start_restore(window, service)
    window.Destroy()
    pump(lambda: not window)
    request[2](None)
    request[3](RuntimeError("late restore error"))
    latest(service, "inspect_backup")[2](summary())
    assert errors == []
    assert len(prompts) == 1
    assert not any(item[0] == "state" for item in service.requests)


def test_already_committed_restore_still_refreshes_after_a_new_picker_is_cancelled(frame):
    window, service, _, _, _ = frame
    Picker.selections = ["C:/fixture/selected.json", None]
    request = start_restore(window, service)
    window._restore()
    # Cancelling a later selection cannot undo an earlier explicitly confirmed
    # transaction that actually committed. Reflect its real outcome.
    request[2](None)
    assert window._filters == {} and window._folders == {}
    assert any(item[0] == "state" for item in service.requests)


@pytest.mark.parametrize("code", ["restored_index_pending", "restored_followup_failed"])
def test_committed_failure_is_success_with_warning_and_refresh_not_repeat_restore(frame, code):
    from notes.domain.models import AppState, NotesError, SearchQuery, SmartList
    window, service, errors, prompts, _ = frame
    smart = SmartList(str(uuid4()), "恢复前视图", SearchQuery())
    window._on_state(replace(AppState(), smart_lists=(smart,)))
    window._activate_smart_list(smart.id)
    window._filters = {"favorites_only": True}
    window._folders = {"old-library": ("original",)}
    service.requests.clear()
    request = start_restore(window, service)
    request[3](NotesError(code, "管理信息已经提交，后续工作未完成"))
    assert window._active_smart_list_id == smart.id  # no broad fallback
    assert window._filters == {} and window._folders == {}
    assert window.list.GetItemCount() == 0
    assert errors == []
    assert prompts[-1][1] == ("管理信息已恢复，索引待处理" if code == "restored_index_pending" else
                              "管理信息已恢复，后续刷新待处理")
    assert "不要重复恢复" in prompts[-1][0]
    latest(service, "state")[2](AppState())
    assert window._active_smart_list_id == smart.id
    assert window._active_smart_list() is None
    assert window.list.GetItemCount() == 0
    assert sum(item[0] == "restore_backup" for item in service.requests) == 1
    assert sum(item[0] == "inspect_backup" for item in service.requests) == 1
    assert not any(item[0] == "rebuild_index" for item in service.requests)


@pytest.mark.parametrize("change", ["backup", "current", "none", "index", "followup"])
def test_real_service_confirmation_rejects_changed_inputs_or_completes_safely(gui, tmp_path, monkeypatch, change):
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    root = tmp_path / "documents"
    root.mkdir()
    body = root / "untouched.txt"
    body.write_bytes(b"original document body must not change")
    candidate, alternate = tmp_path / "candidate.json", tmp_path / "alternate.json"
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter, monitoring=False)
    window = None
    errors, messages, commands = [], [], []
    try:
        lib = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": lib.id})
        tag = service.call("create_tag", {"name": "INSPECTED_A"})
        service.call("export_backup", {"path": str(candidate)})
        service.call("rename_tag", {"tag_id": tag.id, "name": "UNCONFIRMED_B"})
        service.call("export_backup", {"path": str(alternate)})
        service.call("rename_tag", {"tag_id": tag.id, "name": "LIVE_CURRENT"})
        window = MainFrame(service)
        windows.append(window)
        window._error = lambda error: errors.append(error)
        window._filters = {"favorites_only": True}
        window._folders = {lib.id: ("old-cache",)}
        Picker.selections = [str(candidate)]
        monkeypatch.setattr(wx, "FileDialog", Picker)
        original_submit = service.submit

        def counted(command, *args, **kwargs):
            commands.append(command)
            return original_submit(command, *args, **kwargs)
        monkeypatch.setattr(service, "submit", counted)

        def confirm(message, title, *args, **kwargs):
            messages.append(message)
            if title != "确认恢复管理信息":
                return wx.OK
            if change == "backup":
                assert candidate.resolve().is_relative_to(tmp_path.resolve())
                assert alternate.resolve().is_relative_to(tmp_path.resolve())
                os.replace(alternate, candidate)
            elif change == "current":
                service.call("create_tag", {"name": "ADDED_AFTER_INSPECTION"})
            return wx.YES
        monkeypatch.setattr(wx, "MessageBox", confirm)
        if change == "index":
            def failed_index():
                raise RuntimeError("test-owned postcommit index failure")
            monkeypatch.setattr(service.store, "rebuild_index", failed_index)
        elif change == "followup":
            original_notify = service._notify

            def failed_notify(*args, **kwargs):
                status = args[0] if args else kwargs.get("status")
                if isinstance(status, str) and status.startswith("管理信息已恢复"):
                    raise RuntimeError("test-owned postcommit notification failure")
                return original_notify(*args, **kwargs)
            monkeypatch.setattr(service, "_notify", failed_notify)
        window._restore()
        if change in {"none", "index", "followup"}:
            pump(lambda: window._filters == {} and any(item.name == "INSPECTED_A"
                                                       for item in window.state.tags), timeout=12)
            assert errors == []
            assert [item.name for item in service.store.list_tags()] == ["INSPECTED_A"]
        else:
            pump(lambda: bool(errors))
            assert errors[0].code == ("backup_changed" if change == "backup" else "metadata_changed")
            assert window._filters == {"favorites_only": True}
            assert window._folders == {lib.id: ("old-cache",)}
            names = {item.name for item in service.store.list_tags()}
            assert "LIVE_CURRENT" in names and "INSPECTED_A" not in names
            if change == "current":
                assert "ADDED_AFTER_INSPECTION" in names
        assert commands.count("inspect_backup") == 1
        assert commands.count("restore_backup") == 1
        assert len(messages) == (2 if change in {"index", "followup"} else 1)
        assert "signature" not in messages[0]
        if change in {"index", "followup"}:
            assert "管理信息已恢复" in messages[-1] and "不要重复恢复" in messages[-1]
        assert body.read_bytes() == b"original document body must not change"
    finally:
        if window:
            window.Destroy()
        service.close()
