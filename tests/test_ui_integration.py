"""Opt-in hidden wx + real service integration; no modal prompts or editor launch."""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden wx integration")


def test_real_service_search_preview_tags_and_folder_navigation(tmp_path):
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame

    app = wx.App(False)
    service = frame = None
    errors = []

    def wait_until(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.Yield()
            if predicate():
                return
            time.sleep(.01)
        pytest.fail(f"GUI timed out; errors={errors}")

    try:
        root = tmp_path / "library"
        (root / "工作" / "空目录").mkdir(parents=True)
        (root / "工作" / "你好.md").write_text("# 中文😀\n完整子串测试。\n", encoding="utf-8")
        (root / "另一个.txt").write_text("没有关键词", encoding="utf-8")
        service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter,
                                     monitoring=False)
        library = service.call("add_library", {"root_path": str(root), "name": "中文文档库"})
        service.call("scan", {"library_id": library.id})
        document = service.call("query", {"text": "你好", "mode": "name"}).items[0].document
        tag = service.call("create_tag", {"name": "中文标签"})
        service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
        frame = MainFrame(service)
        frame._error = lambda error: errors.append(str(error))
        assert not frame.IsShown()
        wait_until(lambda: frame.list.GetItemCount() == 2)
        frame.search.SetValue("完整子串")
        frame.mode.SetSelection(2)
        frame._query(reset=True)
        wait_until(lambda: frame.list.GetItemCount() == 1)
        frame._load_preview(frame.list.hits[0].document)
        wait_until(lambda: frame._preview is not None)
        assert frame.preview_control.GetText() == (root / "工作" / "你好.md").read_bytes().decode("utf-8")
        assert frame.preview_control.GetReadOnly()
        assert frame.path_label.GetValue() == str(root / "工作" / "你好.md")
        assert "中文标签" in frame.tags_label.GetLabel()
        assert frame.list.OnGetItemText(0, 2) != "—"
        folders = service.call("folders", {"library_id": library.id})
        assert "工作/空目录" in folders
        frame._folders[library.id] = folders
        frame._build_tree()

        # Mutating via UI updates the real store and refreshes the preview.
        frame._favorite()
        wait_until(lambda: frame._selected is not None and frame._selected.is_favorite)
        assert service.call("query", {"favorites_only": True}).total == 1
        assert not errors
    finally:
        if frame:
            frame._closing = True
        if service:
            service.close()
        if frame:
            frame.Destroy()
            app.Yield()
        app.Destroy()


def test_real_service_relocation_merge_and_automatic_backup(tmp_path, monkeypatch):
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame

    app = wx.App(False)
    service = frame = None
    errors = []
    prompts = []
    monkeypatch.setattr(wx, "MessageBox", lambda message, *args, **kwargs:
                        prompts.append(message) or wx.YES)

    def wait_until(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.Yield()
            if predicate():
                return
            time.sleep(.01)
        pytest.fail(f"GUI timed out; errors={errors}")

    try:
        root = tmp_path / "library"
        root.mkdir()
        source = root / "旧文件.txt"
        target = root / "新文件.txt"
        body = "只在临时目录中测试重新定位，正文保持不变。\n"
        source.write_text(body, encoding="utf-8")
        original_bytes = source.read_bytes()
        service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter,
                                     monitoring=False)
        library = service.call("add_library", {"root_path": str(root), "name": "重新定位测试"})
        service.call("scan", {"library_id": library.id})
        original = service.call("query").items[0].document
        tag = service.call("create_tag", {"name": "保留标签"})
        service.call("set_document_tags", {"document_id": original.id, "tag_ids": [tag.id]})
        service.call("set_favorite", {"document_id": original.id, "value": True})
        # This simulates an external move of a test-owned file, never a GUI move.
        assert source.resolve().is_relative_to(tmp_path.resolve())
        assert target.resolve().is_relative_to(tmp_path.resolve())
        source.rename(target)
        service.call("scan", {"library_id": library.id})
        records = service.call("query", {"include_unavailable": True}).items
        missing = next(hit.document for hit in records if hit.document.id == original.id)
        assert missing.availability == "missing"
        frame = MainFrame(service)
        frame._error = lambda error: errors.append(str(error))
        assert not frame.IsShown()
        frame.unavailable.SetValue(True)
        frame._query(reset=True)
        wait_until(lambda: frame.list.GetItemCount() == 2)
        frame._load_preview(missing)
        assert frame.relocate_button.IsEnabled()
        frame._inspect_relocation(missing, str(target))
        wait_until(lambda: frame._selected is not None and
                   frame._selected.relative_path == "新文件.txt" and
                   frame._selected.availability == "present" and
                   frame.list.GetItemCount() == 1)
        assert len(prompts) == 2
        assert "占位" in prompts[1]
        assert frame._selected.id == original.id
        assert frame._selected.is_favorite
        assert [item.name for item in frame._selected.tags] == ["保留标签"]
        assert target.read_bytes() == original_bytes
        assert not source.exists()
        frame._automatic_backup()
        wait_until(lambda: frame.backup_button.IsEnabled())
        result = service.call("backup_status")
        assert result["last_success"] is not None
        assert "不含正文" in frame.backup_label.GetLabel()
        assert not errors
    finally:
        if frame:
            frame._closing = True
        if service:
            service.close()
        if frame:
            frame.Destroy()
            app.Yield()
        app.Destroy()
