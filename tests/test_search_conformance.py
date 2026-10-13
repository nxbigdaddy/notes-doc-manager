"""Black-box search conformance against a small independent literal-search model."""

from __future__ import annotations

import random
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from unittest.mock import patch

import pytest

from notes.domain.models import FileEntry, ReadResult, SearchQuery
from notes.infrastructure.store import Store


def folded(value):
    """The documented search normalization, not a call to the production helper."""
    first = unicodedata.normalize("NFC", value)
    return unicodedata.normalize("NFC", first.casefold())


def body_text(value):
    return folded(value.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass
class Example:
    identifier: str
    library_id: str
    relative: str
    body: str
    modified: int
    ready: bool = True
    present: bool = True
    in_scope: bool = True
    favorite: bool = False
    tags: set[str] = field(default_factory=set)
    opened: int | None = None

    @property
    def name(self):
        return PureWindowsPath(self.relative).name


def reference(examples, libraries, query):
    """Implement product semantics with plain Python sets and substring membership."""
    needle = folded(query.text.strip())
    eligible = []
    folder_parts = tuple(part.lower() for part in PureWindowsPath(query.folder).parts)
    for example in examples:
        enabled, online = libraries[example.library_id]
        if not enabled or not example.in_scope:
            continue
        if query.library_id and example.library_id != query.library_id:
            continue
        if not query.include_unavailable and (not online or not example.present):
            continue
        document_parts = tuple(part.lower() for part in PureWindowsPath(example.relative).parts)
        if folder_parts and document_parts[:len(folder_parts)] != folder_parts:
            continue
        if query.favorites_only and not example.favorite:
            continue
        if not set(query.tag_ids).issubset(example.tags):
            continue
        if query.recent_only and example.opened is None:
            continue
        eligible.append(example)
    if query.recent_only:
        eligible.sort(key=lambda e: (-e.opened, e.identifier))
        eligible = eligible[:100]
    covered = sum(example.ready for example in eligible)
    hits = []
    for example in eligible:
        name_match = bool(needle and needle in folded(example.name))
        content_match = bool(example.ready and needle and needle in body_text(example.body))
        if (not needle or (query.mode == "all" and (name_match or content_match))
                or (query.mode == "name" and name_match)
                or (query.mode == "content" and content_match)):
            hits.append((example, name_match, content_match))
    if query.recent_only:
        hits.sort(key=lambda row: (-row[0].opened, row[0].identifier))
    else:
        hits.sort(key=lambda row: (not row[1], -row[0].modified, row[0].identifier))
    start = query.page * query.page_size
    return hits[start:start + query.page_size], len(hits), covered, len(eligible) - covered


def assert_matches(store, examples, libraries, query):
    expected, total, covered, uncovered = reference(examples, libraries, query)
    actual = store.search(query)
    assert actual.complete, (query, actual.warning)
    assert actual.total == total, (query, actual.total, total)
    assert actual.covered == covered, (query, actual.covered, covered)
    assert actual.uncovered == uncovered, (query, actual.uncovered, uncovered)
    assert [hit.document.id for hit in actual.items] == [row[0].identifier for row in expected], query
    assert len({hit.document.id for hit in actual.items}) == len(actual.items)
    for hit, (example, name_match, content_match) in zip(actual.items, expected, strict=True):
        assert hit.matched_name == name_match
        if content_match and query.mode != "name":
            needle = folded(query.text.strip())
            text = body_text(example.body)
            assert needle.replace("\n", " ") in hit.snippet, (query, hit.snippet)
            assert hit.line == text.count("\n", 0, text.index(needle)) + 1
    return actual


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("search-conformance")
    store = Store(root / "data")
    randomizer = random.Random(20260915)
    tokens = (
        "中文", "服务器", "文档管理", "Alpha", "Straße", "Cafe\u0301", "İstanbul",
        "ΟΣ σς", "📝😀", "👩‍💻", 'a"b', "%_*", "x\x00y", "line\r\nbreak",
        "line\rend", "literal OR NOT", "東京", "ß", "e\u0301", "  pads  ", "ﬃ",
    )
    names = ("alpha", "中文", "café", "cafe\u0301", "Straße", "%_", "📝", "İ", "東京")
    folders = ("", "work/", "work/sub/", "workshop/", "资料/", ".git/")
    tags = tuple(store.create_tag(name).id for name in ("工作", "归档", "Important"))
    examples = []
    libraries = {}
    actual_libraries = []
    try:
        for lib_index in range(3):
            directory = root / f"library-{lib_index}"
            directory.mkdir()
            library = store.add_library(directory)
            actual_libraries.append(library)
            libraries[library.id] = (True, lib_index != 2)
            generated = []
            for index in range(12):
                number = lib_index * 12 + index
                relative = f"{folders[number % len(folders)]}{names[number % len(names)]}-{number}.txt"
                body = "|".join((tokens[number % len(tokens)], *randomizer.sample(tokens, 5)))
                modified = 1000 + randomizer.randrange(5)  # Deliberately tied timestamps.
                generated.append((relative, body, modified))
            entries = tuple(FileEntry(path, len(text.encode()), modified)
                            for path, text, modified in generated)
            documents = store.apply_scan(library.id, entries, complete=True)
            by_path = {doc.relative_path: doc for doc in documents}
            reads = []
            for index, (relative, text, modified) in enumerate(generated):
                document = by_path[relative]
                number = lib_index * 12 + index
                ready = number % 11 != 0
                read = ReadResult(text, "utf-8", size_bytes=len(text.encode()), modified_at=modified)
                if not ready:
                    read = ReadResult(status="needs_encoding", error_code="decode_error")
                reads.append((document.id, document.content_generation, read))
                example = Example(document.id, library.id, relative, text, modified, ready)
                example.in_scope = ".git" not in PureWindowsPath(relative).parts
                example.tags = set(randomizer.sample(tags, randomizer.randrange(4)))
                example.favorite = number % 3 == 0
                if number % 2:
                    example.opened = 2000 + number * 17
                    with patch("notes.infrastructure.store.time.time_ns",
                               return_value=example.opened * 1_000_000):
                        store.mark_opened(document.id)
                store.set_document_tags(document.id, tuple(example.tags))
                store.set_favorite(document.id, example.favorite)
                examples.append(example)
            assert all(store.set_index_batch(reads))
        for index in (5, 19):
            store.invalidate_document(examples[index].identifier)
            examples[index].ready = False
        first_library = actual_libraries[0]
        missing = examples[7]
        retained = tuple(
            FileEntry(e.relative, len(e.body.encode()), e.modified)
            for e in examples if e.library_id == first_library.id and e is not missing
        )
        store.apply_scan(first_library.id, retained, complete=True)
        missing.present = missing.ready = False
        store.apply_scan(actual_libraries[2].id, (), complete=False, availability="offline")
        yield store, examples, libraries, tags, actual_libraries
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["all", "name", "content"])
def test_seeded_unicode_and_literal_queries_match_reference(corpus, mode):
    store, examples, libraries, _, _ = corpus
    for needle in (
        "", " ", "中", "中文", "中文文", "服务器", "alpha", "STRASSE", "cafe\u0301",
        "é", "İ", "i\u0307", "😀", "👩‍💻", '"', 'a"b', "%", "_", "*", "%_*",
        "x\x00y", "\x00", "y", "OR NOT", "line\nbreak", "line\rend", "東京", "ß",
        "does-not-exist", "  pads  ", "ﬃ", "FFI",
    ):
        assert_matches(store, examples, libraries,
                       SearchQuery(text=needle, mode=mode, page_size=1000))


@pytest.mark.parametrize("mode", ["all", "name", "content"])
def test_seeded_combined_filters_and_page_boundaries(corpus, mode):
    store, examples, libraries, tags, actual_libraries = corpus
    randomizer = random.Random(9015)
    for _ in range(30):
        query = SearchQuery(
            text=randomizer.choice(("", "中", "STRASSE", "%_", '"', "😀", "line")),
            mode=mode,
            library_id=randomizer.choice((None, *(lib.id for lib in actual_libraries))),
            folder=randomizer.choice(("", "work", "WORK", "work/sub", "workshop", "资料")),
            tag_ids=tuple(randomizer.sample(tags, randomizer.randrange(3))),
            favorites_only=randomizer.choice((True, False)),
            recent_only=randomizer.choice((True, False)),
            include_unavailable=randomizer.choice((True, False)),
            page=randomizer.randrange(3),
            page_size=randomizer.choice((1, 2, 5)),
        )
        assert_matches(store, examples, libraries, query)


def test_pagination_union_has_no_duplicates_or_missing_hits(corpus):
    store, examples, libraries, _, _ = corpus
    expected, total, _, _ = reference(examples, libraries, SearchQuery(page_size=1000))
    collected = []
    for page in range((total + 3) // 4 + 1):
        result = assert_matches(store, examples, libraries, SearchQuery(page=page, page_size=4))
        collected.extend(hit.document.id for hit in result.items)
    assert collected == [row[0].identifier for row in expected]
    assert len(collected) == len(set(collected))


def test_tag_filters_are_and_and_folder_prefixes_are_whole_components(corpus):
    store, examples, libraries, tags, _ = corpus
    for selected_tags in ((tags[0], tags[1]), (tags[1], tags[2]), tags):
        for folder in ("", "work", "WORK/", "work/sub", "workshop", ".git"):
            for favorites_only in (False, True):
                assert_matches(
                    store, examples, libraries,
                    SearchQuery(tag_ids=selected_tags, folder=folder,
                                favorites_only=favorites_only, page_size=1000),
                )


def test_precancelled_search_is_explicitly_incomplete(corpus):
    store, _, _, _, _ = corpus
    cancel = threading.Event()
    cancel.set()
    for mode in ("all", "name", "content"):
        result = store.search(SearchQuery(text="中", mode=mode), cancel=cancel)
        assert not result.complete
        assert result.warning


def register(store, library, relative, text, modified=100):
    store.apply_scan(library.id, (FileEntry(relative, len(text.encode()), modified),), complete=False)
    document = next(doc for doc in store.list_documents(library.id) if doc.relative_path == relative)
    assert store.set_index(document.id, document.content_generation,
                           ReadResult(text, "utf-8", size_bytes=len(text.encode()), modified_at=modified))
    return store.get_document(document.id)


def test_recent_open_order_and_limit_are_not_modified_time_order(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    try:
        library = store.add_library(root)
        entries = tuple(FileEntry(f"document-{i}.txt", 4, 50_000 - i) for i in range(105))
        documents = store.apply_scan(library.id, entries, complete=True)
        by_name = {doc.file_name: doc for doc in documents}
        reads = []
        ordered = []
        for index in range(105):
            document = by_name[f"document-{index}.txt"]
            with patch("notes.infrastructure.store.time.time_ns", return_value=(10_000 + index) * 1_000_000):
                store.mark_opened(document.id)
            ordered.append(document.id)
            reads.append((document.id, document.content_generation,
                          ReadResult("body", "utf-8", size_bytes=4, modified_at=50_000 - index)))
        assert all(store.set_index_batch(reads))
        result = store.search(SearchQuery(recent_only=True, page_size=1000))
        assert result.total == 100
        assert [hit.document.id for hit in result.items] == list(reversed(ordered))[:100]
        assert result.covered == 100 and result.uncovered == 0
        assert store.search(SearchQuery(recent_only=True, page=1, page_size=100)).items == ()
        # The recent collection is capped before applying a keyword.
        assert store.search(SearchQuery(recent_only=True, text="document-0.txt")).total == 0
        # Organizational filters, on the other hand, precede the recent cap.
        store.set_favorite(ordered[0], True)
        assert store.search(SearchQuery(
            recent_only=True, favorites_only=True, text="document-0.txt"
        )).total == 1
        tag = store.create_tag("rare")
        store.set_document_tags(ordered[0], (tag.id,))
        store.set_document_tags(ordered[-1], (tag.id,))
        tagged = store.search(SearchQuery(recent_only=True, tag_ids=(tag.id,)))
        assert [hit.document.id for hit in tagged.items] == [ordered[-1], ordered[0]]
    finally:
        store.close()


def test_stale_generation_disabled_and_missing_do_not_revive_old_body(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    try:
        library = store.add_library(root)
        old = register(store, library, "old-token.txt", "cached body unique")
        newer = store.invalidate_document(old.id)
        assert not store.set_index(old.id, old.content_generation,
                                   ReadResult("old cached body", "utf-8",
                                              size_bytes=old.size_bytes, modified_at=old.modified_at))
        result = store.search(SearchQuery(text="cached body", mode="content"))
        assert result.total == 0 and result.uncovered == 1
        assert store.search(SearchQuery(text="old-token", mode="name")).total == 1
        assert store.set_index(newer.id, newer.content_generation,
                               ReadResult("cached body unique", "utf-8",
                                          size_bytes=newer.size_bytes, modified_at=newer.modified_at))
        store.set_library_enabled(library.id, False)
        for include in (False, True):
            assert store.search(SearchQuery(include_unavailable=include)).total == 0
        store.set_library_enabled(library.id, True)
        assert store.search(SearchQuery(text="cached body", mode="content")).total == 0
        store.apply_scan(library.id, (), complete=True)
        assert store.search(SearchQuery(text="old-token", mode="name")).total == 0
        assert store.search(SearchQuery(text="old-token", mode="name", include_unavailable=True)).total == 1
        assert store.search(SearchQuery(text="cached body", mode="content", include_unavailable=True)).total == 0
    finally:
        store.close()


def test_normalized_name_search_does_not_merge_distinct_path_identities(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    try:
        library = store.add_library(root)
        composed = register(store, library, "café.txt", "first distinct body")
        decomposed = register(store, library, "cafe\u0301.txt", "second distinct body")
        assert composed.id != decomposed.id
        tag = store.create_tag("first only")
        store.set_document_tags(composed.id, (tag.id,))
        result = store.search(SearchQuery(text="CAFÉ", mode="name"))
        assert result.total == 2
        assert {hit.document.id for hit in result.items} == {composed.id, decomposed.id}
        tagged = store.search(SearchQuery(text="cafe\u0301", mode="name", tag_ids=(tag.id,)))
        assert [hit.document.id for hit in tagged.items] == [composed.id]
        assert store.get_document(decomposed.id).tags == ()
    finally:
        store.close()


def test_historical_cache_can_be_upgraded_without_losing_metadata(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    document = register(store, library, "managed.txt", "historical indexed text")
    tag = store.create_tag("must survive")
    store.set_document_tags(document.id, (tag.id,))
    store.set_favorite(document.id, True)
    store.close()
    # Build the shipped historical cache format, independently of today's schema.
    # All paths are this test's own temporary SQLite databases, never document files.
    store.search_path.unlink()
    with sqlite3.connect(store.search_path) as connection:
        connection.executescript("""
            CREATE TABLE indexed_documents (
                row_id INTEGER PRIMARY KEY, document_id TEXT UNIQUE NOT NULL,
                generation INTEGER NOT NULL, body TEXT NOT NULL, encoding TEXT NOT NULL);
            CREATE VIRTUAL TABLE document_fts USING fts5(body,tokenize='trigram');
            CREATE TABLE index_info (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO indexed_documents VALUES (1,?,?,?,'utf-8')",
                           (document.id, document.content_generation, "historical indexed text"))
        connection.execute("INSERT INTO document_fts(rowid,body) VALUES (1,?)",
                           ("historical indexed text",))
        connection.execute("INSERT INTO index_info VALUES ('normalization',?)",
                           ("nfc-casefold-nfc-v1:" + unicodedata.unidata_version,))
    reopened = Store(store.data_dir)
    try:
        current = reopened.get_document(document.id)
        assert current.tags == (tag,)
        assert current.is_favorite
        assert reopened.fts_available
        # A migration may preserve or safely invalidate the rebuildable cache.
        result = reopened.search(SearchQuery(text="historical", mode="content"))
        assert result.complete
        if result.total == 0:
            assert result.uncovered == 1 and current.index_status != "ready"
        else:
            assert result.total == 1 and result.covered == 1
        assert reopened.set_index(current.id, current.content_generation,
                                  ReadResult("replacement indexed txt", "utf-8",
                                             size_bytes=current.size_bytes, modified_at=current.modified_at))
        assert reopened.search(SearchQuery(text="replacement", mode="content")).total == 1
        assert reopened.search(SearchQuery(text="historical", mode="content")).total == 0
        assert reopened.get_document(document.id).tags == (tag,)
    finally:
        reopened.close()


def test_corrupt_optional_cache_preserves_name_search_and_reports_uncovered(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    document = register(store, library, "important-name.txt", "important body")
    tag = store.create_tag("retained")
    store.set_document_tags(document.id, (tag.id,))
    store.close()
    store.search_path.write_bytes(b"deliberately invalid SQLite cache")
    reopened = Store(store.data_dir)
    try:
        names = reopened.search(SearchQuery(text="important-name", mode="name", tag_ids=(tag.id,)))
        assert names.total == 1
        assert names.items[0].document.id == document.id
        assert names.covered == 0 and names.uncovered == 1
        assert names.warning
        bodies = reopened.search(SearchQuery(text="important body", mode="content"))
        assert bodies.total == 0
        assert bodies.uncovered == 1 and bodies.warning
        assert reopened.get_document(document.id).tags == (tag,)
    finally:
        reopened.close()
