from datetime import datetime, timezone
import sqlite3

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError
from notes.domain.templates import render_template
from notes.infrastructure import editor, files


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    clock = [datetime(2026, 9, 16, 8, 30, tzinfo=timezone.utc).timestamp()]
    service = ApplicationService(tmp_path / "data", monitoring=False, wall_clock=lambda: clock[0])
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {})
        yield service, library, root, clock
    finally:
        service.close()


@pytest.mark.parametrize("template_id", ["meeting", "daily", "troubleshooting"])
def test_preview_and_created_content_match_for_same_time(managed, template_id, monkeypatch):
    service, library, root, _ = managed
    opened = []
    monkeypatch.setattr(editor, "open_document", lambda *_: opened.append(True))
    preview = service.call("preview_template", {"template_id": template_id, "filename": "中文标题.md"})
    document = service.call(
        "create_document",
        {"library_id": library.id, "relative_dir": "", "filename": "中文标题.md",
         "template_id": template_id},
    )
    content = (root / "中文标题.md").read_bytes()
    assert content == preview.text.encode("utf-8")
    assert not content.startswith(b"\xef\xbb\xbf") and b"\r" not in content
    assert document.index_status == "ready" and document.size_bytes == len(content)
    assert service.call("query", {"text": "中文标题", "mode": "content"}).total == 1
    assert opened == []  # Creation alone never executes the editor.


def test_default_creation_keeps_empty_file_and_does_not_overwrite(managed):
    service, library, root, _ = managed
    payload = {"library_id": library.id, "relative_dir": "", "filename": "empty.txt"}
    service.call("create_document", payload)
    path = root / "empty.txt"
    assert path.read_bytes() == b""
    path.write_bytes(b"existing user content")
    with pytest.raises(NotesError):
        service.call("create_document", dict(payload, template_id="meeting"))
    assert path.read_bytes() == b"existing user content"


def test_creation_renders_again_at_actual_creation_time(managed):
    service, library, root, clock = managed
    name = "跨日{{date}}.txt"
    before = service.call("preview_template", {"template_id": "daily", "filename": name})
    clock[0] += 86400
    service.call(
        "create_document",
        {"library_id": library.id, "relative_dir": "", "filename": name, "template_id": "daily"},
    )
    expected = render_template("daily", "跨日{{date}}", datetime.fromtimestamp(clock[0]).astimezone())
    assert (root / name).read_bytes() == expected.text.encode("utf-8")
    assert before.text != expected.text and "{{date}}" in expected.text


@pytest.mark.parametrize("template_id", ["unknown", "../meeting", "", False, None, 1])
def test_unknown_template_never_creates_a_file(managed, template_id):
    service, library, root, _ = managed
    with pytest.raises(NotesError):
        service.call(
            "create_document",
            {"library_id": library.id, "relative_dir": "", "filename": "none.md",
             "template_id": template_id},
        )
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape.md", "a/b.txt", "CON.txt", "bad\x00.txt", "\ud800.txt"])
def test_invalid_preview_filename_has_no_filesystem_side_effect(managed, name):
    service, _, root, _ = managed
    with pytest.raises(NotesError):
        service.call("preview_template", {"template_id": "meeting", "filename": name})
    assert list(root.iterdir()) == []


def test_partial_creation_is_preserved_and_registered_without_repeating_write(managed, monkeypatch):
    service, library, root, _ = managed
    calls = []

    def partial(_root, relative_dir, filename, **kwargs):
        calls.append(filename)
        (_root / filename).write_bytes(b"partial file")
        raise NotesError("created_partial", "文件已创建但模板写入未完成")

    monkeypatch.setattr(files, "create_document", partial)
    with pytest.raises(NotesError) as error:
        service.call(
            "create_document",
            {"library_id": library.id, "relative_dir": "", "filename": "partial.md",
             "template_id": "meeting"},
        )
    assert error.value.code == "created_partial"
    service.call("state")  # Barrier after the one scheduled repair scan.
    assert calls == ["partial.md"] and (root / "partial.md").read_bytes() == b"partial file"
    document = service.store.list_documents(library.id)[0]
    assert document.file_name == "partial.md"


def test_template_list_is_finite_and_has_no_user_supplied_code(managed):
    service, _, _, _ = managed
    templates = service.call("list_templates")
    assert {item.id for item in templates} == {"blank", "meeting", "daily", "troubleshooting"}
    assert len(templates) == 4


@pytest.mark.parametrize("template_id", ["blank", "meeting"])
def test_creation_rejects_excluded_directory_before_writing(managed, template_id):
    service, library, root, _ = managed
    directory = root / ".GIT"
    directory.mkdir()
    with pytest.raises(NotesError) as error:
        service.call(
            "create_document",
            {"library_id": library.id, "relative_dir": ".GIT", "filename": "excluded.md",
             "template_id": template_id},
        )
    assert error.value.code == "out_of_scope"
    assert list(directory.iterdir()) == []
    assert service.store.list_documents(library.id) == ()


def test_registration_failure_retains_path_and_schedules_repair(managed, monkeypatch):
    service, library, root, _ = managed
    original_scan = service.store.apply_scan
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("controlled metadata write failure")
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(service.store, "apply_scan", fail_once)
    with pytest.raises(NotesError) as error:
        service.call(
            "create_document",
            {"library_id": library.id, "filename": "preserved.md", "template_id": "meeting"},
        )
    path = root / "preserved.md"
    assert error.value.code == "created_not_registered"
    assert str(path) in error.value.message
    assert "参与人员" in path.read_text(encoding="utf-8")
    content = path.read_bytes()
    service.call("state")  # Wait for the one queued repair; monitoring is disabled.
    document = service.store.list_documents(library.id)[0]
    assert document.file_name == path.name
    assert path.read_bytes() == content


@pytest.mark.parametrize("failing_step", ["index", "notification"])
def test_post_creation_failure_does_not_request_repeat_creation(managed, monkeypatch, failing_step):
    service, library, root, _ = managed
    operation = "set_index" if failing_step == "index" else "_notify"
    owner = service.store if failing_step == "index" else service
    original = getattr(owner, operation)
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("controlled post-creation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, operation, fail_once)
    with pytest.raises(NotesError) as error:
        service.call(
            "create_document",
            {"library_id": library.id, "filename": "completed.md", "template_id": "meeting"},
        )
    path = root / "completed.md"
    assert error.value.code == "created_followup_failed"
    assert str(path) in error.value.message and "不要重复创建" in error.value.message
    content = path.read_bytes()
    assert "参与人员" in content.decode("utf-8")
    service.call("state")
    assert len(service.store.list_documents(library.id)) == 1
    assert path.read_bytes() == content
    assert service.call("query", {"text": "completed", "mode": "content"}).total == 1


def test_repair_scheduling_error_does_not_hide_partial_creation(managed, monkeypatch):
    service, library, root, _ = managed

    def partial(_root, relative_dir, filename, **kwargs):
        (_root / filename).write_bytes(b"partial")
        raise NotesError("created_partial", "保留文件：" + str(_root / filename))

    def schedule_failed(*args, **kwargs):
        raise RuntimeError("controlled repair scheduling failure")

    monkeypatch.setattr(files, "create_document", partial)
    monkeypatch.setattr(service, "_schedule_scan", schedule_failed)
    with pytest.raises(NotesError) as error:
        service.call(
            "create_document",
            {"library_id": library.id, "filename": "partial.md", "template_id": "daily"},
        )
    assert error.value.code == "created_partial"
    assert (root / "partial.md").read_bytes() == b"partial"
    assert library.id in service._dirty
