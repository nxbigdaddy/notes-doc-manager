"""Real temporary NTFS file identities; no content-based rename guesses."""

import os
import shutil
import stat
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from notes.domain.models import NotesError
from notes.infrastructure import files


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS identity")
def test_rename_preserves_identity_but_copy_and_recreate_do_not(tmp_path):
    original = tmp_path / "Original.txt"
    original.write_text("same content", encoding="utf-8")
    before = files.get_file_entry(tmp_path, original.name)
    assert before.file_identity.startswith("win-ntfs-v1:")
    renamed = tmp_path / "Renamed.TXT"
    original.rename(renamed)
    after = files.get_file_entry(tmp_path, renamed.name)
    assert after.file_identity == before.file_identity
    assert after.relative_path == "Renamed.TXT"
    with pytest.raises(NotesError) as error:
        files.get_file_entry(tmp_path, original.name)
    assert error.value.code == "missing"
    copied = tmp_path / "Copy.txt"
    shutil.copy2(renamed, copied)
    copy_entry = files.get_file_entry(tmp_path, copied.name)
    assert copy_entry.file_identity and copy_entry.file_identity != after.file_identity
    assert copied.read_bytes() == renamed.read_bytes()
    assert copied.stat().st_mtime_ns == renamed.stat().st_mtime_ns
    renamed.unlink()
    renamed.write_text("same content", encoding="utf-8")
    recreated = files.get_file_entry(tmp_path, renamed.name)
    assert recreated.file_identity and recreated.file_identity != after.file_identity


@pytest.mark.skipif(os.name != "nt", reason="Windows stat/fstat birth time")
def test_path_stat_and_shared_handle_birth_identity_agree(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("body")
    by_path = path.stat()
    by_lstat = path.lstat()
    with files._shared_reader(path) as stream:
        by_handle = os.fstat(stream.fileno())
    for name in ("st_dev", "st_ino", "st_birthtime_ns", "st_nlink"):
        assert getattr(by_path, name) == getattr(by_lstat, name) == getattr(by_handle, name)
    assert files._file_identity(by_path, True) == files._file_identity(by_handle, True)
    # ctime is intentionally not asserted: Windows path and handle ctime differ.


@pytest.mark.skipif(os.name != "nt", reason="Windows hard-link identity policy")
def test_hardlinked_files_have_no_automatic_move_identity(tmp_path):
    original = tmp_path / "first.txt"
    original.write_text("body")
    assert files.get_file_entry(tmp_path, original.name).file_identity
    alias = tmp_path / "second.txt"
    os.link(original, alias)
    assert original.stat().st_nlink == 2
    assert files.get_file_entry(tmp_path, original.name).file_identity == ""
    assert files.get_file_entry(tmp_path, alias.name).file_identity == ""
    result = files.scan_directory(tmp_path)
    assert result.complete and len(result.entries) == 2
    assert all(entry.file_identity == "" for entry in result.entries)


@pytest.mark.skipif(os.name != "nt", reason="Windows identity capability")
def test_scan_uses_real_stat_not_zero_identity_directory_entry(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("body")
    actual = path.stat()
    expected = files.get_file_entry(tmp_path, path.name)
    child = SimpleNamespace(
        name=path.name,
        path=str(path),
        stat=lambda **kwargs: SimpleNamespace(
            st_mode=actual.st_mode, st_file_attributes=actual.st_file_attributes,
            st_dev=0, st_ino=0, st_nlink=0, st_size=actual.st_size,
            st_mtime_ns=actual.st_mtime_ns, st_birthtime_ns=actual.st_birthtime_ns,
        ),
    )
    @contextmanager
    def directory(_path):
        yield iter((child,))
    with patch.object(files.os, "scandir", directory):
        result = files.scan_directory(tmp_path)
    assert result.complete
    assert result.entries == (expected,)
    assert result.entries[0].file_identity


@pytest.mark.skipif(os.name != "nt", reason="Windows identity fields")
@pytest.mark.parametrize("missing", ["st_dev", "st_ino", "st_birthtime_ns", "st_nlink"])
def test_identity_missing_or_zero_fields_never_guessed(missing):
    values = dict(st_dev=123, st_ino=456, st_birthtime_ns=789, st_nlink=1)
    assert files._file_identity(SimpleNamespace(**values), True)
    del values[missing]
    assert files._file_identity(SimpleNamespace(**values), True) == ""
    values[missing] = 0
    assert files._file_identity(SimpleNamespace(**values), True) == ""


def test_unsupported_platform_or_volume_has_empty_identity(tmp_path):
    info = SimpleNamespace(st_dev=123, st_ino=456, st_birthtime_ns=789, st_nlink=1)
    assert files._file_identity(info, False) == ""
    with patch.object(files.os, "name", "posix"):
        assert files._file_identity(info, True) == ""
    path = tmp_path / "document.txt"
    path.write_text("body")
    with patch.object(files, "_identity_supported", return_value=False):
        assert files.get_file_entry(tmp_path, path.name).file_identity == ""
        assert files.scan_directory(tmp_path).entries[0].file_identity == ""


def test_file_entry_collection_does_not_read_document_body(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("body")
    with patch.object(files, "_shared_reader", side_effect=AssertionError("No body reads")):
        entry = files.get_file_entry(tmp_path, path.name)
        scanned = files.scan_directory(tmp_path)
    assert entry.size_bytes == 4
    assert scanned.entries == (entry,)


def test_changing_metadata_rejects_identity_snapshot(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("body")
    checker = files._plain_file
    changed = False
    def check_and_change(info):
        nonlocal changed
        checker(info)
        if not changed:
            changed = True
            path.write_text("longer replacement body")
    with patch.object(files, "_plain_file", check_and_change):
        with pytest.raises(NotesError) as error:
            files.get_file_entry(tmp_path, path.name)
    assert error.value.code == "changed"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction rejection")
def test_get_file_entry_rejects_directory_junction(tmp_path):
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.txt").write_text("body")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    _winapi.CreateJunction(str(outside), str(link))
    with pytest.raises(NotesError) as error:
        files.get_file_entry(root, "link/document.txt")
    assert error.value.code == "reparse_point"
    assert files.scan_directory(root).entries == ()


def test_nonregular_file_never_has_metadata_entry(tmp_path):
    directory = tmp_path / "folder"
    directory.mkdir()
    assert stat.S_ISDIR(directory.stat().st_mode)
    with pytest.raises(NotesError):
        files.get_file_entry(tmp_path, directory.name)
