"""Capture only this app's offscreen HWND with isolated, synthetic documents.

No desktop capture, other-app enumeration, clipboard, editor launch, real library
access, or interactive prompts. Uses Windows PrintWindow + wx's memory bitmap;
no screenshot package is needed. Sample/data directory is deleted after closing
the real ApplicationService. The PNG is the only retained sample artifact.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def wait_for(app, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.Yield()
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("GUI callback did not reach expected state")


def capture_window(frame, output):
    import wx
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(frame.GetHandle(), ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    bitmap = wx.Bitmap(rect.right - rect.left, rect.bottom - rect.top)
    memory = wx.MemoryDC(bitmap)
    try:
        memory.SetBackground(wx.Brush(wx.Colour(255, 255, 255)))
        memory.Clear()
        # Deliberately capture only the HWND owned by this frame, even though
        # the entire window is outside the visible desktop.
        if not user32.PrintWindow(frame.GetHandle(), memory.GetHandle(), 0):
            raise RuntimeError("PrintWindow failed for Notes HWND")
    finally:
        memory.SelectObject(wx.NullBitmap)
    image = bitmap.ConvertToImage()
    pixels = image.GetData()
    sample = bytes(pixels)[::17]
    if len(set(sample)) < 8:
        raise RuntimeError("Window-only capture was blank; refusing misleading artifact")
    if not image.SaveFile(str(output), wx.BITMAP_TYPE_PNG):
        raise RuntimeError("Could not save window-only PNG")
    return {"width": image.GetWidth(), "height": image.GetHeight(),
            "method": "PrintWindow, only Notes frame HWND"}


def layout_warnings(frame):
    """Programmatic overflow audit; not a substitute for human visual review."""
    import wx
    warnings = []

    def visit(window):
        for child in window.GetChildren():
            if child.IsShown() and isinstance(child, (wx.Button, wx.StaticText, wx.Choice)):
                size = child.GetSize()
                parent_size = window.GetClientSize()
                position = child.GetPosition()
                if position.x + size.width > parent_size.width + 2:
                    warnings.append({"control": child.GetLabel(), "issue": "parent-overflow"})
                if isinstance(child, wx.StaticText) and "\n" not in child.GetLabel():
                    width, _ = child.GetTextExtent(child.GetLabel())
                    ellipsized = child.GetWindowStyle() & (wx.ST_ELLIPSIZE_START |
                                                          wx.ST_ELLIPSIZE_MIDDLE |
                                                          wx.ST_ELLIPSIZE_END)
                    if width > size.width + 2 and not ellipsized:
                        warnings.append({"control": child.GetLabel(), "issue": "text-clipped",
                                         "needed": width, "width": size.width})
            visit(child)
    visit(frame)
    return warnings


def run(output: Path):
    if os.name != "nt":
        raise RuntimeError("This window-only capture requires Windows")
    import wx
    from notes.application.service import ApplicationService
    from notes.ui.main_frame import MainFrame

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep all synthetic source and metadata below an explicitly validated
    # workspace artifacts directory. Never touch existing user document roots.
    artifact_root = (ROOT / ".artifacts").resolve()
    artifact_root.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="gui-capture-", dir=artifact_root)
    sample_root = Path(temporary.name).resolve()
    if not sample_root.is_relative_to(artifact_root):
        raise RuntimeError("Unexpected sample directory boundary")
    app = wx.App(False)
    service = frame = None
    errors = []
    try:
        library_root = sample_root / "sample-documents"
        (library_root / "工作" / "项目记录").mkdir(parents=True)
        (library_root / "个人" / "待整理").mkdir(parents=True)
        documents = {
            "工作/项目记录/文档管理器 · 设计纪要.md":
                "# 本地文档管理器\n\n让文件有序，让编辑保持简单。\n\n"
                "## 本轮设计\n- 管理器：创建、分类、查找与预览。\n"
                "- Notepad3：继续负责熟悉的文本编辑。\n"
                "- 正文始终是普通文件，不被数据库锁定。\n\n"
                "## 中文搜索验证\n支持项目记录、短词与完整字面关键词。\n"
                "预览只读取已经保存的文字，不修改文件。😀\n\n"
                "## 下一步\n验证目录变化、管理信息备份与安全恢复。\n",
            "工作/项目记录/会议记录.txt": "会议记录\n今天讨论文档库与中文搜索。\n",
            "工作/命令速查.txt": "常用命令速查\n只读保存，便于查找。\n",
            "个人/读书摘记.md": "# 读书摘记\n少即是多；重要的记录值得被找到。\n",
            "个人/灵感清单.txt": "灵感清单\n把日常零散想法整理为可检索的文档。\n",
            "欢迎使用.txt": "这是隔离的可视化测试样例，不是用户真实内容。\n",
        }
        for relative, text in documents.items():
            (library_root / relative).write_text(text, encoding="utf-8")
        service = ApplicationService(sample_root / "metadata", dispatcher=wx.CallAfter,
                                     monitoring=False)
        library = service.call("add_library", {"root_path": str(library_root), "name": "我的文档"})
        service.call("scan", {"library_id": library.id})
        target = service.call("query", {"text": "设计纪要", "mode": "name"}).items[0].document
        work = service.call("create_tag", {"name": "工作"})
        design = service.call("create_tag", {"name": "产品设计"})
        service.call("set_document_tags", {"document_id": target.id,
                                           "tag_ids": (work.id, design.id)})
        service.call("set_favorite", {"document_id": target.id, "value": True})
        frame = MainFrame(service)
        frame._error = lambda error: errors.append(str(error))
        frame.SetPosition((-30000, -30000))
        frame.ShowWithoutActivating()
        wait_for(app, lambda: frame.list.GetItemCount() == len(documents))

        # Assert the real async search + preview lanes, not just a styled shell.
        frame.search.SetValue("中文搜索")
        frame.mode.SetSelection(2)
        frame._query(reset=True)
        wait_for(app, lambda: frame.list.GetItemCount() == 2)
        index = next(i for i, hit in enumerate(frame.list.hits)
                     if hit.document.id == target.id)
        frame.list.Select(index)
        frame._load_preview(frame.list.hits[index].document)
        wait_for(app, lambda: frame._preview is not None)
        assert "中文搜索" in frame.preview_control.GetText()
        assert frame.preview_control.GetReadOnly()
        assert "产品设计" in frame.tags_label.GetLabel()

        folders = service.call("folders", {"library_id": library.id})
        assert "个人/待整理" in folders
        frame._folders[library.id] = folders
        frame._build_tree()
        frame.search.SetValue("")
        frame.mode.SetSelection(0)
        frame._query(reset=True)
        wait_for(app, lambda: frame.list.GetItemCount() == len(documents))
        index = next(i for i, hit in enumerate(frame.list.hits)
                     if hit.document.id == target.id)
        frame.list.Select(index)
        frame._load_preview(frame.list.hits[index].document)
        wait_for(app, lambda: frame._preview is not None)
        assert frame.list.OnGetItemText(index, 2) != "—"
        frame.Refresh()
        frame.Update()
        for _ in range(15):
            app.Yield()
            time.sleep(0.02)
        if errors:
            raise RuntimeError(f"GUI errors: {errors}")
        dimensions = capture_window(frame, output)
        return {"capture": str(output), "documents": len(documents),
                "content_search_hits": 2, "folders_including_empty": len(folders),
                "readonly_preview": True, "chinese_tags": True,
                "layout_warnings": layout_warnings(frame), **dimensions}
    finally:
        if frame:
            frame._closing = True
            frame.Hide()
        if service:
            service.close()
        if frame:
            frame.Destroy()
            app.Yield()
        app.Destroy()
        # Re-verify resolved cleanup target remains under the exact artifacts
        # workspace; TemporaryDirectory only removes this generated subtree.
        if sample_root.resolve().is_relative_to(artifact_root) and sample_root != artifact_root:
            temporary.cleanup()
        else:
            raise RuntimeError("Unsafe sample cleanup target")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts" / "gui-preview.png")
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))
