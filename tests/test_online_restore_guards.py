"""Online confirmation binds two snapshots; all mutations use isolated fixtures."""
import hashlib
import json
import os
import re

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError


@pytest.fixture
def case(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    source = documents / "source.txt"
    source.write_bytes(b"synthetic immutable document")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    library = service.call("add_library", {"root_path": str(documents)})
    service.call("scan")
    service.call("create_tag", {"name": "Backup-A"})
    backup = tmp_path / "candidate.json"
    service.call("export_backup", {"path": str(backup)})
    service.call("create_tag", {"name": "Current-only"})
    try:
        yield service, library, source, backup
    finally:
        service.close()


def inspect(service, backup):
    checked = service.call("inspect_backup", {"path": str(backup)})
    return {"path": str(backup), "confirmed": True, "expected_backup": checked["signature"],
            "expected_current": checked["current_signature"]}


def names(service):
    return {item.name for item in service.store.list_tags()}


def replace_candidate(backup):
    payload = json.loads(backup.read_text(encoding="utf-8"))
    payload["tags"][0]["name"] = "Backup-B"
    payload["tags"][0]["name_key"] = "backup-b"
    replacement = backup.with_name("replacement.json")
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(replacement, backup)


def test_inspection_binds_stable_export_renamed_file_without_writing_current(case):
    service, _, source, backup = case
    before = source.read_bytes()
    a, b = inspect(service, backup), inspect(service, backup)
    assert a == b
    assert re.fullmatch("[0-9a-f]{64}", a["expected_backup"])
    assert re.fullmatch("[0-9a-f]{64}", a["expected_current"])
    assert not list(service.store.data_dir.glob("backups/*.db"))
    service.call("restore_backup", a)
    assert names(service) == {"Backup-A"}
    assert source.read_bytes() == before
    assert len(list(service.store.data_dir.glob("backups/metadata-*.db"))) == 1


@pytest.mark.parametrize("kind", ["replacement", "same_size_same_mtime", "different_path"])
def test_changed_candidate_is_rejected_even_if_summary_counts_match(case, kind):
    service, _, source, backup = case
    confirmed = inspect(service, backup)
    if kind == "replacement":
        replace_candidate(backup)
    elif kind == "same_size_same_mtime":
        info = backup.stat()
        data = backup.read_bytes().replace(b"Backup-A", b"Backup-B")
        backup.write_bytes(data)
        os.utime(backup, ns=(info.st_atime_ns, info.st_mtime_ns))
    else:
        second = backup.with_name("different.json")
        second.write_bytes(backup.read_bytes())
        confirmed["path"] = str(second)
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == "backup_changed"
    assert names(service) == {"Backup-A", "Current-only"}
    assert not list(service.store.data_dir.glob("backups/*.db"))
    assert source.read_bytes() == b"synthetic immutable document"


@pytest.mark.parametrize("change", ["tag", "favorite", "smart_list", "setting", "opened"])
def test_new_management_information_after_inspection_is_not_overwritten(case, change):
    service, library, source, backup = case
    confirmed = inspect(service, backup)
    document = service.store.list_documents(library.id)[0]
    if change == "tag":
        service.call("create_tag", {"name": "New-after-inspection"})
    elif change == "favorite":
        service.store.set_favorite(document.id, True)
    elif change == "smart_list":
        service.call("create_smart_list", {"name": "New-after-inspection", "query": {}})
    elif change == "setting":
        service.store.set_setting("quick_open_hotkey_enabled", "1")  # Never register a real hotkey.
    else:
        service.store.mark_opened(document.id)  # No editor is launched.
    fingerprint = service.store._metadata_fingerprint()[0]
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == "metadata_changed"
    assert service.store._metadata_fingerprint()[0] == fingerprint
    assert source.read_bytes() == b"synthetic immutable document"


@pytest.mark.parametrize("field,value", [
    ("expected_backup", None), ("expected_current", None), ("expected_backup", ""),
    ("expected_backup", True), ("expected_current", 1), ("expected_backup", "not-a-signature"),
])
def test_confirmed_true_without_valid_binding_is_not_enough(case, field, value):
    service, _, _, backup = case
    confirmed = inspect(service, backup)
    confirmed[field] = value
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == "confirmation_required"
    assert "Current-only" in names(service)


@pytest.mark.parametrize("change", ["backup", "current"])
def test_changes_during_protective_copy_are_rechecked_before_transaction_mutations(case, monkeypatch, change):
    service, _, source, backup = case
    confirmed = inspect(service, backup)
    original = service.store.backup_metadata

    def protective_copy():
        result = original()
        if change == "backup":
            replace_candidate(backup)
        else:
            service.store.create_tag("During-protection")
        return result

    monkeypatch.setattr(service.store, "backup_metadata", protective_copy)
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == ("backup_changed" if change == "backup" else "metadata_changed")
    assert "Current-only" in names(service)
    assert ("During-protection" in names(service)) == (change == "current")
    assert source.read_bytes() == b"synthetic immutable document"
    assert len(list(service.store.data_dir.glob("backups/metadata-*.db"))) == 1


def test_derived_fields_and_internal_backup_bookkeeping_do_not_invalidate_confirmation(case):
    service, _, _, backup = case
    confirmed = inspect(service, backup)
    with service.store._connect() as connection:
        connection.execute("UPDATE documents SET modified_at=modified_at+1,content_generation=content_generation+1")
        connection.execute("UPDATE libraries SET availability='offline'")
    service.store.set_setting("__maintenance.synthetic", "changed")
    assert service.store._metadata_fingerprint()[0] == confirmed["expected_current"]
    service.call("restore_backup", confirmed)
    assert names(service) == {"Backup-A"}


def test_protection_failure_keeps_live_management(case, monkeypatch):
    service, _, _, backup = case
    confirmed = inspect(service, backup)

    def failed():
        raise OSError("controlled protection failure")

    monkeypatch.setattr(service.store, "backup_metadata", failed)
    with pytest.raises(OSError):
        service.call("restore_backup", confirmed)
    assert names(service) == {"Backup-A", "Current-only"}


def test_index_failure_after_commit_is_reported_as_restored_and_refreshes_watches(case, monkeypatch):
    service, _, source, backup = case
    confirmed = inspect(service, backup)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    refreshed = []

    def failed():
        raise OSError("controlled post-commit index failure")

    monkeypatch.setattr(service.store, "rebuild_index", failed)
    monkeypatch.setattr(service, "_refresh_watches", lambda: refreshed.append(True))
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == "restored_index_pending"
    assert "已恢复" in error.value.message and "不要重复恢复" in error.value.message
    assert names(service) == {"Backup-A"} and refreshed == [True]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert len(list(service.store.data_dir.glob("backups/metadata-*.db"))) == 1


def test_status_failure_after_commit_still_reports_restored(case, monkeypatch):
    service, _, _, backup = case
    confirmed = inspect(service, backup)
    original = service._notify

    def notification_failed(status=None, busy=None):
        if status and status.startswith("管理信息已恢复"):
            raise OSError("controlled post-commit status failure")
        return original(status, busy)

    monkeypatch.setattr(service, "_notify", notification_failed)
    with pytest.raises(NotesError) as error:
        service.call("restore_backup", confirmed)
    assert error.value.code == "restored_followup_failed"
    assert "已恢复" in error.value.message and "不要重复恢复" in error.value.message
    assert names(service) == {"Backup-A"}
