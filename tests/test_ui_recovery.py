"""Hidden recovery dialog tests; never show prompts or access user metadata."""
import os
from threading import Event, get_ident
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden recovery GUI tests")


class FakeManager:
    def __init__(self):
        self.calls = []
        self.current = {"status": "corrupt", "message": "测试损坏", "signature": "current-1",
                        "recoverable": True, "version": 1}
        self.inspection = {"kind": "json", "signature": "backup-1", "path": "C:/test/backup.json",
                           "libraries": 2, "documents": 30, "tags": 4}
        self.inspect_started = Event()
        self.inspect_release = None
        self.recover_started = Event()
        self.recover_release = None
        self.commit_despite_cancel = False
        self.recover_error = None

    def record(self, command, payload=None):
        self.calls.append((command, payload, get_ident()))

    def list_backups(self):
        self.record("list")
        return ({"path": "C:/test/backup.json", "name": "自动备份.json",
                 "size": 2048, "mtime": 1789444800.0},)

    def inspect_current(self):
        self.record("current")
        return dict(self.current)

    def inspect_backup(self, path):
        self.record("inspect", path)
        self.inspect_started.set()
        if self.inspect_release is not None:
            assert self.inspect_release.wait(5)
        return dict(self.inspection, path=path)

    def recover(self, path, **kwargs):
        from notes.domain.models import NotesError
        self.record("recover", {"path": path, **kwargs})
        self.recover_started.set()
        if self.recover_release is not None:
            assert self.recover_release.wait(5)
        if self.recover_error:
            raise self.recover_error
        if kwargs["cancel"].is_set() and not self.commit_despite_cancel:
            raise NotesError("cancelled", "提交前已取消恢复")
        return {"recovered": True, "quarantine_path": "C:/test/quarantine", "message": "恢复完成"}


@pytest.fixture
def recovery():
    import wx
    from notes.ui.recovery_dialog import RecoveryDialog
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    manager = FakeManager()
    dialog = RecoveryDialog(None, manager, manager.current)

    def pump_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail("Recovery GUI timed out")

    try:
        pump_until(lambda: not dialog._busy)
        yield dialog, manager, pump_until
    finally:
        if manager.inspect_release is not None:
            manager.inspect_release.set()
        if manager.recover_release is not None:
            manager.recover_release.set()
        if dialog:
            dialog.Destroy()
            pump_until(lambda: not dialog)
        app.Yield()
        del activator
        app.Destroy()


def inspect(dialog, manager, pump):
    dialog._inspect(manager.inspection["path"])
    pump(lambda: dialog.inspection is not None and not dialog._busy)


def test_recovery_initial_state_is_explicit_and_no_automatic_mutation(recovery):
    dialog, manager, _ = recovery
    assert not dialog.IsShown()
    assert dialog.backup_list.GetItemCount() == 1
    assert not dialog.recover_button.IsEnabled()
    assert dialog.result is None
    assert [call[0] for call in manager.calls] == ["list"]
    assert manager.calls[0][2] != get_ident()
    assert "不包含" not in dialog.details.GetValue()  # nothing falsely claims inspected yet


def test_recovery_requires_consent_and_passes_exact_fingerprints_off_thread(recovery):
    import wx
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    assert "文档库：2" in dialog.details.GetValue()
    assert "文档记录：30" in dialog.details.GetValue()
    assert "标签：4" in dialog.details.GetValue()
    assert "不包含文档正文" in dialog.details.GetValue()
    assert "编辑器配置需要重新确认" in dialog.details.GetValue()
    dialog._recover()
    assert not any(call[0] == "recover" for call in manager.calls)
    dialog.consent.SetValue(True)
    dialog._update_controls()
    assert dialog.recover_button.IsEnabled()
    dialog._recover()
    pump(lambda: dialog._closed)
    call = next(call for call in manager.calls if call[0] == "recover")
    assert call[2] != get_ident()
    assert call[1]["confirmed"] is True
    assert call[1]["expected_current"] == "current-1"
    assert call[1]["expected_backup"] == "backup-1"
    assert isinstance(call[1]["cancel"], Event)
    assert dialog.GetReturnCode() == wx.ID_OK
    assert dialog.result["recovered"] is True
    assert dialog.exit_requested is False
    assert not dialog._worker.is_alive()


def test_recovery_blocked_current_status_cannot_inspect_or_mutate(recovery):
    dialog, manager, pump = recovery
    manager.current = {"status": "future_version", "message": "新版本数据禁止覆盖",
                       "signature": "future", "version": 999, "recoverable": False}
    dialog._recheck()
    pump(lambda: not dialog._busy)
    assert not dialog.browse_button.IsEnabled()
    assert not dialog.backup_list.IsEnabled()
    assert not dialog.recover_button.IsEnabled()
    count = len(manager.calls)
    dialog._inspect("C:/anything.json")
    dialog.consent.SetValue(True)
    dialog._recover()
    assert len(manager.calls) == count


@pytest.mark.parametrize("status", ["future_version", "busy", "permission"])
def test_initial_blocked_status_never_lists_or_opens_backup(recovery, status):
    from notes.ui.recovery_dialog import RecoveryDialog
    parent_dialog, _, pump = recovery
    manager = FakeManager()
    manager.current.update(status=status, recoverable=False)
    blocked = RecoveryDialog(parent_dialog, manager, manager.current)
    try:
        assert not blocked.IsShown()
        assert not blocked.recover_button.IsEnabled()
        assert not blocked.browse_button.IsEnabled()
        assert not blocked.backup_list.IsEnabled()
        blocked._inspect("C:/test/backup.json")
        blocked.consent.SetValue(True)
        blocked._recover()
        assert manager.calls == []
    finally:
        blocked.Destroy()
        pump(lambda: not blocked)


def test_recheck_invalidates_prior_backup_confirmation_and_signature(recovery):
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    dialog.consent.SetValue(True)
    manager.current["signature"] = "current-after-explicit-recheck"
    dialog._recheck()
    pump(lambda: not dialog._busy)
    assert dialog.initial_status["signature"] == "current-after-explicit-recheck"
    assert dialog.inspection is None
    assert not dialog.consent.GetValue()
    assert not dialog.recover_button.IsEnabled()


def test_close_during_inspection_waits_for_worker_without_touching_destroyed_widgets(recovery):
    dialog, manager, pump = recovery
    manager.inspect_release = Event()
    dialog._inspect(manager.inspection["path"])
    pump(manager.inspect_started.is_set)
    started = time.monotonic()
    dialog.Destroy()
    assert time.monotonic() - started < .2  # no main-thread join
    assert dialog and dialog._worker.is_alive()
    assert dialog._cancel.is_set()
    assert not dialog.recover_button.IsEnabled()
    manager.inspect_release.set()
    pump(lambda: not dialog)
    assert not dialog._worker.is_alive()
    assert not any(call[0] == "recover" for call in manager.calls)


def test_cancel_before_backend_commit_does_not_claim_recovery(recovery):
    import wx
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    manager.recover_release = Event()
    dialog.consent.SetValue(True)
    dialog._recover()
    pump(manager.recover_started.is_set)
    dialog._on_cancel()
    assert dialog._cancel.is_set() and dialog._worker.is_alive()
    manager.recover_release.set()
    pump(lambda: dialog._closed)
    assert dialog.GetReturnCode() == wx.ID_CANCEL
    assert dialog.result is None
    assert dialog.exit_requested is True
    assert not dialog._worker.is_alive()


def test_cancel_after_commit_reports_actual_success_not_false_cancellation(recovery):
    import wx
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    manager.recover_release = Event()
    manager.commit_despite_cancel = True
    dialog.consent.SetValue(True)
    dialog._recover()
    pump(manager.recover_started.is_set)
    dialog._on_cancel()
    manager.recover_release.set()
    pump(lambda: dialog._closed)
    assert dialog.GetReturnCode() == wx.ID_OK
    assert dialog.result["recovered"] is True
    assert dialog.exit_requested is True  # caller must exit, not open the main frame
    assert not dialog._worker.is_alive()


def test_backend_rejection_is_inline_and_can_choose_another_backup(recovery):
    from notes.domain.models import NotesError
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    manager.recover_error = NotesError("changed", "当前库已变化，请重新检查")
    dialog.consent.SetValue(True)
    dialog._recover()
    pump(lambda: not dialog._busy)
    assert "当前库已变化" in dialog.status_label.GetLabel()
    assert not dialog._closing
    assert dialog.browse_button.IsEnabled()
    assert dialog.recheck_button.IsEnabled()
    assert dialog.result is None
    dialog._inspect("C:/test/second.db")
    pump(lambda: not dialog._busy)
    assert dialog.inspection["path"] == "C:/test/second.db"
    assert not dialog.consent.GetValue()


def test_stale_callback_cannot_replace_a_later_inspection(recovery):
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    stale_generation = dialog._generation
    dialog._inspect("C:/test/later.db")
    pump(lambda: not dialog._busy)
    dialog._completed(stale_generation, "inspect",
                      {"path": "C:/stale.json", "signature": "stale"}, None)
    assert dialog.inspection["path"] == "C:/test/later.db"
    assert dialog.path.GetValue() == "C:/test/later.db"


def test_modal_success_returns_ok_only_after_worker_has_exited(recovery, monkeypatch):
    import wx
    dialog, manager, pump = recovery
    inspect(dialog, manager, pump)
    endings = []
    monkeypatch.setattr(dialog, "IsModal", lambda: True)
    monkeypatch.setattr(dialog, "EndModal", lambda code: endings.append(
        (code, dialog._worker.is_alive())))
    dialog.consent.SetValue(True)
    dialog._recover()
    pump(lambda: bool(endings))
    assert endings == [(wx.ID_OK, False)]
    assert dialog.result["recovered"]
    monkeypatch.setattr(dialog, "IsModal", lambda: False)


@pytest.mark.parametrize("kind", ["json", "db"])
def test_real_corrupt_metadata_recovery_hidden_gui_preserves_documents(tmp_path, kind):
    import sqlite3
    import traceback
    import wx
    from notes.application.service import ApplicationService
    from notes.infrastructure.recovery import RecoveryManager
    from notes.infrastructure.store import Store
    from notes.ui.recovery_dialog import RecoveryDialog

    docs = tmp_path / "documents"
    docs.mkdir()
    body_path = docs / "正文不修改.txt"
    body_path.write_text("恢复管理信息不能触碰这份正文。😀", encoding="utf-8")
    body_before = body_path.read_bytes()
    data = tmp_path / "metadata"
    backup_path = tmp_path / ("valid-backup." + kind)
    service = ApplicationService(data, monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(docs), "name": "恢复验证库"})
        service.call("scan", {"library_id": library.id})
        document = service.call("query").items[0].document
        tag = service.call("create_tag", {"name": "需要恢复的中文标签"})
        service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
        service.call("set_favorite", {"document_id": document.id, "value": True})
        service.store.set_setting("editor_path", "C:/must-reconfirm/Notepad3.exe")
        if kind == "json":
            service.call("export_backup", {"path": str(backup_path)})
    finally:
        service.close()
    metadata_path = data / "metadata.db"
    if kind == "db":
        source = sqlite3.connect(str(metadata_path))
        destination = sqlite3.connect(str(backup_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    # Deliberately corrupt only our generated fixture, never user metadata.
    assert metadata_path.resolve().is_relative_to(tmp_path.resolve())
    metadata_path.write_bytes(b"not sqlite: intentionally corrupted GUI fixture")
    manager = RecoveryManager(data)
    initial = manager.inspect_current()
    assert initial["recoverable"] is True
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    dialog = RecoveryDialog(None, manager, initial)

    def pump_until(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail(f"Real recovery GUI timed out: {dialog.status_label.GetLabel()}")

    try:
        assert not dialog.IsShown()
        pump_until(lambda: not dialog._busy)
        dialog._inspect(str(backup_path))
        pump_until(lambda: not dialog._busy)
        assert dialog.inspection is not None, dialog.status_label.GetLabel()
        assert dialog.inspection["documents"] == 1
        assert dialog.inspection["tags"] == 1
        dialog.consent.SetValue(True)
        dialog._recover()
        pump_until(lambda: dialog._closed or (not dialog._busy and not dialog._closing))
        assert dialog._closed, (
            dialog.status_label.GetLabel(),
            repr(dialog._last_outcome[3]),
            "".join(traceback.format_exception(
                getattr(dialog._last_outcome[3], "__context__", None))))
        assert dialog.GetReturnCode() == wx.ID_OK
        assert dialog.result["recovered"] is True
        assert not dialog._worker.is_alive()
        assert body_path.read_bytes() == body_before
        restored = Store(data)
        try:
            recovered = restored.get_document(document.id)
            assert recovered.is_favorite
            assert [item.id for item in recovered.tags] == [tag.id]
            assert restored.get_setting("editor_path", "") == ""
        finally:
            restored.close()
    finally:
        if dialog:
            dialog.Destroy()
            pump_until(lambda: not dialog)
        app.Yield()
        del activator
        app.Destroy()
