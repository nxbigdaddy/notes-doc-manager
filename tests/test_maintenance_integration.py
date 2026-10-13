"""Retry policy exercised through the real application actor and temporary files."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import pytest

from notes.application.retry import RETRY_DELAYS, RetryQueue
from notes.application.service import ApplicationService
from notes.domain.models import ReadResult
from notes.infrastructure import files


@pytest.fixture
def service_setup(tmp_path):
    directory = tmp_path / "documents"
    directory.mkdir()
    source = directory / "document.txt"
    source.write_text("original document body", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    clock = [0.0]
    service._retries = RetryQueue(clock=lambda: clock[0])
    try:
        library = service.call("add_library", {"root_path": str(directory)})
        service.call("scan", {})
        yield service, library, source, clock
    finally:
        service.close()


def fail_first_read(service, library, monkeypatch):
    original = files.read_document
    calls = []
    def reader(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            return ReadResult(status="pending", error_code="sharing_violation")
        return original(*args, **kwargs)
    monkeypatch.setattr(files, "read_document", reader)
    service.call("scan", {"library_id": library.id, "force": True})
    return original, calls


def dispatch_and_drain(service, clock, now):
    clock[0] = now
    service._dispatch_retries()
    # A FIFO actor command is a deterministic barrier for dispatched retries.
    service.call("state")


def test_transient_read_retries_single_document_without_rescan(service_setup, monkeypatch):
    service, library, source, clock = service_setup
    _, calls = fail_first_read(service, library, monkeypatch)
    assert service.store.list_documents(library.id)[0].index_status == "pending"
    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("A retry must not scan the library")
    monkeypatch.setattr(files, "scan_directory", unexpected_scan)
    dispatch_and_drain(service, clock, 0.49)
    assert len(calls) == 1
    dispatch_and_drain(service, clock, 0.5)
    assert len(calls) == 2
    assert service.call("query", {"text": "original document", "mode": "content"}).total == 1
    assert service._retries.health()["tracked"] == 0
    assert source.read_text(encoding="utf-8") == "original document body"


def test_service_stops_after_five_retries_without_hot_loop(service_setup, monkeypatch):
    service, library, _, clock = service_setup
    calls = []
    def always_locked(*args, **kwargs):
        calls.append(None)
        return ReadResult(status="pending", error_code="sharing_violation")
    monkeypatch.setattr(files, "read_document", always_locked)
    service.call("scan", {"library_id": library.id, "force": True})
    now = 0
    for retry, delay in enumerate(RETRY_DELAYS, start=1):
        dispatch_and_drain(service, clock, now + delay - 0.01)
        assert len(calls) == retry
        now += delay
        dispatch_and_drain(service, clock, now)
        assert len(calls) == retry + 1
    for later in range(100, 120):
        dispatch_and_drain(service, clock, later)
    assert len(calls) == 6  # One initial read, five bounded retries.
    assert service._retries.health()["exhausted"] == 1
    assert service._retries.health()["in_flight"] == 0


def test_repeated_unstable_reads_do_not_restart_attempt_one_via_dirty_scans(
    service_setup, monkeypatch
):
    service, library, _, clock = service_setup
    calls = []
    def continuously_changing(*args, **kwargs):
        calls.append(None)
        return ReadResult(status="pending", error_code="changed")
    monkeypatch.setattr(files, "read_document", continuously_changing)
    service.call("scan", {"library_id": library.id, "force": True})
    original_generation = service.store.list_documents(library.id)[0].content_generation
    for tick in range(1, 40):
        dispatch_and_drain(service, clock, float(tick))
        # Reproduce the real maintenance loop's dirty-library processing without
        # waiting for OS timers; no external file event or stat change occurs.
        with service._lock:
            dirty = tuple(service._dirty)
            service._dirty.clear()
        for identifier in dirty:
            service.call("scan", {"library_id": identifier})
    assert len(calls) == 6, "Internal changed->refresh loop restarted bounded retries"
    assert service._retries.health()["exhausted"] == 1
    assert service.store.list_documents(library.id)[0].content_generation == original_generation


def test_cancel_after_scan_invalidates_already_queued_retry(service_setup, monkeypatch):
    service, library, _, clock = service_setup
    _, calls = fail_first_read(service, library, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    def block_actor():
        entered.set()
        assert release.wait(5)
    barrier = service._commands.submit(block_actor)
    try:
        assert entered.wait(2)
        clock[0] = 0.5
        service._dispatch_retries()
        assert service._retries.health()["in_flight"] == 1
        service.call("cancel_scan", {}, timeout=1)
    finally:
        release.set()
    barrier.result(timeout=3)
    service.call("state")
    assert len(calls) == 1, "A cancelled leased retry still performed IO"
    assert service._retries.health()["tracked"] == 0


def test_cancel_inflight_retry_prevents_completed_read_committing(service_setup, monkeypatch):
    service, library, _, clock = service_setup
    original, _ = fail_first_read(service, library, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    def blocked_read(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(files, "read_document", blocked_read)
    clock[0] = 0.5
    service._dispatch_retries()
    try:
        assert entered.wait(2)
        service.call("cancel_scan", {}, timeout=1)
    finally:
        release.set()
    service.call("state")
    assert service.store.list_documents(library.id)[0].index_status == "pending"
    assert service._retries.health()["tracked"] == 0


def test_closing_with_retry_inflight_releases_threads_and_drops_result(
    service_setup, monkeypatch
):
    service, library, _, clock = service_setup
    original, _ = fail_first_read(service, library, monkeypatch)
    existing = {thread for thread in threading.enumerate()
                if not thread.name.startswith("notes-")}
    entered = threading.Event()
    release = threading.Event()
    def blocked_read(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(files, "read_document", blocked_read)
    clock[0] = 0.5
    service._dispatch_retries()
    assert entered.wait(2)
    closed = threading.Event()
    def close_service():
        service.close()
        closed.set()
    closer = threading.Thread(target=close_service, name="test-close-coordinator")
    closer.start()
    try:
        assert service._stop.wait(2)
    finally:
        release.set()
    assert closed.wait(3)
    closer.join(timeout=1)
    assert service._retries.health()["closed"]
    assert service.store.list_documents(library.id)[0].index_status == "pending"
    assert not [thread for thread in threading.enumerate()
                if thread not in existing and thread.is_alive()]


def test_retry_changed_metadata_requires_refresh_before_new_generation_index(
    service_setup, monkeypatch
):
    service, library, source, clock = service_setup
    original, _ = fail_first_read(service, library, monkeypatch)
    old_document = service.store.list_documents(library.id)[0]
    clock[0] = 0.5
    item = service._retries.pop_due()[0]
    source.write_text("replacement content has changed size", encoding="utf-8")
    monkeypatch.setattr(files, "read_document", original)
    result = service.call("retry_index", {"item": item})
    assert result["status"] == "refresh_pending"
    assert not service._retries.is_current(item)
    before_refresh = service.store.get_document(old_document.id)
    assert before_refresh.content_generation == old_document.content_generation
    assert before_refresh.index_status == "pending"
    assert library.id in service._dirty
    service.call("scan", {"library_id": library.id})
    current = service.store.get_document(old_document.id)
    assert current.content_generation > old_document.content_generation
    assert current.index_status == "ready"
    assert service.call("retry_index", {"item": item})["status"] == "stale"
    assert service.call("query", {"text": "replacement content", "mode": "content"}).total == 1
    assert service.call("query", {"text": "original document", "mode": "content"}).total == 0


def test_disabled_library_drops_a_leased_retry_without_io(service_setup, monkeypatch):
    service, library, _, clock = service_setup
    _, calls = fail_first_read(service, library, monkeypatch)
    clock[0] = 0.5
    item = service._retries.pop_due()[0]
    service.call("set_library_enabled", {"library_id": library.id, "enabled": False})
    assert service.call("retry_index", {"item": item})["status"] == "stale"
    assert len(calls) == 1
    assert service._retries.health()["tracked"] == 0


@contextmanager
def exclusive_windows_reader(path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows actual sharing violation")
def test_real_monitor_automatically_retries_released_sharing_lock(tmp_path):
    directory = tmp_path / "documents"
    directory.mkdir()
    source = directory / "document.txt"
    original = "temporarily locked content"
    source.write_text(original, encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=True)
    try:
        library = service.call("add_library", {"root_path": str(directory)})
        service.call("scan", {})
        with exclusive_windows_reader(source):
            read = files.read_document(source)
            assert read.error_code == "sharing_violation"
            service.call("scan", {"library_id": library.id, "force": True})
            assert service._retries.health()["pending"] == 1
            assert service.call("query", {"text": "temporarily", "mode": "content"}).total == 0
        # Releasing a read-only handle does not edit the file. Only retry scheduling
        # should restore this index, not the 15-minute compensating scan.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = service.call("query", {"text": "temporarily", "mode": "content"})
            if result.total:
                break
            time.sleep(0.05)
        else:
            pytest.fail("Unlocking did not trigger automatic read retry recovery")
        assert service._retries.health()["tracked"] == 0
        assert source.read_text(encoding="utf-8") == original
    finally:
        service.close()
