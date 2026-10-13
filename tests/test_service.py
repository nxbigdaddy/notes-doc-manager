from __future__ import annotations

import hashlib
import threading

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError


@pytest.fixture
def setup_service(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "正文 & test.txt").write_text("中文关键词\n原始内容 %_* 🚀", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(docs)})
        service.call("scan", {"library_id": library.id})
        yield service, library, docs
    finally:
        service.close()


def test_complete_non_destructive_roundtrip(setup_service, tmp_path):
    service, library, docs = setup_service
    path = docs / "正文 & test.txt"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = service.call("query", {"text": "中文", "mode": "content"})
    assert result.total == 1 and result.complete
    document = result.items[0].document
    tag = service.call("create_tag", {"name": "工作"})
    service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
    service.call("set_favorite", {"document_id": document.id, "value": True})
    preview = service.call("preview", {"document_id": document.id})
    assert preview.read.status == "ready" and "原始内容" in preview.read.text
    backup = tmp_path / "metadata.json"
    service.call("export_backup", {"path": str(backup)})
    checked = service.call("inspect_backup", {"path": str(backup)})
    service.call("restore_backup", {
        "path": str(backup), "confirmed": True,
        "expected_backup": checked["signature"], "expected_current": checked["current_signature"],
    })
    service.call("scan", {})
    result = service.call("query", {"tag_ids": [tag.id], "favorites_only": True})
    assert result.total == 1
    assert result.items[0].document.id == document.id
    assert service.call("state").editor_path == ""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_new_document_never_overwrites_and_folders_include_empty(setup_service):
    service, library, docs = setup_service
    (docs / "empty").mkdir()
    assert "empty" in service.call("folders", {"library_id": library.id})
    payload = {"library_id": library.id, "relative_dir": "empty", "filename": "新建.md"}
    created = service.call("create_document", payload)
    assert created.file_name == "新建.md"
    target = docs / "empty" / "新建.md"
    target.write_text("不能覆盖", encoding="utf-8")
    with pytest.raises((NotesError, FileExistsError)):
        service.call("create_document", payload)
    assert target.read_text(encoding="utf-8") == "不能覆盖"


def test_preview_encoding_override_reindexes(setup_service):
    service, library, docs = setup_service
    path = docs / "gbk.txt"
    path.write_bytes("编码待确认内容".encode("gbk"))
    service.call("scan", {})
    doc = next(d for d in service.store.list_documents() if d.file_name == "gbk.txt")
    assert doc.index_status == "needs_encoding"
    service.call("set_encoding", {"document_id": doc.id, "encoding": "gbk"})
    result = service.call("query", {"text": "确认", "mode": "content"})
    assert result.total == 1


def test_mutations_revision_but_queries_do_not_emit(setup_service):
    service, _, _ = setup_service
    before = service.call("state").data_revision
    service.call("query", {})
    assert service.call("state").data_revision == before
    service.call("create_tag", {"name": "标签"})
    assert service.call("state").data_revision > before


def test_disabled_library_excluded_without_removing_source(setup_service):
    service, library, docs = setup_service
    service.call("set_library_enabled", {"library_id": library.id, "enabled": False})
    assert service.call("query", {}).total == 0
    assert (docs / "正文 & test.txt").exists()
    service.call("set_library_enabled", {"library_id": library.id, "enabled": True})
    service.call("scan", {})
    assert service.call("query", {}).total == 1


def test_backup_safety_and_confirmation(setup_service, tmp_path):
    service, _, docs = setup_service
    with pytest.raises(NotesError):
        service.call("export_backup", {"path": str(docs / "bad.json")})
    path = tmp_path / "backup.json"
    service.call("export_backup", {"path": str(path)})
    before = path.read_bytes()
    with pytest.raises(NotesError):
        service.call("export_backup", {"path": str(path)})
    assert path.read_bytes() == before
    with pytest.raises(NotesError):
        service.call("restore_backup", {"path": str(path)})


def test_callbacks_errors_and_closed_service(setup_service):
    service, _, _ = setup_service
    done = threading.Event()
    errors = []

    def fail(error):
        errors.append(error)
        done.set()

    service.submit("not_real", {}, lambda _: None, fail)
    assert done.wait(5)
    assert errors[0].code == "unknown_command"
    service.close()
    with pytest.raises(NotesError):
        service.call("state")


def test_real_monitor_updates_saved_file(tmp_path):
    import time

    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "monitored.txt"
    path.write_text("before", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=True)
    try:
        lib = service.call("add_library", {"root_path": str(docs)})
        service.call("scan", {"library_id": lib.id})
        path.write_text("中文监听已保存的新内容", encoding="utf-8")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = service.call("query", {"text": "监听", "mode": "content"})
            if result.total == 1:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Watcher-driven index did not converge")
    finally:
        service.close()


def test_single_instance_ownership_and_activation(tmp_path):
    from notes.infrastructure.instance import SingleInstance

    import os

    if os.name != "nt":
        pytest.skip("Windows named mutex/event")
    first = SingleInstance(tmp_path)
    second = SingleInstance(tmp_path)
    try:
        assert first.primary and not second.primary
        assert first.activation_requested()
    finally:
        second.close()
        first.close()
