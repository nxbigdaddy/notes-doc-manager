from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest

from notes.domain.models import FileEntry, NotesError, ReadResult, SearchQuery
from notes.infrastructure.store import BACKUP_STATE_KEY, Store


DAY = 86400
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def library_store(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    return store, library, root


def register(store, library, root, name, text="original content"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    info = path.stat()
    entry = FileEntry(name, info.st_size, info.st_mtime_ns // 1_000_000)
    documents = store.apply_scan(library.id, (entry,), complete=False)
    document = next(d for d in documents if d.relative_path == name)
    store.set_index(document.id, document.content_generation, ReadResult(
        text, "utf-8", size_bytes=entry.size_bytes, modified_at=entry.modified_at))
    return store.get_document(document.id)


def test_automatic_snapshot_persisted_and_clean_force_is_noop(library_store):
    store, _, _ = library_store
    initial = store.backup_status(now=NOW)
    assert initial["dirty"] and initial["due"] and initial["last_success"] is None
    result = store.run_automatic_backup(now=NOW)
    assert result["status"] == "created"
    snapshot = Path(result["path"])
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM libraries").fetchone()[0] == 1
    assert not store.backup_status(now=NOW)["dirty"]
    assert store.run_automatic_backup(now=NOW + DAY, force=True)["status"] == "clean"
    reopened = Store(store.data_dir)
    assert reopened.backup_status(now=NOW + DAY)["last_success"] == NOW
    assert not reopened.backup_status(now=NOW + DAY)["dirty"]
    assert len(list(snapshot.parent.glob("auto-*.db"))) == 1


def test_new_empty_store_does_not_create_meaningless_snapshot(tmp_path):
    store = Store(tmp_path / "empty-data")
    assert store.run_automatic_backup(now=NOW, force=True)["status"] == "clean"
    assert not (store.data_dir / "backups").exists()


def test_auto_schedule_is_utc_day_not_rolling_24_hours(library_store):
    store, _, _ = library_store
    almost_midnight = datetime(2026, 9, 15, 23, 59, tzinfo=timezone.utc).timestamp()
    assert store.run_automatic_backup(now=almost_midnight)["status"] == "created"
    store.create_tag("changed")
    assert store.run_automatic_backup(now=almost_midnight + 30)["status"] == "scheduled"
    assert store.run_automatic_backup(now=almost_midnight + 120)["status"] == "created"


def test_index_and_internal_bookkeeping_do_not_mark_backup_dirty(library_store):
    store, library, root = library_store
    document = register(store, library, root, "note.txt")
    store.run_automatic_backup(now=NOW)
    store.invalidate_document(document.id)
    store.apply_scan(library.id, (), complete=False, availability="offline")
    store.set_setting("__maintenance.hash_verified." + library.id, str(NOW))
    store.set_setting("__backup.other", "unimportant")
    assert not store.backup_status(now=NOW + DAY)["dirty"]
    store.set_favorite(document.id, True)
    assert store.backup_status(now=NOW + DAY)["dirty"]


def test_automatic_failure_preserves_success_state_and_retries(library_store, monkeypatch):
    store, _, _ = library_store
    previous = store.run_automatic_backup(now=NOW)
    old_path = Path(previous["path"])
    state_before = json.loads(store.get_setting(BACKUP_STATE_KEY))
    store.create_tag("new")
    original = store._create_snapshot
    def fail(*args, **kwargs):
        raise OSError("private path details must not leak")
    monkeypatch.setattr(store, "_create_snapshot", fail)
    failure = store.run_automatic_backup(now=NOW + DAY)
    assert failure["status"] == "failed" and failure["last_success"] == NOW
    assert "private path" not in failure["message"]
    after = json.loads(store.get_setting(BACKUP_STATE_KEY))
    assert after["fingerprint"] == state_before["fingerprint"]
    assert after["snapshots"] == state_before["snapshots"] and old_path.exists()
    monkeypatch.setattr(store, "_create_snapshot", original)
    assert store.run_automatic_backup(now=NOW + DAY + 5)["status"] == "created"
    assert not store.backup_status(now=NOW + DAY + 5)["dirty"]


def test_cancelled_snapshot_retains_old_backup_and_can_retry(library_store):
    store, _, _ = library_store
    previous = store.run_automatic_backup(now=NOW)
    store.create_tag("new")
    cancellation = threading.Event()
    cancellation.set()
    result = store.run_automatic_backup(now=NOW + DAY, cancel=cancellation)
    assert result["status"] == "failed"
    assert result["last_success"] == NOW
    assert Path(previous["path"]).exists()
    assert len(list((store.data_dir / "backups").glob("auto-*.db"))) == 1
    assert store.run_automatic_backup(now=NOW + DAY + 1)["status"] == "created"


def test_rotation_deletes_only_registered_auto_snapshots(library_store):
    store, _, _ = library_store
    protected = store.backup_metadata()
    protected_bytes = protected.read_bytes()
    folder = protected.parent
    unknown = folder / "auto-20260901T120000-00000000000000000000000000000000.db"
    unknown.write_bytes(b"unregistered user file")
    unrelated = folder / "important.txt"
    unrelated.write_text("keep", encoding="utf-8")
    recorded = []
    for day in range(10):
        store.set_setting("theme", str(day))
        result = store.run_automatic_backup(now=NOW + day * DAY)
        assert result["status"] == "created"
        recorded.append(Path(result["path"]))
    assert all(not path.exists() for path in recorded[:3])
    assert all(path.exists() for path in recorded[3:])
    assert len(json.loads(store.get_setting(BACKUP_STATE_KEY))["snapshots"]) == 7
    assert unknown.read_bytes() == b"unregistered user file"
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert protected.read_bytes() == protected_bytes


def test_backup_directory_reparse_rejected_without_touching_destination(library_store, tmp_path):
    store, _, _ = library_store
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = store.data_dir / "backups"
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = store.run_automatic_backup(now=NOW)
    assert result["status"] == "failed" and result["last_success"] is None
    assert not tuple(outside.iterdir())
    with pytest.raises(NotesError):
        store.backup_metadata()


def test_auto_snapshot_called_from_other_threads(library_store):
    store, _, _ = library_store
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: store.run_automatic_backup(now=NOW), range(3)))
    assert sorted(result["status"] for result in results) == ["clean", "clean", "created"]


def test_clock_rollback_is_due_and_newest_success_is_not_rotated(library_store):
    store, _, _ = library_store
    snapshots = []
    for day in range(7):
        store.set_setting("revision", str(day))
        snapshots.append(Path(store.run_automatic_backup(now=NOW + day * DAY)["path"]))
    store.set_setting("revision", "clock rolled back")
    status = store.backup_status(now=NOW - DAY)
    assert status["clock_skew"] and status["due"] and status["dirty"]
    latest = store.run_automatic_backup(now=NOW - DAY)
    assert latest["status"] == "created" and Path(latest["path"]).exists()
    assert not snapshots[0].exists()
    assert all(path.exists() for path in snapshots[1:])
    assert store.backup_status(now=NOW - DAY)["path"] == latest["path"]


def test_future_success_time_and_legacy_registry_migrate_safely(library_store):
    store, _, _ = library_store
    store.run_automatic_backup(now=NOW)
    state = json.loads(store.get_setting(BACKUP_STATE_KEY))
    state["last_success"] = NOW + 100 * DAY
    state.pop("registry_order")
    store.set_setting(BACKUP_STATE_KEY, json.dumps(state))
    store.create_tag("future timestamp")
    assert store.backup_status(now=NOW)["due"]
    assert store.run_automatic_backup(now=NOW)["status"] == "created"
    assert json.loads(store.get_setting(BACKUP_STATE_KEY))["registry_order"] == "append_v1"


def test_success_publication_failure_never_rotates_old_backups(library_store, monkeypatch):
    store, _, _ = library_store
    previous = store.run_automatic_backup(now=NOW)
    before = json.loads(store.get_setting(BACKUP_STATE_KEY))
    store.create_tag("changed")
    original = store.set_setting
    failed_once = False
    def fail_once(key, value):
        nonlocal failed_once
        if key == BACKUP_STATE_KEY and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("disk full")
        return original(key, value)
    monkeypatch.setattr(store, "set_setting", fail_once)
    result = store.run_automatic_backup(now=NOW + DAY)
    assert result["status"] == "failed" and result["last_success"] == NOW
    after = json.loads(store.get_setting(BACKUP_STATE_KEY))
    assert after["fingerprint"] == before["fingerprint"]
    assert after["snapshots"] == before["snapshots"]
    assert Path(previous["path"]).exists()


def test_retention_failure_does_not_claim_snapshot_failed(library_store, monkeypatch):
    store, _, _ = library_store
    def fail(_state):
        raise OSError("directory unavailable")
    monkeypatch.setattr(store, "_rotate_automatic_snapshots", fail)
    result = store.run_automatic_backup(now=NOW)
    assert result["status"] == "created" and result["last_success"] == NOW
    assert Path(result["path"]).exists()
    assert "清理" in result["message"]


def test_metadata_busy_returns_retryable_failure_and_previous_success(library_store, monkeypatch):
    store, _, _ = library_store
    store.run_automatic_backup(now=NOW)
    def fail():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "_metadata_fingerprint", fail)
    result = store.run_automatic_backup(now=NOW + DAY)
    assert result["status"] == "failed" and result["last_success"] == NOW


def test_restore_preserves_local_automatic_backup_ownership(library_store, tmp_path):
    store, _, _ = library_store
    store.run_automatic_backup(now=NOW)
    expected = json.loads(store.get_setting(BACKUP_STATE_KEY))["snapshots"]
    export = tmp_path / "restore.json"
    store.export_backup(export)
    store.create_tag("temporary")
    store.restore_backup(export)
    assert json.loads(store.get_setting(BACKUP_STATE_KEY))["snapshots"] == expected
    assert len(list((store.data_dir / "backups").glob("metadata-*.db"))) == 1


@pytest.mark.parametrize("damage", ["deleted", "truncated", "invalid_header"])
def test_lost_snapshot_is_due_even_without_metadata_changes(library_store, damage):
    store, _, _ = library_store
    result = store.run_automatic_backup(now=NOW)
    snapshot = Path(result["path"])
    original = snapshot.read_bytes()
    if damage == "deleted":
        snapshot.unlink()
    elif damage == "truncated":
        snapshot.write_bytes(original[:100])
    else:
        snapshot.write_bytes(b"x" * len(original))
    status = store.backup_status(now=NOW + 1)
    assert status["dirty"] and status["due"]
    replacement = store.run_automatic_backup(now=NOW + 1)
    assert replacement["status"] == "created"
    assert replacement["path"] != result["path"]
    assert not store.backup_status(now=NOW + 1)["dirty"]


def test_unregistered_or_replaced_sqlite_file_is_not_claimed_as_protection(library_store):
    store, _, _ = library_store
    result = store.run_automatic_backup(now=NOW)
    snapshot = Path(result["path"])
    unknown = snapshot.with_name("auto-20260915T120000-00000000000000000000000000000000.db")
    unknown.write_bytes(snapshot.read_bytes())
    original = store.get_setting(BACKUP_STATE_KEY)
    state = json.loads(original)
    state["last_path"] = str(unknown)
    store.set_setting(BACKUP_STATE_KEY, json.dumps(state))
    assert store.backup_status(now=NOW + 1)["due"]
    store.set_setting(BACKUP_STATE_KEY, original)
    os.replace(unknown, snapshot)
    # Even an identical SQLite copy is not the originally validated file.
    assert store.backup_status(now=NOW + 1)["due"]


def test_legacy_registry_uses_header_page_count_to_detect_truncation(library_store):
    store, _, _ = library_store
    result = store.run_automatic_backup(now=NOW)
    snapshot = Path(result["path"])
    state = json.loads(store.get_setting(BACKUP_STATE_KEY))
    for entry in state["snapshots"]:
        entry.pop("size_bytes")
        entry.pop("file_identity")
    store.set_setting(BACKUP_STATE_KEY, json.dumps(state))
    assert not store.backup_status(now=NOW)["dirty"]
    content = snapshot.read_bytes()
    page_size = int.from_bytes(content[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    snapshot.write_bytes(content[:page_size])
    assert store.backup_status(now=NOW + 1)["due"]


def test_healthy_snapshot_with_stale_maintenance_error_does_not_stick_failed(library_store):
    store, _, _ = library_store
    result = store.run_automatic_backup(now=NOW)
    state = json.loads(store.get_setting(BACKUP_STATE_KEY))
    state["last_error"] = "previous retention bookkeeping failed"
    store.set_setting(BACKUP_STATE_KEY, json.dumps(state))
    store._backup_failure = state["last_error"]
    status = store.backup_status(now=NOW + 1)
    assert status["status"] == "clean" and not status["dirty"]
    retry = store.run_automatic_backup(now=NOW + 1)
    assert retry["status"] == "clean" and retry["last_success"] == NOW
    assert not json.loads(store.get_setting(BACKUP_STATE_KEY))["last_error"]
    assert len(tuple(Path(result["path"]).parent.glob("auto-*.db"))) == 1


def test_second_success_bookkeeping_failure_remains_clean(library_store, monkeypatch):
    store, _, _ = library_store
    setter = store.set_setting
    count = 0
    def fail_second(key, value):
        nonlocal count
        if key == BACKUP_STATE_KEY:
            count += 1
            if count == 2:
                raise OSError("cleanup state write failed")
        return setter(key, value)
    monkeypatch.setattr(store, "set_setting", fail_second)
    created = store.run_automatic_backup(now=NOW)
    assert created["status"] == "created" and Path(created["path"]).exists()
    assert store.backup_status(now=NOW + 1)["status"] == "clean"
    assert store.run_automatic_backup(now=NOW + DAY)["status"] == "clean"


def test_retention_does_not_delete_replacement_file_at_owned_name(library_store):
    store, _, _ = library_store
    created = store.run_automatic_backup(now=NOW)
    snapshot = Path(created["path"])
    replacement = snapshot.with_name("user-content.tmp")
    replacement.write_bytes(b"not the original validated backup")
    os.replace(replacement, snapshot)
    for day in range(1, 9):
        store.set_setting("revision", str(day))
        assert store.run_automatic_backup(now=NOW + day * DAY)["status"] == "created"
    assert snapshot.read_bytes() == b"not the original validated backup"


def _junction(link, target):
    if os.name != "nt":
        pytest.skip("Windows junction validation")
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        pytest.skip("junction creation unavailable")


def test_actual_junction_backup_directory_is_rejected(library_store, tmp_path):
    store, _, _ = library_store
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    directory = store.data_dir / "backups"
    _junction(directory, outside)
    try:
        result = store.run_automatic_backup(now=NOW)
        assert result["status"] == "failed" and result["last_success"] is None
        assert tuple(outside.iterdir()) == ()
    finally:
        directory.rmdir()


def test_successful_snapshot_replaced_by_directory_junction_is_unhealthy(library_store, tmp_path):
    store, _, _ = library_store
    created = store.run_automatic_backup(now=NOW)
    snapshot = Path(created["path"])
    directory = snapshot.parent
    outside = tmp_path / "replacement-backups"
    outside.mkdir()
    moved_snapshot = outside / snapshot.name
    snapshot.rename(moved_snapshot)
    directory.rmdir()
    _junction(directory, outside)
    try:
        status = store.backup_status(now=NOW + 1)
        assert status["dirty"] and status["due"]
        assert store.run_automatic_backup(now=NOW + 1)["status"] == "failed"
        assert moved_snapshot.exists()
    finally:
        directory.rmdir()


def test_manual_relocation_preserves_identity_and_annotations(library_store):
    store, library, root = library_store
    document = register(store, library, root, "old.txt", "previous body")
    tag = store.create_tag("keep")
    store.set_document_tags(document.id, (tag.id,))
    store.set_favorite(document.id, True)
    store.mark_opened(document.id)
    store.set_encoding(document.id, "utf-8")
    original = store.get_document(document.id)
    (root / "old.txt").unlink()
    target = root / "new.txt"
    target.write_text("replacement bytes", encoding="utf-8")
    before = target.read_bytes()
    plan = store.inspect_relocation(document.id, "new.txt")
    assert not plan["requires_merge_confirmation"]
    relocated = store.relocate_document(document.id, "new.txt")
    assert relocated.id == document.id and relocated.relative_path == "new.txt"
    assert relocated.tags == (tag,) and relocated.is_favorite
    assert relocated.last_open_requested_at == original.last_open_requested_at
    assert relocated.encoding_override == "utf-8"
    assert relocated.content_generation == original.content_generation + 1
    assert relocated.index_status == "pending" and relocated.availability == "present"
    assert target.read_bytes() == before and not (root / "old.txt").exists()
    assert store.search(SearchQuery(text="previous body")).total == 0


def test_relocation_requires_explicit_placeholder_merge_confirmation(library_store):
    store, library, root = library_store
    old = register(store, library, root, "old.txt", "old original")
    target = register(store, library, root, "new.txt", "new original")
    (root / "old.txt").unlink()
    plan = store.inspect_relocation(old.id, "new.txt")
    assert plan["requires_merge_confirmation"] and plan["target_document_id"] == target.id
    with pytest.raises(NotesError, match="确认"):
        store.relocate_document(old.id, "new.txt")
    with pytest.raises(NotesError, match="确认"):
        store.relocate_document(old.id, "new.txt", merge_unannotated="false")
    assert len(store.list_documents()) == 2
    relocated = store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    assert relocated.id == old.id and len(store.list_documents()) == 1
    with pytest.raises(NotesError):
        store.get_document(target.id)
    assert store.search(SearchQuery(text="original")).total == 0


@pytest.mark.parametrize("annotation", ["tag", "favorite", "opened", "encoding"])
def test_annotated_target_never_absorbed(library_store, annotation):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    target = register(store, library, root, "new.txt")
    (root / "old.txt").unlink()
    if annotation == "tag":
        tag = store.create_tag("target")
        store.set_document_tags(target.id, (tag.id,))
    elif annotation == "favorite":
        store.set_favorite(target.id, True)
    elif annotation == "opened":
        store.mark_opened(target.id)
    else:
        store.set_encoding(target.id, "gb18030")
    before_old, before_target = store.get_document(old.id), store.get_document(target.id)
    with pytest.raises(NotesError):
        store.inspect_relocation(old.id, "new.txt")
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    assert store.get_document(old.id) == before_old
    assert store.get_document(target.id) == before_target


def test_relocation_revalidates_after_inspection(library_store):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    target = register(store, library, root, "new.txt")
    (root / "old.txt").unlink()
    store.inspect_relocation(old.id, "new.txt")
    store.set_favorite(target.id, True)
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    store.set_favorite(target.id, False)
    (root / "old.txt").write_text("reappeared", encoding="utf-8")
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    assert len(store.list_documents()) == 2
    assert (root / "old.txt").read_text(encoding="utf-8") == "reappeared"


@pytest.mark.parametrize("target", ["../escape.txt", "C:/escape.txt", "bad.bin", ".git/new.txt"])
def test_relocation_rejects_escape_and_out_of_scope(library_store, target):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    (root / "old.txt").unlink()
    with pytest.raises(NotesError):
        store.relocate_document(old.id, target, merge_unannotated=True)
    assert store.get_document(old.id).relative_path == "old.txt"


def test_relocation_rejects_old_permission_failure_not_missing(library_store, monkeypatch):
    import notes.infrastructure.files as files
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    register(store, library, root, "new.txt")
    original = files.validate_document_path
    def inaccessible(root, relative, must_exist=True):
        if relative == "old.txt":
            raise NotesError("unavailable", "permission denied")
        return original(root, relative, must_exist)
    monkeypatch.setattr(files, "validate_document_path", inaccessible)
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    assert store.get_document(old.id).relative_path == "old.txt"


def test_relocation_missing_root_is_not_missing_old_file(library_store):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    (root / "old.txt").unlink()
    root.rmdir()
    with pytest.raises(NotesError):
        store.inspect_relocation(old.id, "new.txt")


def test_relocation_rejects_link_target(library_store, tmp_path):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    (root / "old.txt").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not alter", encoding="utf-8")
    try:
        (root / "new.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt")
    assert outside.read_text(encoding="utf-8") == "do not alter"


def test_relocation_revalidates_inside_transaction(library_store, monkeypatch):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    target = register(store, library, root, "new.txt")
    (root / "old.txt").unlink()
    original = store._relocation_plan
    calls = 0
    def reappearing(*args, **kwargs):
        nonlocal calls
        plan = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            (root / "old.txt").write_text("concurrent new file", encoding="utf-8")
        return plan
    monkeypatch.setattr(store, "_relocation_plan", reappearing)
    with pytest.raises(NotesError):
        store.relocate_document(old.id, "new.txt", merge_unannotated=True)
    assert store.get_document(old.id).relative_path == "old.txt"
    assert store.get_document(target.id).relative_path == "new.txt"


def test_relocation_actual_junction_target_rejected(library_store, tmp_path):
    store, library, root = library_store
    old = register(store, library, root, "old.txt")
    (root / "old.txt").unlink()
    outside = tmp_path / "outside-relocation"
    outside.mkdir()
    target = outside / "new.txt"
    target.write_text("safe bytes", encoding="utf-8")
    link = root / "link"
    _junction(link, outside)
    try:
        with pytest.raises(NotesError):
            store.relocate_document(old.id, "link/new.txt")
        assert target.read_text(encoding="utf-8") == "safe bytes"
    finally:
        link.rmdir()
