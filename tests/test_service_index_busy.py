"""A transient cache writer must not disable search or strand the scan UI state."""
import sqlite3

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError


def test_busy_scan_keeps_cache_healthy_and_can_retry_without_rebuild(tmp_path, monkeypatch):
    documents = tmp_path / "documents"
    documents.mkdir()
    path = documents / "note.txt"
    path.write_bytes(b"before-token")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(documents)})
        service.call("scan", {"library_id": library.id})
        document = service.store.list_documents(library.id)[0]
        tag = service.call("create_tag", {"name": "Preserve management"})
        service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
        service.call("set_favorite", {"document_id": document.id, "value": True})
        verified_at = service.store.get_setting(f"__maintenance.hash_verified.{library.id}")
        states = []
        service.subscribe(states.append)
        original_connect = service.store._new_connection

        def short_wait(database=None):
            connection = original_connect(database)
            if database == service.store.search_path:
                connection.execute("PRAGMA busy_timeout=40")
            return connection

        monkeypatch.setattr(service.store, "_new_connection", short_wait)
        path.write_bytes(b"after-token")
        blocker = sqlite3.connect(service.store.search_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(NotesError) as failed:
                service.call("scan", {"library_id": library.id, "force": True})
            assert failed.value.code == "database_busy"
            assert service.store.fts_available and not service.store.search_warning
            assert states and not states[-1].busy and "未完成" in states[-1].status
            assert not service.call("state").busy
            current = service.store.get_document(document.id)
            assert current.index_status == "pending"
            assert current.is_favorite and current.tags == (tag,)
            assert service.store.get_setting(f"__maintenance.hash_verified.{library.id}") == verified_at
            assert service.call("query", {"text": "after-token", "mode": "content"}).total == 0
            assert service.call("query", {"text": "note", "mode": "name"}).total == 1
            assert path.read_bytes() == b"after-token"
        finally:
            blocker.rollback()
            blocker.close()

        def forbidden_rebuild():
            pytest.fail("A transient cache lock must not require rebuilding the index")

        monkeypatch.setattr(service.store, "rebuild_index", forbidden_rebuild)
        result = service.call("scan", {"library_id": library.id})
        assert result["failures"] == 0 and result["indexed"] == 1
        assert service.store.get_document(document.id).index_status == "ready"
        assert service.call("query", {"text": "after-token", "mode": "content"}).total == 1
        assert service.call("query", {"text": "before-token", "mode": "content"}).total == 0
        assert path.read_bytes() == b"after-token"
    finally:
        service.close()


def test_unavailable_cache_does_not_count_ready_reads_as_successful_indexes(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    path = documents / "note.txt"
    path.write_bytes(b"must remain ordinary text")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.store.add_library(documents)
        service.store.fts_available = False
        service.store.search_warning = "Controlled unavailable cache"
        result = service.call("scan", {"library_id": library.id})
        assert result["indexed"] == 0 and result["failures"] == 1
        assert service.store.list_documents(library.id)[0].index_status == "error"
        assert service.call("query", {"text": "note", "mode": "name"}).total == 1
        assert path.read_bytes() == b"must remain ordinary text"
    finally:
        service.close()
