"""Quick-open integration on temporary files; editor and system hotkeys never run."""
from concurrent.futures import CancelledError
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError
from notes.infrastructure import editor, files
from notes.infrastructure.recovery import RecoveryManager


def hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@contextmanager
def blocked_executor(executor):
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(5), "test did not release its executor barrier"
    barrier = executor.submit(hold)
    assert entered.wait(3)
    try:
        yield
    finally:
        release.set()
        barrier.result(timeout=3)


def populate(service, root, count=3):
    root.mkdir()
    for number in range(count):
        (root / f"文档-{number:03d}.txt").write_text(f"正文唯一锚点 {number:03d}\nunchanged body", encoding="utf-8")
    library = service.call("add_library", {"root_path": str(root)})
    service.call("scan", {"library_id": library.id})
    records = sorted(service.store.list_documents(library.id), key=lambda doc: doc.file_name)
    tag = service.call("create_tag", {"name": "保留标签"})
    service.call("set_document_tags", {"document_id": records[0].id, "tag_ids": [tag.id]})
    service.call("set_favorite", {"document_id": records[0].id, "value": True})
    return library, records, tag


@pytest.fixture
def real_service(tmp_path):
    application = ApplicationService(tmp_path / "data", monitoring=False)
    root = tmp_path / "documents"
    library, records, tag = populate(application, root)
    try:
        yield application, root, library, records, tag
    finally:
        application.close()


def test_hotkey_default_strict_boolean_and_persistence_without_native_registration(tmp_path):
    data = tmp_path / "data"
    service = ApplicationService(data, monitoring=False)
    try:
        assert service.call("state").quick_open_hotkey_enabled is False
        service.call("set_quick_open_hotkey", {"enabled": True})
        assert service.call("state").quick_open_hotkey_enabled is True
        assert service.store.get_setting("quick_open_hotkey_enabled") == "1"
    finally:
        service.close()
    reopened = ApplicationService(data, monitoring=False)
    try:
        assert reopened.call("state").quick_open_hotkey_enabled is True
        reopened.call("set_quick_open_hotkey", {"enabled": False})
        assert reopened.call("state").quick_open_hotkey_enabled is False
        assert reopened.store.get_setting("quick_open_hotkey_enabled") == "0"
    finally:
        reopened.close()
    final = ApplicationService(data, monitoring=False)
    try:
        assert final.call("state").quick_open_hotkey_enabled is False
    finally:
        final.close()


@pytest.mark.parametrize("value", [None, 0, 1, "0", "1", "false", "true", "", [], {}])
def test_hotkey_setting_rejects_truthy_non_booleans(real_service, value):
    service, root, _, records, tag = real_service
    before = hashes(root)
    service.call("set_quick_open_hotkey", {"enabled": True})
    with pytest.raises(NotesError) as failure:
        service.call("set_quick_open_hotkey", {"enabled": value})
    assert failure.value.code == "invalid_setting"
    assert service.call("state").quick_open_hotkey_enabled is True
    assert service.store.get_document(records[0].id).tags == (tag,)
    assert hashes(root) == before


def test_real_quick_name_search_does_not_inherit_main_filters_or_match_body(real_service, tmp_path, monkeypatch):
    service, root, library, records, tag = real_service
    other = tmp_path / "other-library"
    other.mkdir()
    (other / "另一个文档.txt").write_text("another unchanged body", encoding="utf-8")
    second = service.call("add_library", {"root_path": str(other)})
    service.call("scan", {"library_id": second.id})
    before = hashes(root), hashes(other)
    main = service.call("query", {"channel": "main", "library_id": library.id,
                                 "tag_ids": [tag.id], "favorites_only": True})
    assert main.total == 1 and main.items[0].document.id == records[0].id
    def no_document_reads(*args, **kwargs):
        raise AssertionError("Quick name queries must not read document files")
    monkeypatch.setattr(files, "read_document", no_document_reads)
    quick = service.call("query", {"channel": "quick", "text": "文档", "mode": "name", "page_size": 20})
    assert quick.total == 4 and quick.complete
    assert {hit.document.library_id for hit in quick.items} == {library.id, second.id}
    assert service.call("query", {"channel": "quick", "text": "正文唯一锚点",
                                  "mode": "name", "page_size": 20}).total == 0
    assert (hashes(root), hashes(other)) == before


def test_empty_quick_request_uses_recent_hundred_scope_with_twenty_item_page(tmp_path):
    root = tmp_path / "documents"
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        _, documents, _ = populate(service, root, count=120)
        # Seed deterministic existing launch history without running an editor.
        with service.store._connect() as connection, service.store._transaction(connection):
            connection.executemany("UPDATE documents SET last_open_requested_at=? WHERE id=?",
                                   [(index + 1, document.id) for index, document in enumerate(documents[:110])])
        result = service.call("query", {"channel": "quick", "text": "", "mode": "name",
                                        "recent_only": True, "page_size": 20})
        assert result.total == result.covered == 100 and len(result.items) == 20
        assert [hit.document.id for hit in result.items] == [doc.id for doc in reversed(documents[90:110])]
        tail = service.call("query", {"channel": "quick", "text": "", "mode": "name",
                                      "recent_only": True, "page_size": 20, "page": 4})
        assert [hit.document.id for hit in tail.items] == [doc.id for doc in reversed(documents[10:30])]
        assert not service.call("query", {"channel": "quick", "mode": "name",
                                          "recent_only": True, "page_size": 20, "page": 5}).items
    finally:
        service.close()


def test_open_request_only_invokes_mock_editor_and_preserves_source_tags(real_service, tmp_path, monkeypatch):
    service, root, _, records, tag = real_service
    fake_editor = tmp_path / "Notepad3.exe"
    fake_editor.write_bytes(b"Test fixture only: never executed")
    service.call("set_editor", {"path": str(fake_editor)})
    before = hashes(root)
    launches = []
    monkeypatch.setattr(editor, "open_document",
                        lambda configured, path, line=None: launches.append((configured, Path(path), line)))
    def no_document_reads(*args, **kwargs):
        raise AssertionError("Opening a selected document must not read its body")
    monkeypatch.setattr(files, "read_document", no_document_reads)
    document = service.call("query", {"channel": "quick", "text": "文档-000",
                                      "mode": "name", "page_size": 20}).items[0].document
    assert document.id == records[0].id and document.last_open_requested_at is None
    assert service.call("open_document", {"document_id": document.id}) is None
    assert launches == [(str(fake_editor.resolve()), root / "文档-000.txt", None)]
    updated = service.store.get_document(document.id)
    assert updated.last_open_requested_at is not None
    assert updated.tags == (tag,) and updated.is_favorite
    assert hashes(root) == before


def test_failed_editor_request_does_not_record_recent_or_touch_source(real_service, monkeypatch):
    service, root, _, records, tag = real_service
    service.store.set_setting("editor_path", "mocked configured path")
    before = hashes(root)
    def fail(*args, **kwargs):
        raise NotesError("editor_launch_failed", "simulated launch failure")
    monkeypatch.setattr(editor, "open_document", fail)
    with pytest.raises(NotesError) as error:
        service.call("open_document", {"document_id": records[0].id})
    assert error.value.code == "editor_launch_failed"
    document = service.store.get_document(records[0].id)
    assert document.last_open_requested_at is None and document.tags == (tag,)
    assert hashes(root) == before


def test_open_action_bypasses_blocked_actor_and_exhausted_background_slots(real_service, monkeypatch):
    service, root, _, records, tag = real_service
    service.store.set_setting("editor_path", "mock editor: never launched")
    before = hashes(root)
    launched = threading.Event()
    calls = []
    def request(configured, path, line=None):
        calls.append(Path(path))
        launched.set()
    monkeypatch.setattr(editor, "open_document", request)
    service._commands.submit(lambda: None).result(timeout=3)
    service._pending_slots = threading.BoundedSemaphore(1)
    with blocked_executor(service._commands):
        queued_state = service.submit("state")
        assert not queued_state.done()
        with pytest.raises(NotesError) as overflow:
            service.submit("query", {"channel": "main", "mode": "name"}).result(timeout=1)
        assert overflow.value.code == "busy"
        assert service.call("query", {"channel": "quick", "mode": "name"}, timeout=3).total == 3
        opening = service.submit("open_document", {"document_id": records[0].id})
        assert launched.wait(3), "open should not wait behind a long scan/write actor"
        assert opening.result(timeout=3) is None
        assert service.store.get_document(records[0].id).last_open_requested_at is not None
        assert not queued_state.done()
    queued_state.result(timeout=3)
    assert calls == [root / records[0].relative_path]
    assert service.store.get_document(records[0].id).tags == (tag,)
    assert hashes(root) == before


def test_all_background_slots_full_leave_quick_open_and_short_preferences_available(
        real_service, tmp_path, monkeypatch, caplog):
    service, root, _, records, tag = real_service
    fake_editor = tmp_path / "Notepad3.exe"
    fake_editor.write_bytes(b"fixture: not a real executable")
    calls = []
    monkeypatch.setattr(editor, "open_document",
                        lambda configured, path, line=None: calls.append((configured, Path(path))))
    before = hashes(root)
    with blocked_executor(service._commands):
        background = [service.submit("state") for _ in range(128)]
        assert not any(future.done() for future in background)
        with pytest.raises(NotesError) as full:
            service.submit("state").result(timeout=1)
        assert full.value.code == "busy"
        quick = service.submit("query", {"channel": "quick", "mode": "name", "page_size": 20})
        assert quick.result(timeout=3).total == 3
        assert service.call("set_editor", {"path": str(fake_editor)}, timeout=3) is None
        assert service.call("set_quick_open_hotkey", {"enabled": True}, timeout=3) is None
        assert service.store.get_setting("quick_open_hotkey_enabled") == "1"
        assert service.call("set_quick_open_hotkey", {"enabled": False}, timeout=3) is None
        assert service.store.get_setting("quick_open_hotkey_enabled") == "0"
        assert service.call("open_document", {"document_id": records[0].id}, timeout=3) is None
        assert not any(future.done() for future in background)
    for future in background:
        future.result(timeout=5)
    for executor in (service._commands, service._search, service._actions):
        executor.submit(lambda: None).result(timeout=3)
    for slots, expected in ((service._pending_slots, 128), (service._quick_query_slots, 32),
                            (service._action_slots, 8)):
        acquired = 0
        try:
            for _ in range(expected):
                assert slots.acquire(blocking=False)
                acquired += 1
            assert not slots.acquire(blocking=False)
        finally:
            for _ in range(acquired):
                slots.release()
    assert calls == [(str(fake_editor.resolve()), root / records[0].relative_path)]
    assert service.store.get_document(records[0].id).tags == (tag,)
    assert hashes(root) == before
    assert not any("exception calling callback" in record.message.lower() for record in caplog.records)


def test_cancelled_queued_open_releases_only_action_capacity(real_service, monkeypatch, caplog):
    service, root, _, records, _ = real_service
    service.store.set_setting("editor_path", "mock editor: never launched")
    service._action_slots = threading.BoundedSemaphore(1)
    calls = []
    monkeypatch.setattr(editor, "open_document", lambda configured, path, line=None: calls.append(Path(path)))
    before = hashes(root)
    with blocked_executor(service._actions):
        queued = service.submit("open_document", {"document_id": records[0].id})
        with pytest.raises(NotesError) as full:
            service.submit("open_document", {"document_id": records[1].id}).result(timeout=1)
        assert full.value.code == "busy"
        assert queued.cancel()
        replacement = service.submit("open_document", {"document_id": records[1].id})
        assert not replacement.done()
        # An exhausted action budget must not consume the search/command budget.
        assert service.call("query", {"channel": "main", "mode": "name"}).total == 3
    with pytest.raises(CancelledError):
        queued.result(timeout=1)
    assert replacement.result(timeout=3) is None
    service._actions.submit(lambda: None).result(timeout=3)
    assert service._action_slots.acquire(blocking=False)
    try:
        assert not service._action_slots.acquire(blocking=False)
    finally:
        service._action_slots.release()
    assert calls == [root / records[1].relative_path]
    assert service.store.get_document(records[0].id).last_open_requested_at is None
    assert not any("operation_failed" in record.message for record in caplog.records)
    assert hashes(root) == before


def test_close_cancels_not_yet_started_open_without_surprise_launch(real_service, monkeypatch):
    service, root, _, records, _ = real_service
    service.store.set_setting("editor_path", "mock editor")
    calls = []
    monkeypatch.setattr(editor, "open_document", lambda *args, **kwargs: calls.append("launched"))
    closed, queued_done = threading.Event(), threading.Event()
    closer = None
    before = hashes(root)
    try:
        with blocked_executor(service._actions):
            queued = service.submit("open_document", {"document_id": records[0].id})
            queued.add_done_callback(lambda future: queued_done.set())
            closer = threading.Thread(target=lambda: (service.close(), closed.set()),
                                      name="quick-open-test-close")
            closer.start()
            assert service._stop.wait(3)
            assert queued_done.wait(3), "closing must cancel queued actions without waiting to launch them"
            assert queued.cancelled()
            assert calls == []
        assert closed.wait(5)
        closer.join(timeout=1)
        assert not any(thread.is_alive() for thread in service._actions._threads)
        assert service.store.get_document(records[0].id).last_open_requested_at is None
        assert hashes(root) == before
    finally:
        if closer:
            closer.join(timeout=5)


def test_close_waits_for_running_open_request_without_terminating_it(real_service, monkeypatch):
    service, root, _, records, _ = real_service
    service.store.set_setting("editor_path", "mock editor")
    entered, release, shutdown_entered, closed = (threading.Event() for _ in range(4))
    calls, close_errors = [], []
    before = hashes(root)
    def running_request(*args, **kwargs):
        calls.append("requested")
        entered.set()
        assert release.wait(5), "test did not release running mock request"
        calls.append("returned")
    monkeypatch.setattr(editor, "open_document", running_request)
    original_shutdown = service._actions.shutdown
    def observe_shutdown(*args, **kwargs):
        assert kwargs.get("wait", True) is True
        shutdown_entered.set()
        return original_shutdown(*args, **kwargs)
    monkeypatch.setattr(service._actions, "shutdown", observe_shutdown)
    opening = service.submit("open_document", {"document_id": records[0].id})
    assert entered.wait(3)
    def closing():
        try:
            service.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            closed.set()
    closer = threading.Thread(target=closing, name="running-open-test-close")
    try:
        closer.start()
        assert shutdown_entered.wait(3)
        assert not opening.cancelled() and not closed.is_set()
        assert calls == ["requested"]
    finally:
        release.set()
        closer.join(timeout=5)
    assert closed.is_set() and not close_errors
    assert opening.result(timeout=1) is None
    assert calls == ["requested", "returned"]
    assert service.store.get_document(records[0].id).last_open_requested_at is not None
    assert hashes(root) == before


def test_open_does_not_use_stale_path_after_concurrent_relocation(real_service, monkeypatch):
    service, root, _, records, tag = real_service
    document = records[0]
    source = root / document.relative_path
    target = root / "重新定位后的原文件.txt"
    service.store.set_setting("editor_path", "mock editor")
    entered, release = threading.Event(), threading.Event()
    original_get = service.store.get_document
    first = True
    def paused_snapshot(identifier):
        nonlocal first
        result = original_get(identifier)
        if identifier == document.id and first:
            first = False
            entered.set()
            assert release.wait(5), "test did not release the old document snapshot"
        return result
    calls = []
    monkeypatch.setattr(service.store, "get_document", paused_snapshot)
    monkeypatch.setattr(editor, "open_document", lambda configured, path, line=None: calls.append(Path(path)))
    opening = service.submit("open_document", {"document_id": document.id})
    try:
        assert entered.wait(3)
        assert source.resolve().is_relative_to(root.resolve())
        assert target.resolve().is_relative_to(root.resolve())
        source.rename(target)
        relocated = service.call("relocate_document", {
            "document_id": document.id, "target_path": str(target), "confirmed": True,
        }, timeout=3)
        assert relocated.relative_path == target.name
        source.write_text("Unrelated new file at the previous path.", encoding="utf-8")
        before = hashes(root)
    finally:
        release.set()
    try:
        opening.result(timeout=3)
    except NotesError:
        pass  # Explicitly rejecting a stale selection is also safe.
    assert calls in ([], [target]), "Never open another file now occupying an obsolete path."
    current = service.store.get_document(document.id)
    assert current.relative_path == target.name and current.tags == (tag,)
    assert hashes(root) == before


def test_ordinary_json_restore_does_not_reactivate_hotkey_configuration(real_service, tmp_path):
    service, root, _, records, tag = real_service
    service.call("set_quick_open_hotkey", {"enabled": True})
    backup = tmp_path / "backup.json"
    service.call("export_backup", {"path": str(backup)})
    payload = json.loads(backup.read_text(encoding="utf-8"))
    # Even explicit machine settings injected into an otherwise valid logical
    # backup must not be activated by ordinary metadata restore.
    payload["settings"] = [{"key": "quick_open_hotkey_enabled", "value": "1"}]
    backup.write_text(json.dumps(payload), encoding="utf-8")
    before = hashes(root)
    checked = service.call("inspect_backup", {"path": str(backup)})
    service.call("restore_backup", {
        "path": str(backup), "confirmed": True,
        "expected_backup": checked["signature"], "expected_current": checked["current_signature"],
    })
    assert service.call("state").quick_open_hotkey_enabled is False
    assert service.store.get_document(records[0].id).tags == (tag,)
    assert hashes(root) == before


@pytest.mark.parametrize("kind", ["json", "db"])
def test_offline_json_or_sqlite_recovery_does_not_reactivate_machine_hotkey(tmp_path, kind):
    data, root = tmp_path / "data", tmp_path / "documents"
    service = ApplicationService(data, monitoring=False)
    try:
        _, records, tag = populate(service, root)
        service.call("set_quick_open_hotkey", {"enabled": True})
        if kind == "json":
            backup = tmp_path / "backup.json"
            service.call("export_backup", {"path": str(backup)})
            payload = json.loads(backup.read_text(encoding="utf-8"))
            payload["settings"] = [{"key": "quick_open_hotkey_enabled", "value": "1"}]
            backup.write_text(json.dumps(payload), encoding="utf-8")
        else:
            backup = service.store.backup_metadata()
            with closing(sqlite3.connect(backup)) as connection:
                assert connection.execute("SELECT value FROM settings WHERE key='quick_open_hotkey_enabled'").fetchone() == ("1",)
    finally:
        service.close()
    before = hashes(root)
    (data / "metadata.db").write_bytes(b"isolated corruption fixture; preserve during recovery")
    manager = RecoveryManager(data)
    current, candidate = manager.inspect_current(), manager.inspect_backup(backup)
    result = manager.recover(backup, True, current["signature"], candidate["signature"])
    assert result["recovered"]
    recovered = ApplicationService(data, monitoring=False)
    try:
        assert recovered.call("state").quick_open_hotkey_enabled is False
        assert recovered.store.get_setting("quick_open_hotkey_enabled", "absent") == "absent"
        assert recovered.store.get_document(records[0].id).tags == (tag,)
        assert hashes(root) == before
    finally:
        recovered.close()


@pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1", reason="Opt-in hidden wx integration")
@pytest.mark.parametrize("queued_close", [False, True])
def test_hidden_quick_dialog_real_service_enter_and_whitespace_recent_contract(tmp_path, monkeypatch, queued_close):
    import wx
    from notes.ui.quick_open import QuickOpenDialog

    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    service = dialog = parent = None
    requests, launches = [], []
    launched = threading.Event()
    def pump(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
        pytest.fail("Hidden quick-open integration did not reach its event condition")
    try:
        root = tmp_path / "documents"
        service = ApplicationService(tmp_path / "data", dispatcher=wx.CallAfter, monitoring=False)
        _, records, tag = populate(service, root)
        fake_editor = tmp_path / "Notepad3.exe"
        fake_editor.write_bytes(b"Not an executable; must never launch")
        service.call("set_editor", {"path": str(fake_editor)})
        service.store.mark_opened(records[0].id)
        before = hashes(root)
        def fake_open(configured, path, line=None):
            launches.append((configured, Path(path), line))
            launched.set()
        monkeypatch.setattr(editor, "open_document", fake_open)
        def no_document_reads(*args, **kwargs):
            raise AssertionError("Quick dialog must not read document bodies")
        monkeypatch.setattr(files, "read_document", no_document_reads)
        original_submit = service.submit
        def record(command, payload=None, on_success=None, on_error=None):
            requests.append((command, dict(payload or {})))
            return original_submit(command, payload, on_success, on_error)
        monkeypatch.setattr(service, "submit", record)
        parent = wx.Frame(None)
        dialog = QuickOpenDialog(parent, service)
        assert not parent.IsShown() and not dialog.IsShown()
        pump(lambda: bool(dialog.state.libraries) and not dialog._query_pending)
        dialog.search.ChangeValue("   ")
        dialog._query()
        pump(lambda: not dialog._query_pending)
        last_query = [payload for command, payload in requests if command == "query"][-1]
        assert last_query["channel"] == "quick" and last_query["mode"] == "name"
        assert last_query["page_size"] == 20 and last_query["recent_only"] is True
        assert [hit.document.id for hit in dialog.hits] == [records[0].id]
        dialog.search.ChangeValue("文档-000")
        dialog._query()
        pump(lambda: not dialog._query_pending and len(dialog.hits) == 1)
        previous_recent = service.store.get_document(records[0].id).last_open_requested_at
        if queued_close:
            with blocked_executor(service._actions):
                dialog._key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_RETURN, Skip=lambda: None))
                assert dialog._open_future is not None and not dialog._open_future.running()
                dialog._key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_ESCAPE, Skip=lambda: None))
                assert dialog.open_request_cancelled is True
            service._actions.submit(lambda: None).result(timeout=3)
            app.Yield()
            assert not launches and not launched.is_set()
            assert service.store.get_document(records[0].id).last_open_requested_at == previous_recent
        else:
            dialog._key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_RETURN, Skip=lambda: None))
            pump(lambda: launched.is_set() and dialog._closed)
            assert launches == [(str(fake_editor.resolve()), root / "文档-000.txt", None)]
        assert any(command == "open_document" and payload["document_id"] == records[0].id
                   for command, payload in requests)
        assert service.store.get_document(records[0].id).tags == (tag,)
        assert hashes(root) == before
    finally:
        if dialog and not dialog._closed:
            dialog.Destroy()
        if service:
            service.close()
        if parent:
            parent.Destroy()
        app.Yield()
        del activator
        app.Destroy()
