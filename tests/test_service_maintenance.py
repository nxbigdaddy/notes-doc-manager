"""Clock-controlled cross-restart verification and application backup orchestration."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError, ReadResult, ScanResult
from notes.infrastructure import files

BASE = datetime(2026, 9, 15, 8, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, now=BASE):
        self.now = now

    def __call__(self):
        return self.now


def prepare(tmp_path, clock):
    root = tmp_path / "documents"
    root.mkdir()
    path = root / "note.txt"
    path.write_text("old-source", encoding="utf-8")
    data = tmp_path / "data"
    service = ApplicationService(data, monitoring=False, wall_clock=clock)
    library = service.call("add_library", {"root_path": str(root)})
    service.call("scan", {})
    return service, library, path, data


def test_full_verification_timestamp_is_durable_and_derived(tmp_path):
    clock = Clock()
    service, library, _, data = prepare(tmp_path, clock)
    key = f"__maintenance.hash_verified.{library.id}"
    try:
        assert float(service.store.get_setting(key)) == BASE
        first = service.call("automatic_backup", {"force": True})
        assert first["status"] == "created"
        service.call("scan", {"force": True})
        assert service.call("backup_status")["dirty"] is False
    finally:
        service.close()
    reopened = ApplicationService(data, monitoring=False, wall_clock=clock)
    try:
        reopened.call("state")  # Barrier after startup scan.
        assert float(reopened.store.get_setting(key)) == BASE
        assert reopened.call("backup_status")["dirty"] is False
    finally:
        reopened.close()


def test_reopening_before_due_does_not_reread_ready_files(tmp_path, monkeypatch):
    clock = Clock()
    service, library, _, data = prepare(tmp_path, clock)
    service.close()
    clock.now += 3600
    reads = []
    original = files.read_document

    def counted(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(files, "read_document", counted)
    reopened = ApplicationService(data, monitoring=False, wall_clock=clock)
    try:
        reopened.call("state")
        assert reads == []
        assert not reopened._hash_due(library.id)
    finally:
        reopened.close()


def test_reopening_after_due_detects_same_size_same_time_change(tmp_path):
    clock = Clock()
    service, library, path, data = prepare(tmp_path, clock)
    before = path.stat()
    service.close()
    path.write_text("new-source", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    clock.now += 86401
    reopened = ApplicationService(data, monitoring=False, wall_clock=clock)
    try:
        found = reopened.call("query", {"text": "new-source", "mode": "content"})
        # Query has its own executor, so wait for the startup writer before asserting.
        reopened.call("state")
        found = reopened.call("query", {"text": "new-source", "mode": "content"})
        assert found.total == 1
        assert reopened.call("query", {"text": "old-source", "mode": "content"}).total == 0
        assert float(reopened.store.get_setting(
            f"__maintenance.hash_verified.{library.id}"
        )) == clock.now
    finally:
        reopened.close()


@pytest.mark.parametrize("timestamp", ["not a timestamp", "nan", "inf", str(BASE + 86400 * 100)])
def test_invalid_or_future_verification_timestamp_is_due(tmp_path, timestamp):
    clock = Clock()
    service, library, _, _ = prepare(tmp_path, clock)
    try:
        service.store.set_setting(f"__maintenance.hash_verified.{library.id}", timestamp)
        assert service._hash_due(library.id)
        service.call("scan", {})
        assert not service._hash_due(library.id)
    finally:
        service.close()


def test_partial_or_failed_verification_never_marks_success(tmp_path, monkeypatch):
    clock = Clock()
    service, library, _, _ = prepare(tmp_path, clock)
    key = f"__maintenance.hash_verified.{library.id}"
    original = service.store.get_setting(key)
    try:
        clock.now += 86401
        monkeypatch.setattr(
            files, "scan_directory",
            lambda *_args, **_kwargs: ScanResult(complete=False, errors=("partial",)),
        )
        service.call("scan", {"force": True})
        assert service.store.get_setting(key) == original
        assert service._hash_due(library.id, ignore_delay=True)
        assert not service._hash_due(library.id)  # Bounded delay, not a false success stamp.
    finally:
        service.close()


def test_read_failure_does_not_advance_full_verification(tmp_path, monkeypatch):
    clock = Clock()
    service, library, _, _ = prepare(tmp_path, clock)
    key = f"__maintenance.hash_verified.{library.id}"
    original = service.store.get_setting(key)
    try:
        clock.now += 86401
        monkeypatch.setattr(
            files, "read_document",
            lambda *_args, **_kwargs: ReadResult(
                status="pending", error_code="sharing_violation"
            ),
        )
        service.call("scan", {"force": True})
        assert service.store.get_setting(key) == original
        assert service._retries.health()["pending"] == 1
        service.call("cancel_scan")
        assert service._retries.health()["tracked"] == 0
        assert not service._hash_due(library.id)
    finally:
        service.close()


def test_maintenance_backup_survives_restart_and_skips_unchanged(tmp_path):
    clock = Clock()
    service, _, path, data = prepare(tmp_path, clock)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        service.call("maintenance")
        summary = service.call("backup_status")
        assert summary["last_success"] == BASE
        assert not summary["dirty"]
        assert service.call("state").last_backup_at == BASE
        assert len(list((data / "backups").glob("auto-*.db"))) == 1
        service.call("create_tag", {"name": "下一天"})
        service.call("maintenance")
        assert len(list((data / "backups").glob("auto-*.db"))) == 1
    finally:
        service.close()
    clock.now += 86400
    reopened = ApplicationService(data, monitoring=False, wall_clock=clock)
    try:
        reopened.call("maintenance")
        assert len(list((data / "backups").glob("auto-*.db"))) == 2
        assert reopened.call("backup_status")["last_success"] == clock.now
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    finally:
        reopened.close()


def test_backup_failure_uses_application_backoff_and_keeps_prior_status(tmp_path, monkeypatch):
    clock = Clock()
    service, _, _, _ = prepare(tmp_path, clock)
    calls = []
    try:
        def fail(**_kwargs):
            calls.append(clock.now)
            return {
                "status": "failed", "message": "测试备份失败",
                "last_success": None, "dirty": True, "due": True,
            }

        monkeypatch.setattr(service.store, "run_automatic_backup", fail)
        service.call("maintenance")
        for delta in (1, 10, 100, 299):
            clock.now = BASE + delta
            service.call("maintenance")
        assert len(calls) == 1
        assert service.call("state").backup_status == "测试备份失败"
        clock.now = BASE + 300
        service.call("maintenance")
        assert len(calls) == 2
    finally:
        service.close()


def test_relocation_confirmation_preserves_identity_and_source_bytes(tmp_path):
    clock = Clock()
    service, library, old_path, _ = prepare(tmp_path, clock)
    try:
        document = service.store.list_documents(library.id)[0]
        tag = service.call("create_tag", {"name": "关联保留"})
        service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
        service.call("set_favorite", {"document_id": document.id, "value": True})
        new_path = old_path.with_name("renamed.txt")
        old_path.rename(new_path)
        digest = hashlib.sha256(new_path.read_bytes()).hexdigest()
        service.call("scan", {})
        target = {"document_id": document.id, "target_path": str(new_path)}
        inspection = service.call("inspect_relocation", target)
        assert inspection["requires_merge_confirmation"]
        with pytest.raises(NotesError):
            service.call("relocate_document", target)
        with pytest.raises(NotesError):
            service.call("relocate_document", dict(target, confirmed=True))
        result = service.call(
            "relocate_document", dict(target, confirmed=True, merge_unannotated=True)
        )
        assert result.id == document.id and result.is_favorite
        assert result.tags == (tag,)
        assert result.relative_path == "renamed.txt" and result.index_status == "ready"
        assert len(service.store.list_documents(library.id)) == 1
        assert hashlib.sha256(new_path.read_bytes()).hexdigest() == digest
        assert not old_path.exists()
    finally:
        service.close()
