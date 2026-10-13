import codecs
import os
from threading import Event
from unittest.mock import patch

import pytest

from notes.domain.models import NotesError
from notes.infrastructure.files import (
    INDEX_LIMIT,
    PREVIEW_LIMIT,
    create_document,
    list_folders,
    read_document,
    scan_directory,
    validate_document_path,
    validate_library_root,
)


@pytest.mark.parametrize("name", ["../escape.txt", "..\\escape.txt", "/escape.txt",
    "C:\\escape.txt", "a:b.txt", "CON.txt", "LPT1.md", "file.txt.", "file.txt ",
    "a//file.txt", "a/../file.txt", "a/./file.txt", "file?.txt", ""])
def test_document_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(NotesError):
        validate_document_path(tmp_path, name, must_exist=False)


def test_library_overlap_and_appdata(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    data = tmp_path / "data"
    assert validate_library_root(str(root), data) == root
    for forbidden in (tmp_path, data):
        forbidden.mkdir(exist_ok=True)
        with pytest.raises(NotesError, match="应用数据"):
            validate_library_root(str(forbidden), data)
    with pytest.raises(NotesError, match="嵌套"):
        validate_library_root(str(root), data, (str(tmp_path),))


@pytest.mark.parametrize("root", [r"\\server\share", r"\\?\C:\data", "relative"])
def test_library_network_and_relative_rejected(tmp_path, root):
    with pytest.raises(NotesError):
        validate_library_root(root, tmp_path / "data")


def test_exclusive_creation_and_original_bytes(tmp_path):
    entry = create_document(tmp_path, "", "中文 & (记录).txt")
    path = tmp_path / entry.relative_path
    assert path.read_bytes() == b""
    path.write_bytes(b"original")
    with pytest.raises(NotesError) as error:
        create_document(tmp_path, "", path.name)
    assert error.value.code == "already_exists"
    assert path.read_bytes() == b"original"
    with pytest.raises(NotesError):
        create_document(tmp_path, "missing", "new.md")
    assert not (tmp_path / "missing").exists()
    with pytest.raises(NotesError):
        create_document(tmp_path, "", "script.exe")


def test_scan_and_folders_scope(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden.txt").write_text("hidden")
    (tmp_path / "empty" / "upper.TXT").write_text("text")
    (tmp_path / "skip.exe").write_bytes(b"binary")
    result = scan_directory(tmp_path)
    assert result.complete
    assert [e.relative_path for e in result.entries] == ["empty/upper.TXT"]
    assert list_folders(tmp_path) == ("", "empty")


def test_scan_cancellation_and_offline(tmp_path):
    cancel = Event()
    cancel.set()
    assert not scan_directory(tmp_path, cancel=cancel).complete
    assert not scan_directory(tmp_path / "missing").complete
    assert scan_directory(tmp_path / "missing").availability == "offline"
    with pytest.raises(NotesError):
        list_folders(tmp_path, cancel=cancel)


def test_scan_failure_never_complete(tmp_path):
    (tmp_path / "a.txt").write_text("safe")
    with patch("notes.infrastructure.files.os.scandir", side_effect=PermissionError):
        result = scan_directory(tmp_path)
    assert not result.complete
    assert result.errors


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_strict_bom_decoding_and_unchanged_bytes(tmp_path, encoding):
    path = tmp_path / "doc.txt"
    original = "中文文本 📝\nline two".encode(encoding)
    path.write_bytes(original)
    result = read_document(path)
    assert result.status == "ready"
    assert result.text == "中文文本 📝\nline two"
    assert path.read_bytes() == original


def test_legacy_needs_explicit_encoding(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_bytes("中文文本".encode("gb18030"))
    assert read_document(path).status == "needs_encoding"
    assert read_document(path, "gb18030").text == "中文文本"
    assert read_document(path, "rot13").status == "needs_encoding"


def test_binary_and_limits(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_bytes(b"a\x00b")
    assert read_document(path).status == "skipped_binary"
    path.write_bytes(b"a" * (INDEX_LIMIT + 1))
    assert read_document(path).status == "skipped_large"
    result = read_document(path, preview=True)
    assert result.status == "ready"
    assert result.truncated
    assert len(result.text) == PREVIEW_LIMIT


def test_preview_multibyte_boundary_and_full_validation(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_bytes(b"a" * (PREVIEW_LIMIT - 1) + "中".encode())
    assert read_document(path, preview=True).status == "ready"
    assert read_document(path, preview=True).text == "a" * (PREVIEW_LIMIT - 1)
    assert read_document(path).text.endswith("中")
    path.write_bytes(codecs.BOM_UTF16_LE + ("中" * PREVIEW_LIMIT).encode("utf-16-le"))
    assert read_document(path, preview=True).status == "ready"
    path.write_bytes(b"a" * PREVIEW_LIMIT + b"\xff")
    assert read_document(path, preview=True).status == "ready"
    assert read_document(path).status == "needs_encoding"


def test_read_empty_cancel_missing(tmp_path):
    path = tmp_path / "empty.txt"
    path.touch()
    assert read_document(path).status == "ready"
    assert read_document(path).text == ""
    cancel = Event()
    cancel.set()
    assert read_document(path, cancel=cancel).status == "pending"
    assert read_document(tmp_path / "missing.txt").status == "error"


def test_shared_handle_allows_windows_rename_and_write(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows sharing contract")
    from notes.infrastructure.files import _shared_reader
    path = tmp_path / "doc.txt"
    path.write_bytes(b"old")
    with _shared_reader(path) as stream:
        # Rename requires FILE_SHARE_DELETE. MoveFileEx replacement may still
        # be rejected by Windows despite sharing, so assert the actual contract.
        os.rename(path, tmp_path / "renamed.txt")
        path.write_bytes(b"new")
        assert stream.read() == b"old"
    assert path.read_bytes() == b"new"


def test_read_race_returns_pending(tmp_path):
    from notes.infrastructure.files import _shared_reader
    from contextlib import contextmanager
    path = tmp_path / "doc.txt"
    path.write_bytes(b"old")
    @contextmanager
    def changing_reader(file):
        with _shared_reader(file) as stream:
            yield stream
        file.write_bytes(b"new content")
    with patch("notes.infrastructure.files._shared_reader", changing_reader):
        assert read_document(path).status == "pending"


def test_reparse_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("outside")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks unavailable for this account")
    with pytest.raises(NotesError):
        validate_document_path(root, "link/secret.txt")
    assert scan_directory(root).entries == ()
    assert list_folders(root) == ("",)
    assert read_document(link / "secret.txt").status == "error"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_windows_junction_rejected_without_symlink_privilege(tmp_path):
    import _winapi
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("outside")
    root = tmp_path / "library"
    root.mkdir()
    junction = root / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    assert junction.is_junction()
    with pytest.raises(NotesError):
        validate_library_root(str(junction), tmp_path / "data")
    with pytest.raises(NotesError):
        validate_document_path(root, "junction/secret.txt")
    with pytest.raises(NotesError):
        create_document(root, "junction", "new.txt")
    assert not (target / "new.txt").exists()
    assert read_document(junction / "secret.txt").status == "error"
    assert scan_directory(root).entries == ()
    assert list_folders(root) == ("",)
