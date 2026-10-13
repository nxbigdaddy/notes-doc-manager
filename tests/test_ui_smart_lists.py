"""Hidden saved-filter GUI safety tests; no real editor or global hotkey use."""
from concurrent.futures import Future
from dataclasses import replace
import os
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden smart-list GUI tests")


class FakeService:
    def __init__(self):
        self.requests = []

    def subscribe(self, callback):
        return lambda: None

    def submit(self, command, payload, success, error):
        self.requests.append((command, payload, success, error))
        return Future()

    def close(self):
        pass


@pytest.fixture
def gui():
    import wx
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    windows = []

    def pump(predicate=lambda: True, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail("Smart-list GUI timed out")
    yield windows, pump
    for window in reversed(windows):
        if window:
            window.Destroy()
    pump()
    del activator
    app.Destroy()


@pytest.fixture
def frame(gui):
    from notes.domain.models import AppState, Library, SearchQuery, SmartList, Tag
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    service = FakeService()
    window = MainFrame(service)
    windows.append(window)
    lib = Library(str(uuid4()), "中文文档库", "C:/temporary", availability="online")
    tag = Tag(str(uuid4()), "工作")
    query = SearchQuery(text="needle", mode="content", library_id=lib.id, folder=" folder",
                        tag_ids=(tag.id,), favorites_only=True, include_unavailable=True)
    smart = SmartList(str(uuid4()), "完整组合", query)
    window._on_state(AppState(libraries=(lib,), tags=(tag,), smart_lists=(smart,)))
    service.requests.clear()
    return window, service, smart, lib, tag, pump


def query_request(service):
    return next(item for item in reversed(service.requests) if item[0] == "query")


def test_saved_query_sends_only_id_page_and_locks_visible_controls(frame):
    window, service, smart, _, _, _ = frame
    window._activate_smart_list(smart.id)
    assert query_request(service)[1] == {"smart_list_id": smart.id, "page": 0, "page_size": 100}
    assert window.search.GetValue() == smart.query.text
    assert window.mode.GetSelection() == 2
    assert window.unavailable.GetValue()
    assert not window.search.IsEnabled() and not window.mode.IsEnabled()
    assert not window.unavailable.IsEnabled()
    window._change_page(1)
    assert query_request(service)[1] == {"smart_list_id": smart.id, "page": 1, "page_size": 100}
    assert "完整组合" in window.summary.GetLabel()
    assert " folder" in window.summary.GetToolTipText()
    assert "仅收藏" in window.scope_detail.GetLabel()


def test_missing_reference_clears_results_and_late_preview_without_dropping_condition(frame):
    from notes.domain.models import Document, Preview, ReadResult, SearchHit, SearchResult
    window, service, smart, lib, tag, _ = frame
    window._filters = {"library_id": lib.id, "tag_ids": (tag.id,)}
    window._activate_smart_list(smart.id)
    old_query = query_request(service)
    doc = Document(str(uuid4()), lib.id, " folder/a.txt", "a.txt")
    old_query[2](SearchResult(items=(SearchHit(doc),), total=1))
    window._load_preview(doc)
    old_preview = next(item for item in reversed(service.requests) if item[0] == "preview")
    bad = replace(smart, available=False, warning="保存的标签已删除；不会移除该筛选条件")
    window._on_state(replace(window.state, tags=(), smart_lists=(bad,), data_revision=1))
    assert window._active_smart_list_id == smart.id
    assert window._filters["tag_ids"] == (tag.id,)
    assert window.list.GetItemCount() == 0 and window._selected is None
    assert window.preview_control.GetText() == ""
    assert "标签已删除" in window.empty.GetLabel()
    old_query[2](SearchResult(items=(SearchHit(doc),), total=1))
    old_preview[2](Preview(doc, "C:/temporary/a.txt", ReadResult(text="不得回显旧内容")))
    assert window.list.GetItemCount() == 0
    assert window.preview_control.GetText() == ""
    assert all("smart_list_id" in item[1] for item in service.requests if item[0] == "query")


def test_deleted_or_corrupt_active_list_never_falls_back_to_all_documents(frame):
    window, service, smart, _, _, _ = frame
    window._activate_smart_list(smart.id)
    window._on_state(replace(window.state, smart_lists=(), smart_lists_warning="列表数据损坏",
                             data_revision=1))
    before = len(service.requests)
    window._query()
    assert len(service.requests) == before
    assert window._active_smart_list_id == smart.id
    assert "列表数据损坏" in window.empty.GetLabel()
    assert window.list.GetItemCount() == 0
    with pytest.raises(ValueError):
        window._temporary_query()


def test_authoritative_query_error_is_inline_and_clears_cached_preview(frame, monkeypatch):
    from notes.domain.models import Document, NotesError, ReadResult, Preview
    window, service, smart, lib, _, _ = frame
    modal_errors = []
    monkeypatch.setattr(window, "_error", lambda error: modal_errors.append(error))
    window._activate_smart_list(smart.id)
    request = query_request(service)
    doc = Document(str(uuid4()), lib.id, "old.txt", "old.txt")
    window._load_preview(doc)
    preview = next(item for item in reversed(service.requests) if item[0] == "preview")
    preview[2](Preview(doc, "C:/old.txt", ReadResult(text="旧预览")))
    request[3](NotesError("invalid_reference", "文档库已停用"))
    assert modal_errors == []
    assert window.preview_control.GetText() == ""
    assert "已停用" in window.empty.GetLabel()
    assert window._active_smart_list_id == smart.id


def test_explicit_ordinary_navigation_restores_temporary_controls_and_ignores_late_saved_result(frame):
    from notes.domain.models import Document, SearchHit, SearchResult
    window, service, smart, lib, _, _ = frame
    window.search.ChangeValue("原来的临时搜索")
    window.mode.SetSelection(1)
    window.unavailable.SetValue(False)
    window._activate_smart_list(smart.id)
    old = query_request(service)
    root = window.tree.GetRootItem()
    all_documents, _ = window.tree.GetFirstChild(root)
    event = type("Selection", (), {"GetItem": lambda self: all_documents})()
    window._navigate(event)
    assert window._active_smart_list_id is None
    assert window.search.IsEnabled() and window.mode.IsEnabled() and window.unavailable.IsEnabled()
    assert window.search.GetValue() == "原来的临时搜索"
    assert query_request(service)[1]["text"] == "原来的临时搜索"
    assert "smart_list_id" not in query_request(service)[1]
    old[2](SearchResult(items=(SearchHit(Document(str(uuid4()), lib.id, "stale.txt", "stale.txt")),),
                        total=1))
    assert window.list.GetItemCount() == 0


def test_smart_dialog_preserves_missing_library_tags_and_leading_space_folder(gui):
    from notes.domain.models import AppState, SearchQuery, SmartList, Tag
    from notes.ui.smart_list_dialog import SmartListDialog
    windows, _ = gui
    missing_library, missing_tag = str(uuid4()), str(uuid4())
    present_tag = Tag(str(uuid4()), "当前标签")
    query = SearchQuery(text=" literal ", mode="name", library_id=missing_library,
                        folder=" folder/sub", tag_ids=(present_tag.id, missing_tag),
                        favorites_only=True, recent_only=True, include_unavailable=True)
    smart = SmartList(str(uuid4()), "失效引用保留", query, False, "原库和一个标签已不存在")
    dialog = SmartListDialog(None, AppState(tags=(present_tag,)), smart_list=smart)
    windows.append(dialog)
    assert "失效文档库" in dialog.library.GetStringSelection()
    assert any("失效标签" in dialog.tags.GetString(index) for index in dialog.tags.GetCheckedItems())
    payload = dialog.payload()
    assert payload["id"] == smart.id
    assert payload["query"]["library_id"] == missing_library
    assert set(payload["query"]["tag_ids"]) == {present_tag.id, missing_tag}
    assert payload["query"]["folder"] == " folder/sub"
    assert payload["query"]["text"] == " literal "
    assert all(payload["query"][key] for key in ("favorites_only", "recent_only", "include_unavailable"))
    assert set(payload["query"]) == {"text", "mode", "library_id", "folder", "tag_ids",
                                    "favorites_only", "recent_only", "include_unavailable"}
    assert "0.8" in dialog.compatibility_note.GetLabel()
    assert "旧版不能打开" in dialog.compatibility_note.GetLabel()


def test_smart_dialog_rejects_invalid_folder_without_clearing_it(gui):
    import wx
    from notes.domain.models import AppState, NotesError
    from notes.ui.smart_list_dialog import SmartListDialog
    windows, _ = gui
    dialog = SmartListDialog(None, AppState())
    windows.append(dialog)
    dialog.name.SetValue("非法路径测试")
    dialog.folder.SetValue("../escape")
    dialog._accept()
    assert dialog.GetReturnCode() != wx.ID_OK
    assert dialog.folder.GetValue() == "../escape"
    assert dialog.warning.GetLabel()
    with pytest.raises(NotesError):
        dialog.payload()


def test_delete_requires_explicit_confirmation_and_never_removes_documents(frame, monkeypatch):
    import wx
    window, service, smart, _, _, _ = frame
    window._activate_smart_list(smart.id)
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda message, *args: prompts.append(message) or wx.NO)
    window._delete_smart_list()
    assert not any(item[0] == "delete_smart_list" for item in service.requests)
    monkeypatch.setattr(wx, "MessageBox", lambda message, *args: prompts.append(message) or wx.YES)
    window._delete_smart_list()
    request = next(item for item in service.requests if item[0] == "delete_smart_list")
    assert request[1] == {"id": smart.id, "confirmed": True}
    assert all("不删除文档" in text for text in prompts)
    assert not any(item[0] in {"delete_document", "delete_tag"} for item in service.requests)


def test_save_current_filter_and_edit_submit_complete_query_not_old_gui_scope(frame, monkeypatch):
    import wx
    from notes.domain.smart_lists import query_payload
    from notes.ui import smart_list_dialog
    window, service, smart, _, _, _ = frame
    captured = []

    class FakeDialog:
        def __init__(self, parent, state, smart_list=None, query=None):
            self.smart = smart_list
            self.query = smart_list.query if smart_list else query
            captured.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ShowModal(self):
            return wx.ID_OK

        def payload(self):
            payload = {"name": "保存的组合", "query": query_payload(self.query)}
            if self.smart:
                payload["id"] = self.smart.id
            return payload
    monkeypatch.setattr(smart_list_dialog, "SmartListDialog", FakeDialog)
    window.search.ChangeValue("临时词")
    window.mode.SetSelection(2)
    window._filters = {"favorites_only": True, "recent_only": True}
    window._save_current_smart_list()
    request = next(item for item in service.requests if item[0] == "create_smart_list")
    assert request[1]["query"]["text"] == "临时词"
    assert request[1]["query"]["favorites_only"] is True
    window._activate_smart_list(smart.id)
    window._edit_smart_list()
    request = next(item for item in reversed(service.requests) if item[0] == "update_smart_list")
    assert request[1]["id"] == smart.id
    assert request[1]["query"] == query_payload(smart.query)
    assert captured[-1].smart is smart


def test_restore_confirmation_discloses_list_counts_and_format1_clearing(frame):
    window, _, _, _, _, _ = frame
    legacy = window._confirm_restore_message({"format_version": 1, "documents": 3})
    assert "智能列表数：0" in legacy
    assert "清空当前保存的智能列表" in legacy
    current = window._confirm_restore_message({"format_version": 2, "smart_lists": 4})
    assert "智能列表数：4" in current
    assert "智能列表等管理信息" in current
    assert "清空当前保存的智能列表" not in current


def test_real_saved_view_dynamic_update_invalid_reference_explicit_repair_and_delete(gui, tmp_path):
    import wx
    from notes.application.service import ApplicationService
    from notes.domain.models import NotesError, SearchQuery
    from notes.ui.main_frame import MainFrame
    from notes.ui.smart_list_dialog import SmartListDialog
    windows, pump = gui
    root = tmp_path / "documents"
    folder = root / " folder"
    folder.mkdir(parents=True)
    for path in (folder / "符合.txt", folder / "少一个标签.txt", root / "库外层.txt"):
        path.write_text("共同关键词：原文始终保持不变", encoding="utf-8")
    originals = {path: path.read_bytes() for path in root.rglob("*.txt")}
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter, monitoring=False)
    frame = form = repair = None
    errors = []
    try:
        lib = service.call("add_library", {"root_path": str(root), "name": "完整条件库"})
        service.call("scan", {"library_id": lib.id})
        documents = service.call("query").items
        selected = next(hit.document for hit in documents if hit.document.file_name == "符合.txt")
        second = next(hit.document for hit in documents if hit.document.file_name == "少一个标签.txt")
        first_tag = service.call("create_tag", {"name": "第一个"})
        second_tag = service.call("create_tag", {"name": "第二个"})
        service.call("set_document_tags", {"document_id": selected.id,
                                           "tag_ids": [first_tag.id, second_tag.id]})
        service.call("set_document_tags", {"document_id": second.id, "tag_ids": [first_tag.id]})
        service.call("set_favorite", {"document_id": selected.id, "value": True})
        form = SmartListDialog(None, service.call("state"), query=SearchQuery(
            text="共同关键词", mode="content", library_id=lib.id, folder=" folder",
            tag_ids=(first_tag.id, second_tag.id), favorites_only=True))
        windows.append(form)
        form.name.SetValue("我的组合")
        payload = form.payload()
        smart = service.call("create_smart_list", payload)
        frame = MainFrame(service)
        windows.append(frame)
        frame._error = lambda error: errors.append(str(error))
        pump(lambda: any(item.id == smart.id for item in frame.state.smart_lists))
        frame._activate_smart_list(smart.id)
        pump(lambda: frame.list.GetItemCount() == 1)
        assert frame.list.hits[0].document.id == selected.id
        frame._load_preview(frame.list.hits[0].document)
        pump(lambda: frame._preview is not None)

        modified = {**payload["query"], "text": "没有这个内容"}
        service.call("update_smart_list", {"id": smart.id, "name": "已更新", "query": modified})
        pump(lambda: frame.search.GetValue() == "没有这个内容" and frame.list.GetItemCount() == 0)
        service.call("update_smart_list", {"id": smart.id, **payload})
        pump(lambda: frame.search.GetValue() == "共同关键词" and frame.list.GetItemCount() == 1)
        service.call("delete_tag", {"tag_id": first_tag.id})
        pump(lambda: frame._active_smart_list() is not None
             and not frame._active_smart_list().available and frame.list.GetItemCount() == 0)
        assert frame._active_smart_list_id == smart.id
        assert frame._selected is None and frame.preview_control.GetText() == ""
        assert not frame.search.IsEnabled()
        invalid = frame._active_smart_list()
        assert first_tag.id in invalid.query.tag_ids

        repair = SmartListDialog(frame, frame.state, smart_list=invalid)
        windows.append(repair)
        assert first_tag.id in repair.payload()["query"]["tag_ids"]
        with pytest.raises(NotesError):
            service.call("update_smart_list", repair.payload())
        # This is the user's explicit removal, not automatic condition cleanup.
        repair.tags.Check(repair.tag_ids.index(first_tag.id), False)
        service.call("update_smart_list", repair.payload())
        pump(lambda: frame._active_smart_list().available and frame.list.GetItemCount() == 1)
        service.call("delete_smart_list", {"id": smart.id, "confirmed": True})
        pump(lambda: frame._active_smart_list() is None and frame.list.GetItemCount() == 0)
        assert frame._active_smart_list_id == smart.id
        assert "已删除" in frame.empty.GetLabel()
        assert service.call("query").total == 3
        assert all(path.read_bytes() == content for path, content in originals.items())
        assert errors == []
    finally:
        for window in (repair, form, frame):
            if window:
                window.Destroy()
        service.close()


def test_real_disappeared_saved_folder_returns_empty_without_root_fallback(gui, tmp_path):
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame
    windows, pump = gui
    root = tmp_path / "documents"
    folder = root / "original"
    folder.mkdir(parents=True)
    (folder / "inside.txt").write_text("target", encoding="utf-8")
    (root / "outside.txt").write_text("target", encoding="utf-8")
    service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter, monitoring=False)
    frame = None
    try:
        lib = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": lib.id})
        smart = service.call("create_smart_list", {"name": "固定文件夹",
                             "query": {"library_id": lib.id, "folder": "original"}})
        frame = MainFrame(service)
        windows.append(frame)
        pump(lambda: any(item.id == smart.id for item in frame.state.smart_lists))
        frame._activate_smart_list(smart.id)
        pump(lambda: frame.list.GetItemCount() == 1)
        relocated = root / "renamed"
        assert folder.resolve().is_relative_to(tmp_path.resolve())
        assert relocated.resolve().is_relative_to(tmp_path.resolve())
        folder.rename(relocated)  # test-owned external directory move, not a GUI operation
        service.call("scan", {"library_id": lib.id})
        pump(lambda: frame.list.GetItemCount() == 0)
        assert frame._active_smart_list_id == smart.id
        assert frame._active_smart_list().query.folder == "original"
        assert frame._active_smart_list().available
        assert service.call("query").total == 2
    finally:
        if frame:
            frame.Destroy()
        service.close()


def _mock_new_saved_form(monkeypatch):
    import wx
    from notes.ui import smart_list_dialog

    class AcceptedForm:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ShowModal(self):
            return wx.ID_OK

        def payload(self):
            return {"name": "迟到保存", "query": {"text": "saved"}}
    monkeypatch.setattr(smart_list_dialog, "SmartListDialog", AcceptedForm)


@pytest.mark.parametrize("navigation_at", ["before_saved_reply", "before_state_reply"])
def test_late_saved_list_does_not_steal_explicit_ordinary_navigation(frame, monkeypatch, navigation_at):
    from notes.domain.models import SearchQuery, SmartList
    window, service, _, _, _, _ = frame
    _mock_new_saved_form(monkeypatch)
    window._new_smart_list()
    create = next(item for item in reversed(service.requests) if item[0] == "create_smart_list")
    saved = SmartList(str(uuid4()), "迟到保存", SearchQuery(text="saved"))
    all_item, _ = window.tree.GetFirstChild(window.tree.GetRootItem())
    favorites_item = window.tree.GetNextSibling(all_item)
    navigation = type("Selection", (), {"GetItem": lambda self: favorites_item})()
    if navigation_at == "before_saved_reply":
        window._navigate(navigation)
    create[2](saved)
    state_reply = next(item for item in reversed(service.requests) if item[0] == "state")
    if navigation_at == "before_state_reply":
        window._navigate(navigation)
    state_reply[2](replace(window.state, smart_lists=window.state.smart_lists + (saved,),
                          data_revision=window.state.data_revision + 1))
    assert any(item.id == saved.id for item in window.state.smart_lists)
    assert window._active_smart_list_id is None
    assert window._filters == {"favorites_only": True}
    assert window.summary.GetLabel() == "收藏"
    assert query_request(service)[1]["favorites_only"] is True
    assert "smart_list_id" not in query_request(service)[1]


def test_background_query_revision_does_not_block_intended_saved_activation(frame, monkeypatch):
    from notes.domain.models import SearchQuery, SmartList
    window, service, _, _, _, _ = frame
    _mock_new_saved_form(monkeypatch)
    window._new_smart_list()
    create = next(item for item in reversed(service.requests) if item[0] == "create_smart_list")
    navigation = window._navigation_generation
    query_generation = window._query_generation
    window._on_state(replace(window.state, data_revision=window.state.data_revision + 1))
    assert window._query_generation > query_generation
    assert window._navigation_generation == navigation
    saved = SmartList(str(uuid4()), "保存后激活", SearchQuery(text="saved"))
    create[2](saved)
    state_reply = next(item for item in reversed(service.requests) if item[0] == "state")
    state_reply[2](replace(window.state, smart_lists=window.state.smart_lists + (saved,)))
    assert window._active_smart_list_id == saved.id
    assert query_request(service)[1]["smart_list_id"] == saved.id
    assert window._navigation_generation == navigation


def test_explicit_smart_and_right_click_navigation_advance_navigation_generation(frame, monkeypatch):
    window, _, smart, _, _, _ = frame
    before = window._navigation_generation
    window._activate_smart_list(smart.id)
    assert window._navigation_generation > before
    selected = window.tree.GetSelection()
    event = type("RightClick", (), {"GetItem": lambda self: selected})()
    monkeypatch.setattr(window, "PopupMenu", lambda menu: None)
    before = window._navigation_generation
    window._tree_context(event)
    assert window._navigation_generation > before


@pytest.mark.parametrize("scope", ["valid", "unavailable", "missing", "all", "corrupt", "ordinary"])
def test_new_document_initial_location_comes_from_current_saved_scope_not_old_filters(
        frame, monkeypatch, scope):
    import wx
    from notes.domain.models import Library
    from notes.ui import main_frame
    window, service, smart, ordinary_library, _, _ = frame
    saved_library = Library(str(uuid4()), "智能列表的库", "C:/saved", availability="online")
    active = replace(smart, query=replace(smart.query, library_id=saved_library.id,
                                         folder=" 保存目录"))
    if scope == "unavailable":
        active = replace(active, available=False, warning="不可用")
    elif scope == "all":
        active = replace(active, query=replace(active.query, library_id=None, folder=""))
    state = replace(window.state, libraries=(ordinary_library, saved_library), smart_lists=(active,),
                    smart_lists_warning="数据不可读取" if scope == "corrupt" else "")
    window._on_state(state)
    window._filters = {"library_id": ordinary_library.id, "folder": "旧普通目录"}
    if scope != "ordinary":
        window._activate_smart_list(str(uuid4()) if scope == "missing" else active.id)
    calls = []

    class CapturedNewDocument:
        def __init__(self, parent, libraries, selected_library, folder, **kwargs):
            calls.append((selected_library, folder, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ShowModal(self):
            return wx.ID_CANCEL  # no file creation or editor invocation
    monkeypatch.setattr(main_frame, "NewDocumentDialog", CapturedNewDocument)
    window._new_document()
    if scope == "valid":
        assert calls[0][:2] == (saved_library.id, " 保存目录")
    elif scope == "ordinary":
        assert calls[0][:2] == (ordinary_library.id, "旧普通目录")
    else:
        assert calls[0][:2] == (None, "")
    assert not {"text", "tag_ids", "favorites_only", "recent_only"} & calls[0][2].keys()
    assert not any(item[0] in {"create_document", "open_document"} for item in service.requests)
