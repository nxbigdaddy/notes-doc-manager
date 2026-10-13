"""Hidden GUI regression: observed rename updates selection and preview by ID."""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("NOTES_GUI_TESTS") != "1",
    reason="Set NOTES_GUI_TESTS=1 on Windows for hidden observed-move integration")


def test_selected_document_refreshes_to_observed_new_path_without_stale_preview(tmp_path):
    import wx
    from notes.application.service import ApplicationService
    from notes.domain.models import MoveHint
    from notes.ui.main_frame import MainFrame

    app = wx.App(False)
    service = frame = None
    errors = []

    def pump_until(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.Yield()
            if predicate():
                return
            time.sleep(.01)
        pytest.fail(f"Observed-move GUI timed out: {errors}")

    try:
        root = tmp_path / "documents"
        root.mkdir()
        source = root / "旧路径.txt"
        target = root / "新路径.txt"
        source.write_text("旧预览：改名前已保存的内容", encoding="utf-8")
        service = ApplicationService(tmp_path / "metadata", dispatcher=wx.CallAfter,
                                     monitoring=False)
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        original = service.call("query").items[0].document
        frame = MainFrame(service)
        frame._error = lambda error: errors.append(str(error))
        assert not frame.IsShown()
        pump_until(lambda: frame.list.GetItemCount() == 1)
        frame.list.Select(0)
        frame._load_preview(frame.list.hits[0].document)
        pump_until(lambda: frame._preview is not None)
        assert "旧预览" in frame.preview_control.GetText()
        old_revision = frame.state.data_revision
        # Rename and change content on the SAME physical file identity.
        assert source.resolve().is_relative_to(tmp_path.resolve())
        assert target.resolve().is_relative_to(tmp_path.resolve())
        source.rename(target)
        target.write_text("新预览：新路径的已保存内容", encoding="utf-8")
        completed = []
        service.submit("scan", {"library_id": library.id,
                                "moves": (MoveHint(source.name, target.name, time.monotonic()),),
                                "moves_reliable": True},
                       lambda value: completed.append(value), lambda error: errors.append(str(error)))
        pump_until(lambda: completed and frame.state.data_revision > old_revision
                   and frame._selected is not None
                   and frame._selected.id == original.id
                   and frame.path_label.GetValue() == str(target)
                   and "新预览" in frame.preview_control.GetText())
        assert "旧预览" not in frame.preview_control.GetText()
        assert frame.preview_title.GetLabel() == target.name
        assert frame.preview_control.GetReadOnly()
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
