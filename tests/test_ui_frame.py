"""Hidden native widget tests, opt in with NOTES_GUI_TESTS=1 on desktop Windows."""
import os
from concurrent.futures import Future
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden wx desktop tests")


class FakeService:
    def __init__(self):
        self.requests = []

    def subscribe(self, callback):
        self.subscriber = callback

    def submit(self, command, payload, success, error):
        future = Future()
        self.requests.append((command, payload, success, error, future))
        return future

    def close(self):
        pass


@pytest.fixture
def frame():
    import wx
    from notes.ui.main_frame import MainFrame
    app = wx.App(False)
    service = FakeService()
    instance = MainFrame(service)
    yield instance, service
    instance._closing = True
    instance._debounce.Stop()
    instance.Destroy()
    app.Yield()
    app.Destroy()


def test_hidden_empty_shell_and_cached_list(frame):
    from notes.domain.models import Document, Library, SearchHit
    instance, service = frame
    assert instance.preview_control.GetReadOnly()
    assert not instance.IsShown()
    doc = Document("d", "l", "sub/你好.txt", "你好.txt", is_favorite=True)
    lib = Library("l", "文档库", "C:/docs")
    instance.list.replace((SearchHit(doc),), (lib,))
    assert instance.list.OnGetItemText(0, 0) == "★ 你好.txt"
    assert instance.list.OnGetItemText(0, 1) == "文档库 / sub"
    count = len(service.requests)
    for _ in range(100):
        instance.list.OnGetItemText(0, 2)
    assert len(service.requests) == count  # paint path is cache-only


def test_preview_generation_rejects_stale_reply(frame):
    from notes.domain.models import Document, Preview, ReadResult
    instance, service = frame
    first = Document("a", "l", "a.txt", "a.txt")
    second = replace(first, id="b", file_name="b.txt", relative_path="b.txt")
    instance._load_preview(first)
    old = service.requests[-1]
    instance._load_preview(second)
    new = service.requests[-1]
    new[2](Preview(second, "C:/b.txt", ReadResult(text="new")))
    old[2](Preview(first, "C:/a.txt", ReadResult(text="old")))
    assert instance.preview_control.GetText() == "new"
    assert instance.path_label.GetValue() == "C:/b.txt"
    assert instance.preview_control.GetReadOnly()


def test_query_generation_rejects_old_page(frame):
    from notes.domain.models import Document, SearchHit, SearchResult
    instance, service = frame
    first = service.requests[-1]
    instance._query()
    latest = service.requests[-1]
    doc = Document("b", "l", "b.txt", "b.txt")
    latest[2](SearchResult(items=(SearchHit(doc),), total=1))
    first[2](SearchResult())
    assert instance.list.GetItemCount() == 1
    assert instance.list.hits[0].document.id == "b"


def test_callbacks_are_ignored_after_close_begins(frame):
    from notes.domain.models import AppState, SearchResult
    instance, service = frame
    instance._closing = True
    for command, payload, success, error, future in service.requests:
        success(AppState() if command == "state" else SearchResult(total=999))
        error(RuntimeError("should not display"))
    assert "999" not in instance.page_label.GetLabel()


def test_state_status_does_not_loop_queries(frame):
    from notes.domain.models import AppState
    instance, service = frame
    before = len(service.requests)
    instance._on_state(AppState(status="查询中", busy=True))
    instance._on_state(AppState(status="就绪", busy=False))
    assert len(service.requests) == before


def test_revision_refreshes_results_once(frame):
    from notes.domain.models import AppState
    instance, service = frame
    before = len(service.requests)
    instance._on_state(AppState(status="扫描完成", data_revision=1))
    assert len(service.requests) == before + 1
    assert service.requests[-1][0] == "query"
    instance._on_state(AppState(status="就绪", data_revision=1))
    assert len(service.requests) == before + 1


def test_favorite_and_cancel_actions_use_service(frame):
    from notes.domain.models import Document
    instance, service = frame
    instance._selected = Document("d", "l", "a.txt", "a.txt")
    instance._favorite()
    assert service.requests[-1][:2] == (
        "set_favorite", {"document_id": "d", "value": True})
    instance._cancel_scan()
    assert service.requests[-1][:2] == ("cancel_scan", {})


def test_unavailable_filter_and_content_mode(frame):
    instance, service = frame
    instance.mode.SetSelection(2)
    instance.unavailable.SetValue(True)
    instance._filters = {"library_id": "lib", "folder": "sub", "tag_ids": ("x", "y")}
    instance._query(reset=True)
    command, payload = service.requests[-1][:2]
    assert command == "query"
    assert payload["mode"] == "content"
    assert payload["include_unavailable"]
    assert payload["folder"] == "sub"
    assert payload["tag_ids"] == ("x", "y")
    assert payload["page_size"] == 100


def test_new_dialog_folder_callbacks_are_lifecycle_guarded(frame):
    import wx
    from notes.domain.models import Library
    from notes.ui.dialogs import NewDocumentDialog
    instance, _ = frame
    requests = []
    dialog = NewDocumentDialog(instance, [Library("l", "库", "C:/docs")],
                               request_folders=lambda lib, cb: requests.append(cb))
    requests[0](("", "empty/sub"))
    assert dialog.directory.GetItems() == ["", "empty/sub"]
    dialog.Destroy()
    wx.GetApp().Yield()
    # A delayed folder enumeration must not touch destroyed native controls.
    requests[0](("", "late"))


def test_missing_file_relocation_requires_two_confirmations_for_merge(frame, monkeypatch):
    import wx
    from notes.domain.models import Document
    instance, service = frame
    document = Document("missing", "lib", "old.txt", "old.txt", availability="missing")
    instance._selected = document
    instance._selection_controls(True)
    assert instance.relocate_button.IsEnabled()
    assert not instance.open_button.IsEnabled()
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda message, *args, **kwargs:
                        prompts.append(message) or wx.YES)
    instance._inspect_relocation(document, "C:/docs/new.txt")
    request = service.requests[-1]
    assert request[:2] == ("inspect_relocation", {"document_id": "missing",
                                                  "target_path": "C:/docs/new.txt"})
    request[2]({"document_id": "missing", "target_relative": "new.txt",
                "requires_merge_confirmation": True, "target_document_id": "placeholder"})
    assert len(prompts) == 2
    assert "只更新管理关联" in prompts[0]
    assert "占位" in prompts[1] and "不移动" in prompts[1]
    assert service.requests[-1][:2] == (
        "relocate_document", {"document_id": "missing", "target_path": "C:/docs/new.txt",
                              "merge_unannotated": True, "confirmed": True})


def test_relocation_merge_cancel_never_mutates(frame, monkeypatch):
    import wx
    from notes.domain.models import Document
    instance, service = frame
    document = Document("missing", "lib", "old.txt", "old.txt", availability="missing")
    instance._selected = document
    decisions = iter((wx.YES, wx.NO))
    monkeypatch.setattr(wx, "MessageBox", lambda *args, **kwargs: next(decisions))
    instance._inspect_relocation(document, "C:/docs/new.txt")
    service.requests[-1][2]({"requires_merge_confirmation": True})
    assert not any(request[0] == "relocate_document" for request in service.requests)


def test_relocation_delayed_inspection_cannot_prompt_after_selection_changed(frame, monkeypatch):
    import wx
    from notes.domain.models import Document
    instance, service = frame
    document = Document("missing", "lib", "old.txt", "old.txt", availability="missing")
    instance._selected = document
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda *args, **kwargs: prompts.append(args) or wx.YES)
    instance._inspect_relocation(document, "C:/docs/new.txt")
    request = service.requests[-1]
    instance._clear_preview()
    request[2]({"requires_merge_confirmation": True})
    request[3](RuntimeError("stale failure"))
    assert prompts == []
    assert not any(request[0] == "relocate_document" for request in service.requests)


def test_relocation_nonmerge_and_late_success_do_not_replace_new_selection(frame, monkeypatch):
    import wx
    from notes.domain.models import Document
    instance, service = frame
    document = Document("missing", "lib", "old.txt", "old.txt", availability="missing")
    instance._selected = document
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda *args, **kwargs: prompts.append(args) or wx.YES)
    instance._inspect_relocation(document, "C:/docs/new.txt")
    service.requests[-1][2]({"requires_merge_confirmation": False})
    mutation = service.requests[-1]
    assert len(prompts) == 1
    assert mutation[1]["merge_unannotated"] is False
    other = Document("other", "lib", "other.txt", "other.txt")
    instance._load_preview(other)
    mutation[2](replace(document, availability="present", relative_path="new.txt"))
    assert instance._selected.id == "other"


def test_automatic_backup_controls_and_status_use_service(frame):
    instance, service = frame
    instance._automatic_backup()
    assert not instance.backup_button.IsEnabled()
    assert service.requests[-1][:2] == ("automatic_backup", {"force": True})
    service.requests[-1][2]({"status": "success", "last_success": 1789444800.0,
                            "message": "备份成功"})
    assert instance.backup_button.IsEnabled()
    assert "备份成功" in instance.backup_label.GetLabel()
    assert "不含正文" in instance.backup_label.GetLabel()
    assert "2026" in instance.backup_label.GetLabel()


def test_backup_status_details_warn_body_not_backed_up(frame, monkeypatch):
    import wx
    instance, service = frame
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda message, *args, **kwargs: prompts.append(message))
    instance._backup_details()
    assert service.requests[-1][:2] == ("backup_status", {})
    service.requests[-1][2]({"status": "dirty", "last_success": None,
                            "due": True, "dirty": True, "path": "C:/data/backups"})
    assert "不包含文档正文" in prompts[0]
    assert "C:/data/backups" in prompts[0]
