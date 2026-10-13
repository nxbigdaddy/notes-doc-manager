import hashlib
import json
import sqlite3
import threading

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError


@pytest.fixture
def library_service(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "meeting.md").write_text("智能组合会议文件", encoding="utf-8")
    (root / "other.md").write_text("其他文档", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {})
        yield service, library, root
    finally:
        service.close()


def make_list(service, library, **extra):
    return service.call("create_smart_list", {
        "name": "常用组合", "query": {"library_id": library.id, "text": "会议",
                                 "mode": "content", **extra},
    })


def test_saved_combination_reopens_exports_restores_without_body_changes(library_service, tmp_path):
    service, library, root = library_service
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    tag = service.call("create_tag", {"name": "项目"})
    hit = service.call("query", {"text": "meeting", "mode": "name"}).items[0].document
    service.call("set_document_tags", {"document_id": hit.id, "tag_ids": [tag.id]})
    service.call("set_favorite", {"document_id": hit.id, "value": True})
    saved = make_list(service, library, tag_ids=[tag.id], favorites_only=True)
    assert service.call("state").smart_lists == (saved,)
    assert service.call("query", {"smart_list_id": saved.id}).total == 1
    backup = tmp_path / "saved-filters.json"
    service.call("export_backup", {"path": str(backup)})
    assert json.loads(backup.read_text(encoding="utf-8"))["format_version"] == 2
    service.call("delete_smart_list", {"id": saved.id, "confirmed": True})
    assert service.call("state").smart_lists == ()
    checked = service.call("inspect_backup", {"path": str(backup)})
    service.call("restore_backup", {
        "path": str(backup), "confirmed": True,
        "expected_backup": checked["signature"], "expected_current": checked["current_signature"],
    })
    service.call("state")  # All restore-triggered scans precede this barrier.
    assert service.call("query", {"smart_list_id": saved.id}).total == 1
    data_dir = service.store.data_dir
    service.close()
    reopened = ApplicationService(data_dir, monitoring=False)
    try:
        assert reopened.call("state").smart_lists[0].id == saved.id
        assert reopened.call("query", {"smart_list_id": saved.id}).total == 1
    finally:
        reopened.close()
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}


def test_deleted_tag_and_disabled_library_cannot_broaden_saved_scope(library_service):
    service, library, _ = library_service
    tag = service.call("create_tag", {"name": "短期标签"})
    saved = make_list(service, library, tag_ids=[tag.id])
    service.call("delete_tag", {"tag_id": tag.id})
    state = service.call("state")
    assert not state.smart_lists[0].available
    assert state.smart_lists[0].query.tag_ids == (tag.id,)
    with pytest.raises(NotesError):
        service.call("query", {"smart_list_id": saved.id})
    updated = service.call("update_smart_list", {
        "id": saved.id, "name": "仍然限定文档库", "query": {"library_id": library.id},
    })
    assert updated.available
    service.call("set_library_enabled", {"library_id": library.id, "enabled": False})
    with pytest.raises(NotesError):
        service.call("query", {"smart_list_id": saved.id})


@pytest.mark.parametrize("extra", [{"text": ""}, {"tag_ids": []}, {"library_id": None},
                                  {"include_unavailable": True}, {"mode": "all"}])
def test_client_cannot_override_saved_query(library_service, extra):
    service, library, _ = library_service
    saved = make_list(service, library)
    with pytest.raises(NotesError) as error:
        service.call("query", {"smart_list_id": saved.id, **extra})
    assert error.value.code == "invalid_smart_list"


@pytest.mark.parametrize("confirmed", [None, False, "true", 1])
def test_delete_requires_literal_confirmation(library_service, confirmed):
    service, library, root = library_service
    saved = make_list(service, library)
    with pytest.raises(NotesError):
        service.call("delete_smart_list", {"id": saved.id, "confirmed": confirmed})
    assert service.call("state").smart_lists[0].id == saved.id
    assert len(list(root.iterdir())) == 2


def test_runtime_bad_list_reports_warning_without_reset_or_blocking_normal_query(library_service):
    service, library, _ = library_service
    make_list(service, library)
    with sqlite3.connect(service.store.metadata_path) as connection:
        connection.execute("UPDATE smart_lists SET query_json='bad saved data'")
    state = service.call("state")
    assert state.smart_lists_warning and state.libraries
    assert service.call("query", {"mode": "name"}).total == 2
    with sqlite3.connect(service.store.metadata_path) as connection:
        assert connection.execute("SELECT query_json FROM smart_lists").fetchone()[0] == "bad saved data"


def test_smart_query_uses_main_cancellation_channel(library_service, monkeypatch):
    service, library, _ = library_service
    saved = make_list(service, library)
    seen = []
    entered = threading.Event()
    release = threading.Event()
    original = service.store.search_smart_list

    def blocked(*args, **kwargs):
        seen.append(kwargs["cancel"])
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "search_smart_list", blocked)
    future = service.submit("query", {"smart_list_id": saved.id})
    try:
        assert entered.wait(5)
        service.call("cancel_query", {"channel": "quick"})
        assert not seen[0].is_set()
        service.call("cancel_query", {"channel": "main"})
        assert seen[0].is_set()
    finally:
        release.set()
    result = future.result(timeout=5)
    assert not result.complete


def test_reference_validation_and_search_share_gate_with_tag_deletion(library_service, monkeypatch):
    service, library, _ = library_service
    tag = service.call("create_tag", {"name": "并发范围"})
    document = service.call("query", {"text": "meeting", "mode": "name"}).items[0].document
    service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
    saved = make_list(service, library, tag_ids=[tag.id])
    entered = threading.Event()
    release = threading.Event()
    delete_entered = threading.Event()
    original_search = service.store.search
    original_delete = service.store.delete_tag

    def paused_search(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_search(*args, **kwargs)

    def watched_delete(*args, **kwargs):
        delete_entered.set()
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(service.store, "search", paused_search)
    monkeypatch.setattr(service.store, "delete_tag", watched_delete)
    query = service.submit("query", {"smart_list_id": saved.id})
    deletion = None
    try:
        assert entered.wait(5)  # Saved query has already validated its references.
        deletion = service.submit("delete_tag", {"tag_id": tag.id})
        assert delete_entered.wait(5)
        assert not deletion.done()  # Store gate spans reference check and search.
    finally:
        release.set()
    assert query.result(timeout=5).total == 1
    assert deletion is not None
    deletion.result(timeout=5)
    with pytest.raises(NotesError):
        service.call("query", {"smart_list_id": saved.id})
