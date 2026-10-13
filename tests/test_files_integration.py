"""Real temporary-file security boundaries across service and adapters."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError
from notes.infrastructure.files import _shared_reader, read_document


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "original.txt"
    source.write_text("original source", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {})
        yield service, library, root
    finally:
        service.close()


@pytest.mark.parametrize("relative", ["", "child", ".."])
def test_restore_cannot_reintroduce_app_data_library(managed, tmp_path, relative):
    service, library, root = managed
    backup = tmp_path / "backup.json"
    service.call("export_backup", {"path": str(backup)})
    original = (root / "original.txt").read_bytes()
    payload = json.loads(backup.read_text(encoding="utf-8"))
    payload["libraries"][0]["root_path"] = str((service.data_dir / relative).resolve())
    backup.write_text(json.dumps(payload), encoding="utf-8")
    for command in ("inspect_backup", "restore_backup"):
        with pytest.raises(NotesError):
            service.call(command, {"path": str(backup), "confirmed": True})
    assert service.store.get_library(library.id).root_path == str(root)
    assert (root / "original.txt").read_bytes() == original


def test_restore_rejects_unc_before_io(managed, tmp_path):
    service, _, _ = managed
    backup = tmp_path / "backup.json"
    service.call("export_backup", {"path": str(backup)})
    payload = json.loads(backup.read_text(encoding="utf-8"))
    payload["libraries"][0]["root_path"] = r"\\invalid-server\share"
    backup.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NotesError):
        service.call("inspect_backup", {"path": str(backup)})


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_root_replaced_by_junction_preserves_metadata_and_rejects_write(managed, tmp_path):
    import _winapi
    service, library, root = managed
    document = service.store.list_documents(library.id)[0]
    service.call("set_favorite", {"document_id": document.id, "value": True})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "original.txt").write_text("outside secret", encoding="utf-8")
    root.rename(tmp_path / "original-directory")
    _winapi.CreateJunction(str(outside), str(root))
    result = service.call("scan", {"library_id": library.id})
    assert result["failures"]
    assert service.store.get_library(library.id).availability == "offline"
    assert service.store.get_document(document.id).is_favorite
    assert service.store.get_document(document.id).availability != "missing"
    for command, data in (
        ("preview", {"document_id": document.id}),
        ("create_document", {"library_id": library.id, "filename": "new.txt"}),
    ):
        with pytest.raises(NotesError):
            service.call(command, data)
    assert not (outside / "new.txt").exists()
    assert service.call("query", {"text": "outside secret", "mode": "content"}).total == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_backup_cannot_enter_document_library_through_junction(managed, tmp_path):
    import _winapi
    service, _, root = managed
    alias = tmp_path / "alias"
    _winapi.CreateJunction(str(root), str(alias))
    with pytest.raises(NotesError):
        service.call("export_backup", {"path": str(alias / "backup.json")})
    assert not (root / "backup.json").exists()
    original = (root / "original.txt").read_bytes()
    assert original == b"original source"


@pytest.mark.skipif(os.name != "nt", reason="Windows shared handles")
def test_reader_does_not_prevent_normal_truncate_save(tmp_path):
    source = tmp_path / "document.txt"
    source.write_text("old", encoding="utf-8")
    with _shared_reader(source):
        with source.open("w", encoding="utf-8") as writer:
            writer.write("saved while preview open")
    assert read_document(source).text == "saved while preview open"


def test_monitor_detects_equal_size_equal_mtime_save(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "doc.txt"
    source.write_text("old-content", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=True)
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        stamp = source.stat()
        source.write_text("new-content", encoding="utf-8")
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = service.call("query", {"text": "new-content", "mode": "content"})
            if result.total:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Received a saved-file event but equal-size/mtime content stayed stale")
        assert service.call("query", {"text": "old-content", "mode": "content"}).total == 0
    finally:
        service.close()


def test_monitoring_service_close_releases_owned_threads(tmp_path):
    before = set(threading.enumerate())
    root = tmp_path / "documents"
    root.mkdir()
    service = ApplicationService(tmp_path / "data", monitoring=True)
    service.call("add_library", {"root_path": str(root)})
    service.call("scan", {})
    service.call("query", {})
    service.close()
    service.close()
    remaining = [thread for thread in threading.enumerate()
                 if thread not in before and thread.is_alive()]
    assert not remaining


def test_monitor_health_restart_compensates_missed_save(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import notes.application.service as application
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "doc.txt"
    source.write_text("before", encoding="utf-8")
    offset = [0]
    clock_started = threading.Event()
    def clock():
        clock_started.set()
        return time.monotonic() + offset[0]
    monkeypatch.setattr(application, "time", SimpleNamespace(monotonic=clock))
    service = ApplicationService(tmp_path / "data", monitoring=True)
    try:
        assert clock_started.wait(2)
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        old_observer = service._monitor._observer
        old_observer.stop()
        old_observer.join(timeout=3)
        assert not old_observer.is_alive()
        source.write_text("after!", encoding="utf-8")
        offset[0] = 35  # Trigger health, but not the 15-minute periodic scan.
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = service.call("query", {"text": "after!", "mode": "content"})
            if result.total:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Restarted monitoring without compensating for a missed save")
        assert service._monitor._observer is not old_observer
        assert service._monitor.health()["alive"]
    finally:
        service.close()


def test_cancel_scan_is_immediate_and_partial_enumeration_keeps_documents(managed, monkeypatch):
    from notes.infrastructure import files
    service, library, root = managed
    (root / "second.txt").write_text("second source", encoding="utf-8")
    service.call("scan", {})
    previous = {d.id: d for d in service.store.list_documents(library.id)}
    entered = threading.Event()
    release = threading.Event()
    scandir = os.scandir

    @contextmanager
    def slow_directory(path):
        with scandir(path) as iterator:
            def children():
                for index, child in enumerate(iterator):
                    if index == 1:
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("Test cancellation was not released")
                    yield child
            yield children()

    monkeypatch.setattr(files.os, "scandir", slow_directory)
    future = service.submit("scan", {"library_id": library.id})
    try:
        assert entered.wait(3)
        # The writer is blocked in an actual directory enumeration; cancellation
        # must not wait behind it in the command executor.
        service.call("cancel_scan", {}, timeout=1)
    finally:
        release.set()
    result = future.result(timeout=4)
    assert result["cancelled"]
    current = {d.id: d for d in service.store.list_documents(library.id)}
    assert current.keys() == previous.keys()
    assert all(d.availability == "present" for d in current.values())
    assert (root / "original.txt").read_bytes() == b"original source"
    monkeypatch.setattr(files.os, "scandir", scandir)
    resumed = service.call("scan", {})
    assert not resumed["cancelled"]
    assert resumed["discovered"] == 2


def test_cancel_scan_during_read_defers_index_until_resumed(managed, monkeypatch):
    from notes.infrastructure import files
    service, library, root = managed
    entered = threading.Event()
    release = threading.Event()
    reader = files._shared_reader

    @contextmanager
    def slow_reader(path):
        with reader(path) as stream:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test reader was not released")
            yield stream

    monkeypatch.setattr(files, "_shared_reader", slow_reader)
    future = service.submit("scan", {"library_id": library.id, "force": True})
    try:
        assert entered.wait(3)
        service.call("cancel_scan", {}, timeout=1)
    finally:
        release.set()
    assert future.result(timeout=4)["cancelled"]
    assert service.store.list_documents(library.id)[0].index_status == "pending"
    assert (root / "original.txt").read_bytes() == b"original source"
    monkeypatch.setattr(files, "_shared_reader", reader)
    service.call("scan", {})
    assert service.store.list_documents(library.id)[0].index_status == "ready"
