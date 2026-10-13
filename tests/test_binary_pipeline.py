"""Black-box file/Service regressions for decoded-text binary classification.

Expected outcomes are fixed public semantics, not calls into the optimized
classifier. All documents are test-owned; no editor, native GUI, or hotkey is
started. The only 5 MiB input is read directly and then grows beyond the index
limit before Service sees it, so this is not a large full-text benchmark.
"""
import codecs
import hashlib
import os
from pathlib import Path
from threading import Event

import pytest

from notes.application.service import ApplicationService
from notes.infrastructure.files import read_document

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows local-NTFS document pipeline")

MARKER = "PIPELINE_MARKER"
PREVIEW_BYTES = 256 * 1024
INDEX_BYTES = 5 * 1024 * 1024


def fingerprint(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(text, encoding):
    if encoding == "utf8":
        return text.encode("utf-8")
    if encoding == "utf8_bom":
        return codecs.BOM_UTF8 + text.encode("utf-8")
    if encoding == "utf16le_bom":
        return codecs.BOM_UTF16_LE + text.encode("utf-16-le")
    if encoding == "utf16be_bom":
        return codecs.BOM_UTF16_BE + text.encode("utf-16-be")
    raise AssertionError("test fixture encoding must be explicit")


class Pipeline:
    def __init__(self, root):
        self.root = root / "documents"
        self.root.mkdir()
        self.data = root / "metadata"
        self.service = None
        self.original_hashes = {}
        self.documents = {}

    def start(self, entries):
        assert self.service is None
        for name, raw in entries.items():
            path = self.root / name
            assert path.resolve().is_relative_to(self.root.resolve())
            path.write_bytes(raw)
            self.original_hashes[path] = fingerprint(path)
        self.service = ApplicationService(self.data, monitoring=False)
        library = self.service.call("add_library", {"root_path": str(self.root)})
        self.service.call("scan", {"library_id": library.id})
        result = self.service.call("query", {"mode": "name", "page_size": 100})
        assert result.total == len(entries)
        self.documents = {hit.document.file_name: hit.document for hit in result.items}
        assert len(self.documents) == len(entries)
        return self

    def preview(self, name):
        return self.service.call("preview", {"document_id": self.documents[name].id}).read

    def indexed_names(self):
        result = self.service.call("query", {"mode": "content", "text": MARKER, "page_size": 100})
        return {hit.document.file_name for hit in result.items}

    def close(self):
        if self.service:
            self.service.close()
        for path, before in self.original_hashes.items():
            assert fingerprint(path) == before, f"Reading/indexing modified a source: {path.name}"


@pytest.fixture
def pipeline(tmp_path):
    instance = Pipeline(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("encoding", ["utf8", "utf8_bom", "utf16le_bom", "utf16be_bom"])
@pytest.mark.parametrize("filler", ["汉", "😀"])
def test_threshold_uses_decoded_characters_exact_two_percent_and_just_over(pipeline, encoding, filler):
    # Two controls / 100 decoded characters is allowed. The same two controls
    # / 99 characters is just over 2%, despite multibyte UTF-8/UTF-16 encodings.
    exact = MARKER + filler * (100 - len(MARKER) - 2) + "\x01\x02"
    over = MARKER + filler * (99 - len(MARKER) - 2) + "\x01\x02"
    assert len(exact) == 100 and len(over) == 99
    pipeline.start({"exact.txt": encoded(exact, encoding), "over.txt": encoded(over, encoding)})
    assert pipeline.documents["exact.txt"].index_status == "ready"
    assert pipeline.documents["over.txt"].index_status == "skipped_binary"
    assert pipeline.preview("exact.txt").text == exact
    bad = pipeline.preview("over.txt")
    assert bad.status == "skipped_binary" and bad.error_code == "binary"
    assert bad.text == ""
    assert pipeline.indexed_names() == {"exact.txt"}


def test_each_non_whitelisted_c0_is_counted_including_controls_at_the_end(pipeline):
    entries = {}
    expected = set()
    for code in range(1, 32):
        if code in (9, 10, 12, 13):
            continue
        control = chr(code)
        accepted = MARKER + "x" * (100 - len(MARKER) - 2) + control * 2
        rejected = MARKER + "x" * (100 - len(MARKER) - 3) + control * 3
        entries[f"exact-{code:02d}.txt"] = accepted.encode("utf-8")
        entries[f"over-{code:02d}.txt"] = rejected.encode("utf-8")
        expected.add(f"exact-{code:02d}.txt")
    pipeline.start(entries)
    for name, document in pipeline.documents.items():
        assert document.index_status == ("ready" if name in expected else "skipped_binary")
    assert pipeline.indexed_names() == expected


def test_tab_lf_cr_ff_del_and_c1_are_preserved_not_counted_as_binary_controls(pipeline):
    allowed = MARKER + ("\t\n\r\f" * 30)
    del_c1 = MARKER + ("".join(chr(code) for code in range(0x7F, 0xA0)) * 10)
    pipeline.start({
        "allowed-whitespace.txt": allowed.encode("utf-8"),
        "del-c1.txt": del_c1.encode("utf-8"),
        "del-c1-utf16.txt": encoded(del_c1, "utf16le_bom"),
    })
    assert all(document.index_status == "ready" for document in pipeline.documents.values())
    assert pipeline.preview("allowed-whitespace.txt").text == allowed
    assert pipeline.preview("del-c1.txt").text == del_c1
    assert pipeline.preview("del-c1-utf16.txt").text == del_c1
    assert pipeline.indexed_names() == set(pipeline.documents)


@pytest.mark.parametrize("encoding", ["utf8", "utf8_bom", "utf16le_bom", "utf16be_bom"])
def test_sparse_nul_is_binary_anywhere_even_far_below_two_percent(pipeline, encoding):
    text = MARKER + "a" * 10000
    texts = {"first.txt": "\x00" + text,
             "middle.txt": text[:5000] + "\x00" + text[5000:],
             "last.txt": text + "\x00"}
    pipeline.start({name: encoded(value, encoding) for name, value in texts.items()})
    if encoding == "utf16le_bom":
        # FF FE followed by a UTF-16 NUL is also the UTF-32LE BOM. The existing
        # longest-BOM-first rule reaches decode failure for this fixture before
        # binary classification. Do not mislabel that legacy decoding priority
        # as a classifier regression: explicitly choose UTF-16, then test NUL.
        first = pipeline.documents["first.txt"]
        assert first.index_status == "needs_encoding"
        pipeline.service.call("set_encoding", {"document_id": first.id, "encoding": "utf-16"})
        refreshed = pipeline.service.call("query", {"mode": "name", "text": "first.txt"})
        pipeline.documents["first.txt"] = refreshed.items[0].document
    for name, document in pipeline.documents.items():
        assert document.index_status == "skipped_binary"
        preview = pipeline.preview(name)
        assert preview.status == "skipped_binary" and preview.error_code == "binary"
        assert not preview.text
    assert pipeline.indexed_names() == set()


@pytest.mark.parametrize("tail", ["nul", "controls"])
def test_preview_prefix_can_be_text_while_full_index_rejects_binary_tail(pipeline, tail):
    total = 512 * 1024
    bad = b"\x00" if tail == "nul" else b"\x01" * (total // 50 + 1)
    raw = MARKER.encode() + b"x" * (total - len(MARKER) - len(bad)) + bad
    assert len(raw) == total
    pipeline.start({"tail.txt": raw})
    full = read_document(pipeline.root / "tail.txt")
    assert full.status == "skipped_binary" and full.error_code == "binary"
    assert pipeline.documents["tail.txt"].index_status == "skipped_binary"
    preview = pipeline.preview("tail.txt")
    assert preview.status == "ready" and preview.truncated
    assert len(preview.text) == PREVIEW_BYTES
    assert preview.text.startswith(MARKER)
    assert "\x00" not in preview.text and "\x01" not in preview.text
    assert pipeline.indexed_names() == set()
    assert pipeline.service.call("query", {"mode": "name", "text": "tail.txt"}).total == 1


def test_exact_two_percent_large_tail_is_not_mistaken_for_over_threshold(pipeline):
    # 10,000 tail controls / 500,000 decoded ASCII characters = exactly 2%.
    raw = MARKER.encode() + b"x" * (490000 - len(MARKER)) + b"\x02" * 10000
    assert len(raw) == 500000
    pipeline.start({"exact-tail.txt": raw})
    assert pipeline.documents["exact-tail.txt"].index_status == "ready"
    full = read_document(pipeline.root / "exact-tail.txt")
    assert full.status == "ready" and full.text.endswith("\x02" * 10000)
    preview = pipeline.preview("exact-tail.txt")
    assert preview.status == "ready" and preview.truncated
    assert len(preview.text) == PREVIEW_BYTES and "\x02" not in preview.text
    assert pipeline.indexed_names() == {"exact-tail.txt"}


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_explicit_utf16_decoding_precedes_binary_decision_and_reindexes(pipeline, encoding):
    accepted = MARKER + "a" * (100 - len(MARKER) - 2) + "\x01\x02"
    rejected = MARKER + "a" * (99 - len(MARKER) - 2) + "\x01\x02"
    pipeline.start({"good.txt": accepted.encode(encoding), "bad.txt": rejected.encode(encoding)})
    # No BOM: do not require an encoding heuristic. Explicitly selecting the
    # documented encoding must evaluate Unicode characters, not raw zero bytes.
    for name in pipeline.documents:
        pipeline.service.call("set_encoding", {"document_id": pipeline.documents[name].id,
                                               "encoding": encoding})
    assert pipeline.preview("good.txt").status == "ready"
    assert pipeline.preview("good.txt").text == accepted
    assert pipeline.preview("bad.txt").status == "skipped_binary"
    assert pipeline.indexed_names() == {"good.txt"}


def test_empty_text_and_bom_only_documents_do_not_divide_by_zero(pipeline):
    pipeline.start({"empty.txt": b"", "utf8-bom.txt": codecs.BOM_UTF8,
                    "utf16le-bom.txt": codecs.BOM_UTF16_LE,
                    "utf16be-bom.txt": codecs.BOM_UTF16_BE})
    for name, document in pipeline.documents.items():
        assert document.index_status == "ready"
        preview = pipeline.preview(name)
        assert preview.status == "ready" and preview.text == "" and not preview.truncated


def test_truncated_utf8_and_utf16_codepoints_stay_preview_only_not_decode_errors(pipeline):
    utf8 = MARKER.encode() + b"x" * (PREVIEW_BYTES - len(MARKER) - 1) + "😀".encode("utf-8")
    prefix = MARKER + "x" * ((PREVIEW_BYTES - 4) // 2 - len(MARKER))
    utf16 = codecs.BOM_UTF16_LE + (prefix + "😀").encode("utf-16-le")
    assert len(utf8) == PREVIEW_BYTES + 3
    assert len(utf16) == PREVIEW_BYTES + 2
    pipeline.start({"utf8-boundary.txt": utf8, "utf16-boundary.txt": utf16})
    for name in pipeline.documents:
        preview = pipeline.preview(name)
        full = read_document(pipeline.root / name)
        assert preview.status == "ready" and preview.truncated
        assert not preview.text.endswith("😀")
        assert full.status == "ready" and full.text.endswith("😀")
        assert pipeline.documents[name].index_status == "ready"
    assert pipeline.indexed_names() == set(pipeline.documents)


def test_decode_error_is_not_relabelled_binary_and_preview_does_not_read_invalid_tail(pipeline):
    raw = MARKER.encode() + b"x" * (PREVIEW_BYTES - len(MARKER)) + b"\xff\x01\x02"
    pipeline.start({"invalid-tail.txt": raw})
    assert pipeline.documents["invalid-tail.txt"].index_status == "needs_encoding"
    full = read_document(pipeline.root / "invalid-tail.txt")
    assert full.status == "needs_encoding"
    preview = pipeline.preview("invalid-tail.txt")
    assert preview.status == "ready" and preview.truncated
    assert preview.text.startswith(MARKER)
    assert pipeline.indexed_names() == set()


def test_pre_cancelled_read_stays_pending_before_binary_classification(pipeline):
    pipeline.start({"cancelled.txt": MARKER.encode() + b"\x00"})
    path = pipeline.root / "cancelled.txt"
    cancel = Event()
    cancel.set()
    for preview in (False, True):
        read = read_document(path, preview=preview, cancel=cancel)
        assert read.status == "pending" and read.error_code == "cancelled"
        assert read.text == ""
    assert pipeline.documents["cancelled.txt"].index_status == "skipped_binary"


def test_five_mib_limit_and_larger_skip_remain_distinct_from_binary_detection(pipeline):
    path = pipeline.root / "limit.txt"
    raw = MARKER.encode() + b"x" * (INDEX_BYTES - len(MARKER))
    path.write_bytes(raw)
    before = fingerprint(path)
    exact = read_document(path)
    assert exact.status == "ready" and not exact.truncated
    assert len(exact.text) == INDEX_BYTES
    assert fingerprint(path) == before
    # Service receives only the oversized file, not a 5 MiB FTS benchmark. The
    # size gate wins before the NUL byte at the end is examined.
    pipeline.start({"limit.txt": raw + b"\x00"})
    assert pipeline.documents["limit.txt"].index_status == "skipped_large"
    full = read_document(path)
    assert full.status == "skipped_large" and full.error_code == "too_large"
    preview = pipeline.preview("limit.txt")
    assert preview.status == "ready" and preview.truncated
    assert len(preview.text) == PREVIEW_BYTES
    assert pipeline.indexed_names() == set()
