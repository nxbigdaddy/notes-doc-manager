import sqlite3

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError
from notes.infrastructure import editor, files


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    path = root / "note.txt"
    path.write_text("original text", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {})
        document = service.store.list_documents(library.id)[0]
        service.store.set_setting("editor_path", "not-used-in-mocked-launch.exe")
        yield service, library, document, path
    finally:
        service.close()


@pytest.mark.parametrize("action", ["open_document", "preview"])
@pytest.mark.parametrize("invalidated", ["disabled", "excluded"])
def test_stale_document_selection_cannot_bypass_scope(managed, monkeypatch, action, invalidated):
    service, library, document, path = managed
    calls = []
    monkeypatch.setattr(editor, "open_document", lambda *_args, **_kwargs: calls.append("editor"))
    monkeypatch.setattr(files, "read_document", lambda *_args, **_kwargs: calls.append("read"))
    if invalidated == "disabled":
        service.store.set_library_enabled(library.id, False)
    else:
        service.store.update_library_rules(library.id, (".md",), ())
    with pytest.raises(NotesError) as error:
        service.call(action, {"document_id": document.id})
    assert error.value.code in {"disabled", "out_of_scope"}
    assert calls == [] and path.read_text(encoding="utf-8") == "original text"


def test_launched_editor_is_not_reported_as_failed_if_recent_record_write_fails(managed, monkeypatch):
    service, _, document, path = managed
    calls = []
    monkeypatch.setattr(editor, "open_document", lambda *_args, **_kwargs: calls.append("requested"))

    def denied(_):
        raise sqlite3.OperationalError("test readonly")

    monkeypatch.setattr(service.store, "mark_opened", denied)
    assert service.call("open_document", {"document_id": document.id}) is None
    assert calls == ["requested"]
    assert "已请求" in service.call("state").status
    assert "未能保存" in service.call("state").status
    assert service.store.get_document(document.id).last_open_requested_at is None
    assert path.read_text(encoding="utf-8") == "original text"
