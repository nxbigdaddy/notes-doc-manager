"""Unexplained experimental optimizations must not enter the shipped scan path."""

from notes.application.service import ApplicationService


def test_default_scanner_does_not_enable_experimental_anchor(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "sample.txt"
    source.write_text("普通扫描的中文内容", encoding="utf-8")
    service = ApplicationService(tmp_path / "data", monitoring=False)
    try:
        def forbidden():
            raise AssertionError("Experimental index_session unexpectedly enabled")

        monkeypatch.setattr(service.store, "index_session", forbidden)
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        result = service.call("query", {"text": "中文", "mode": "content"})
        assert result.complete and result.total == 1
        assert source.read_text(encoding="utf-8") == "普通扫描的中文内容"
    finally:
        service.close()
