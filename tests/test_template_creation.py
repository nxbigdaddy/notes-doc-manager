"""Exclusive initial-text creation, including partial-file failure boundaries."""

import codecs
import errno
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from notes.domain.models import NotesError
from notes.infrastructure import files


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", [
    "", ".", "..", "CON.txt", "conin$", "CONOUT$.md", "LPT¹.txt", "NUL",
    "bad/name.txt", "bad\\name.txt", "note.txt:stream", "bad?.txt", "tail.txt.",
    "tail.txt ", "bad\x00.txt", "bad\ud800.txt", None, 123, b"file.txt",
])
def test_invalid_names_are_rejected_by_pure_lexical_helper(name):
    with patch.object(Path, "stat", side_effect=AssertionError("No filesystem calls")):
        with patch.object(files, "_local_volume", side_effect=AssertionError("No volume calls")):
            with pytest.raises(NotesError) as error:
                files.validate_document_name(name)
    assert error.value.code == "invalid_path"


def test_name_helper_preserves_legal_unicode_and_case():
    assert files.validate_document_name("中文 📝 & (Draft).TXT") == "中文 📝 & (Draft).TXT"


def test_name_limit_counts_utf16_units_without_accessing_filesystem():
    assert files.validate_document_name("a" * 251 + ".txt")
    assert files.validate_document_name("😀" * 125 + ".txt")
    for name in ("a" * 252 + ".txt", "😀" * 126 + ".txt"):
        with patch.object(Path, "stat", side_effect=AssertionError("No filesystem calls")):
            with pytest.raises(NotesError) as error:
                files.validate_document_name(name)
        assert error.value.code == "invalid_path"


@pytest.mark.parametrize("relative_dir", [
    ".git", ".GIT", "project/.git", "nested/.GiT/sub", ".venv", "tools/__PYCACHE__",
])
def test_excluded_directory_components_rejected_before_filesystem_access(tmp_path, relative_dir):
    with patch.object(files, "validate_document_path",
                      side_effect=AssertionError("Excluded directory must not reach filesystem")):
        with pytest.raises(NotesError) as error:
            files.create_document(
                tmp_path, relative_dir, "new.txt", initial_text="body",
                excluded_dirs=(".git", ".venv", "__pycache__"),
            )
    assert error.value.code == "out_of_scope"
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("relative_dir", [".github", ".git-backup", "Git", "project/git-notes"])
def test_similarly_named_nonexcluded_directories_are_allowed(tmp_path, relative_dir):
    target = tmp_path.joinpath(*relative_dir.split("/"))
    target.mkdir(parents=True)
    entry = files.create_document(
        tmp_path, relative_dir, "new.txt", initial_text="body", excluded_dirs=(".git",),
    )
    assert entry.relative_path == relative_dir + "/new.txt"
    assert (target / "new.txt").read_bytes() == b"body"


def test_default_empty_exclusion_list_preserves_direct_api_compatibility(tmp_path):
    (tmp_path / ".git").mkdir()
    entry = files.create_document(tmp_path, ".git", "new.txt")
    assert entry.relative_path == ".git/new.txt"
    assert (tmp_path / ".git" / "new.txt").read_bytes() == b""


@pytest.mark.parametrize("text,code", [
    (None, "invalid_content"), (b"bytes", "invalid_content"), (123, "invalid_content"),
    ("prefix\x00suffix", "invalid_content"), ("\x01control", "invalid_content"),
    ("bad\ud800text", "invalid_content"), ("bad\udffftail", "invalid_content"),
    ("\ufeffleading BOM", "invalid_content"), ("delete\x7f", "invalid_content"),
    ("next\x85line", "invalid_content"), ("c1\x9f", "invalid_content"),
    ("line\u2028separator", "invalid_content"), ("paragraph\u2029separator", "invalid_content"),
    ("x" * (files.INITIAL_TEXT_LIMIT + 1), "content_too_large"),
    ("中" * (files.INITIAL_TEXT_LIMIT // 3 + 1), "content_too_large"),
], ids=["none", "bytes", "integer", "nul", "control", "high-surrogate", "low-surrogate",
        "leading-bom", "delete-control", "c1-nel", "c1-control", "line-separator",
        "paragraph-separator", "large-ascii", "large-utf8"])
def test_invalid_initial_content_is_rejected_before_path_io_or_creation(tmp_path, text, code):
    with patch.object(files, "validate_document_path", side_effect=AssertionError("Must validate text first")):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "new.txt", initial_text=text)
    assert error.value.code == code
    assert tuple(tmp_path.iterdir()) == ()


def test_utf8_lf_without_added_bom_and_current_metadata(tmp_path):
    initial = "# 标题 📝\r\n\r\n第一行\r第二行\n\t缩进\n"
    expected = "# 标题 📝\n\n第一行\n第二行\n\t缩进\n".encode("utf-8")
    entry = files.create_document(tmp_path, "", "中文.TXT", initial_text=initial)
    path = tmp_path / "中文.TXT"
    assert path.read_bytes() == expected
    assert not path.read_bytes().startswith(codecs.BOM_UTF8)
    assert entry.size_bytes == len(expected)
    assert entry.modified_at == path.stat().st_mtime_ns // 1_000_000
    assert entry == files.get_file_entry(tmp_path, "中文.TXT")
    if os.name == "nt":
        assert entry.file_identity.startswith("win-ntfs-v1:")


def test_legitimate_zwj_emoji_and_internal_format_character_are_not_stripped(tmp_path):
    text = "开发 👩🏽‍💻\n内联\ufeff标记\n"
    files.create_document(tmp_path, "", "unicode.md", initial_text=text)
    assert (tmp_path / "unicode.md").read_bytes() == text.encode("utf-8")


def test_exact_utf8_byte_limit_and_old_empty_default_remain_supported(tmp_path):
    text = "中" * (files.INITIAL_TEXT_LIMIT // 3) + "x"
    assert len(text.encode("utf-8")) == files.INITIAL_TEXT_LIMIT
    entry = files.create_document(tmp_path, "", "limit.md", initial_text=text)
    assert entry.size_bytes == files.INITIAL_TEXT_LIMIT
    empty = files.create_document(tmp_path, "", "blank.txt")
    assert empty.size_bytes == 0
    assert (tmp_path / "blank.txt").read_bytes() == b""
    normalized = files.create_document(tmp_path, "", "lf.txt",
                                       initial_text="\r\n" * files.INITIAL_TEXT_LIMIT)
    assert normalized.size_bytes == files.INITIAL_TEXT_LIMIT
    assert (tmp_path / "lf.txt").read_bytes() == b"\n" * files.INITIAL_TEXT_LIMIT


def test_content_is_never_executed_or_treated_as_a_template_program(tmp_path):
    text = "__import__('os').system('not executed')\n{{ raw_placeholder }}\n${literal}\n"
    with patch.object(files.os, "system", side_effect=AssertionError("Do not execute text")):
        files.create_document(tmp_path, "", "literal.md", initial_text=text)
    assert (tmp_path / "literal.md").read_text(encoding="utf-8") == text


def test_existing_file_is_never_overwritten_or_appended(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"original user content\x00including arbitrary bytes")
    before = sha256(existing)
    with pytest.raises(NotesError) as error:
        files.create_document(tmp_path, "", "existing.txt", initial_text="replacement")
    assert error.value.code == "already_exists"
    assert sha256(existing) == before


def test_another_writer_wins_race_without_being_overwritten(tmp_path):
    target = tmp_path / "race.txt"
    original_open = os.open
    def competitor(path, flags, mode=0o777):
        descriptor = original_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                   | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(descriptor, b"another writer won")
        finally:
            os.close(descriptor)
        return original_open(path, flags, mode)
    with patch.object(files.os, "open", competitor):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", target.name, initial_text="ours")
    assert error.value.code == "already_exists"
    assert target.read_bytes() == b"another writer won"


def test_two_real_concurrent_creators_have_exactly_one_winner(tmp_path):
    def attempt(text):
        try:
            return files.create_document(tmp_path, "", "same.txt", initial_text=text)
        except NotesError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("first text", "second text")))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert results.count("already_exists") == 1
    assert (tmp_path / "same.txt").read_bytes() in (b"first text", b"second text")


class FaultingWriter:
    def __init__(self, stream, fault, cancel=None):
        self.stream = stream
        self.fault = fault
        self.cancel = cancel

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()
        if self.fault == "close":
            raise OSError(errno.ENOSPC, "controlled close failure")

    def write(self, value):
        if self.fault == "short":
            part = value[:len(value) // 2]
            return self.stream.write(part)
        if self.fault == "none":
            return None
        if self.fault == "write":
            self.stream.write(value[:2])
            raise OSError(errno.ENOSPC, "controlled write failure")
        result = self.stream.write(value)
        if self.cancel is not None:
            self.cancel.set()
        return result

    def flush(self):
        if self.fault == "flush":
            raise OSError(errno.ENOSPC, "controlled flush failure")
        return self.stream.flush()

    def fileno(self):
        return self.stream.fileno()


@pytest.mark.parametrize("fault", ["short", "none", "write", "flush", "close"])
def test_write_failures_preserve_new_file_and_report_created_partial(tmp_path, fault):
    original = tmp_path / "original.txt"
    original.write_text("untouched original")
    before = sha256(original)
    original_fdopen = os.fdopen
    def wrap(descriptor, mode):
        assert mode == "wb"
        return FaultingWriter(original_fdopen(descriptor, mode), fault)
    with patch.object(files.os, "fdopen", wrap):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "partial.txt", initial_text="template content")
    assert error.value.code == "created_partial"
    assert "文件已建立" in error.value.message
    assert str((tmp_path / "partial.txt").resolve()) in error.value.message
    assert (tmp_path / "partial.txt").is_file()
    assert sha256(original) == before


def test_fsync_failure_does_not_claim_no_file_was_created(tmp_path):
    with patch.object(files.os, "fsync", side_effect=OSError(errno.ENOSPC, "controlled fsync failure")):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "partial.txt", initial_text="written but not confirmed durable")
    assert error.value.code == "created_partial"
    assert (tmp_path / "partial.txt").read_bytes() == b"written but not confirmed durable"


def test_fd_wrapper_failure_preserves_empty_file_and_closes_descriptor(tmp_path):
    descriptors = []
    original_open = os.open
    def capture(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor
    with patch.object(files.os, "open", capture):
        with patch.object(files.os, "fdopen", side_effect=OSError("controlled fd wrapping failure")):
            with pytest.raises(NotesError) as error:
                files.create_document(tmp_path, "", "partial.txt", initial_text="body")
    assert error.value.code == "created_partial"
    assert (tmp_path / "partial.txt").read_bytes() == b""
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("failure", [
    NotesError("unavailable", "metadata cannot be read"),
    PermissionError("metadata denied"),
    FileExistsError("post-creation failure must not be labelled already_exists"),
    RuntimeError("unexpected metadata failure"),
])
def test_metadata_failure_never_deletes_successfully_written_text(tmp_path, failure):
    with patch.object(files, "get_file_entry", side_effect=failure):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "partial.txt", initial_text="complete bytes remain")
    assert error.value.code == "created_partial"
    assert str((tmp_path / "partial.txt").resolve()) in error.value.message
    assert (tmp_path / "partial.txt").read_bytes() == b"complete bytes remain"


def test_failure_to_open_creates_nothing_and_is_not_partial(tmp_path):
    with patch.object(files.os, "open", side_effect=PermissionError("controlled refusal")):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "new.txt", initial_text="body")
    assert error.value.code == "create_failed"
    assert tuple(tmp_path.iterdir()) == ()


def test_cancellation_before_creation_does_not_create_a_file(tmp_path):
    cancel = Event()
    cancel.set()
    with patch.object(files.os, "open", side_effect=AssertionError("No creation after cancel")):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "new.txt", initial_text="body", cancel=cancel)
    assert error.value.code == "cancelled"
    assert tuple(tmp_path.iterdir()) == ()


def test_cancel_during_path_validation_still_precedes_exclusive_create(tmp_path):
    cancel = Event()
    original_validate = files.validate_document_path
    def validate_then_cancel(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        cancel.set()
        return result
    with patch.object(files, "validate_document_path", validate_then_cancel):
        with patch.object(files.os, "open", side_effect=AssertionError("No creation after cancel")):
            with pytest.raises(NotesError) as error:
                files.create_document(tmp_path, "", "new.txt", initial_text="body", cancel=cancel)
    assert error.value.code == "cancelled"
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("boundary", ["open", "write", "fsync", "metadata"])
def test_cancellation_after_creation_is_honestly_reported_as_partial(tmp_path, boundary):
    cancel = Event()
    original_open = os.open
    original_fdopen = os.fdopen
    original_fsync = os.fsync
    original_entry = files.get_file_entry

    def open_then_cancel(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        cancel.set()
        return descriptor

    def write_then_cancel(descriptor, mode):
        return FaultingWriter(original_fdopen(descriptor, mode), "", cancel=cancel)

    def fsync_then_cancel(descriptor):
        original_fsync(descriptor)
        cancel.set()

    def entry_then_cancel(*args, **kwargs):
        entry = original_entry(*args, **kwargs)
        cancel.set()
        return entry

    target, replacement = {
        "open": ("open", open_then_cancel),
        "write": ("fdopen", write_then_cancel),
        "fsync": ("fsync", fsync_then_cancel),
        "metadata": ("get_file_entry", entry_then_cancel),
    }[boundary]
    owner = files if boundary == "metadata" else files.os
    with patch.object(owner, target, replacement):
        with pytest.raises(NotesError) as error:
            files.create_document(tmp_path, "", "partial.txt", initial_text="body", cancel=cancel)
    assert error.value.code == "created_partial"
    assert (tmp_path / "partial.txt").is_file()
    if boundary == "open":
        assert (tmp_path / "partial.txt").read_bytes() == b""
    else:
        assert (tmp_path / "partial.txt").read_bytes() == b"body"
