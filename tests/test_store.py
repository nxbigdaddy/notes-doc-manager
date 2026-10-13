import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading

import pytest

from notes.domain.models import FileEntry, NotesError, ReadResult, SearchQuery
from notes.infrastructure.store import Store


@pytest.fixture
def setup_store(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    return store, library, root


def add_document(store, library, name="note.txt", text="中文服务器 hello Straße", modified=100):
    entry = FileEntry(name, len(text.encode()), modified)
    store.apply_scan(library.id, (entry,), complete=False)
    document = next(d for d in store.list_documents() if d.relative_path == name)
    assert store.set_index(document.id, document.content_generation,
                           ReadResult(text, "utf-8", size_bytes=entry.size_bytes, modified_at=modified))
    return store.get_document(document.id)


def test_scan_search_and_generation(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    for word in ("中文", "服务器", "STRASSE", "中", "hello"):
        result = store.search(SearchQuery(text=word))
        assert result.total == result.covered == 1
        assert result.uncovered == 0
    invalid = store.invalidate_document(doc.id)
    assert invalid.content_generation == doc.content_generation + 1
    assert store.search(SearchQuery(text="服务器")).total == 0
    assert store.search(SearchQuery(text="服务器")).uncovered == 1
    assert not store.set_index(doc.id, doc.content_generation, ReadResult("stale"))


@pytest.mark.parametrize("needle", ['"', "*", "%", "_", "a_b", 'a"b', "OR", "(abc)", "x%y", "'; DROP TABLE tags;--"])
def test_literal_queries(setup_store, needle):
    store, library, _ = setup_store
    add_document(store, library, text=f"before {needle} after")
    assert store.search(SearchQuery(text=needle, mode="content")).total == 1
    assert store.list_tags() == ()


def test_unicode_normalization(setup_store):
    store, library, _ = setup_store
    add_document(store, library, "café.txt", "e\u0301 STRASSE")
    assert store.search(SearchQuery(text="é")).total == 1
    assert store.search(SearchQuery(text="cafe\u0301", mode="name")).total == 1
    store.create_tag("Straße")
    with pytest.raises(NotesError):
        store.create_tag("STRASSE")
    store.create_tag("e\u0301")
    with pytest.raises(NotesError):
        store.create_tag("é")


def test_scan_missing_offline_and_rules_preserve_metadata(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    tag = store.create_tag("工作")
    store.set_document_tags(doc.id, (tag.id,))
    store.set_favorite(doc.id, True)
    store.apply_scan(library.id, (), complete=False)
    assert store.get_document(doc.id).availability == "present"
    store.apply_scan(library.id, (), complete=True, availability="offline")
    assert store.get_document(doc.id).availability == "present"
    assert store.search(SearchQuery()).total == 0
    assert store.search(SearchQuery(include_unavailable=True)).total == 1
    store.apply_scan(library.id, (), complete=True)
    assert store.get_document(doc.id).availability == "missing"
    assert store.get_document(doc.id).tags == (tag,)
    store.apply_scan(library.id, (FileEntry(doc.relative_path, doc.size_bytes, doc.modified_at),), True)
    store.update_library_rules(library.id, (".md",), ())
    assert not store.get_document(doc.id).in_scope
    assert store.get_document(doc.id).is_favorite
    assert store.get_document(doc.id).tags == (tag,)
    store.update_library_rules(library.id, (".txt",), ())
    assert store.get_document(doc.id).in_scope


def test_search_filters_and_page(setup_store):
    store, library, _ = setup_store
    first = add_document(store, library, "folder/a.txt", "common", 100)
    second = add_document(store, library, "b.txt", "common", 200)
    add_document(store, library, "c.txt", "unrelated", 300)
    store.set_favorite(first.id, True)
    tag = store.create_tag("tag")
    store.set_document_tags(second.id, (tag.id,))
    store.mark_opened(second.id)
    for query in (SearchQuery(favorites_only=True), SearchQuery(folder="folder"),
                  SearchQuery(tag_ids=(tag.id,)), SearchQuery(recent_only=True)):
        result = store.search(query)
        assert result.total == result.covered == 1
    result = store.search(SearchQuery(text="common", page_size=1))
    assert result.total == 2
    assert result.items[0].document.id == second.id
    assert store.search(SearchQuery(text="common", page_size=1, page=1)).items[0].document.id == first.id


def test_backup_roundtrip_no_bodies_or_editor_activation(setup_store, tmp_path):
    store, library, root = setup_store
    source = root / "note.txt"
    source.write_text("real original content", encoding="utf-8")
    doc = add_document(store, library)
    tag = store.create_tag("重要")
    store.set_document_tags(doc.id, (tag.id,))
    store.set_favorite(doc.id, True)
    store.set_encoding(doc.id, "gb18030")
    store.set_setting("editor_path", "untrusted.exe")
    target = tmp_path / "backup.json"
    store.export_backup(target)
    assert "real original content" not in target.read_text(encoding="utf-8")
    assert "untrusted.exe" not in target.read_text(encoding="utf-8")
    assert store.inspect_backup(target)["documents"] == 1
    store.delete_tag(tag.id)
    store.restore_backup(target)
    restored = store.get_document(doc.id)
    assert restored.is_favorite and restored.tags == (tag,)
    assert restored.encoding_override == "gb18030"
    assert restored.index_status == "pending"
    assert store.get_setting("editor_path") == ""
    assert source.read_text(encoding="utf-8") == "real original content"
    backups = list((store.data_dir / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("mutation", ["path_escape", "duplicate_id", "missing_tag", "version", "overlap", "encoding"])
def test_reject_malicious_backups_without_mutation(setup_store, tmp_path, mutation):
    store, library, _ = setup_store
    doc = add_document(store, library)
    target = tmp_path / "backup.json"
    store.export_backup(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if mutation == "path_escape":
        payload["documents"][0]["relative_path"] = "../outside.txt"
    elif mutation == "duplicate_id":
        payload["documents"].append(payload["documents"][0])
    elif mutation == "missing_tag":
        payload["document_tags"].append({"document_id": doc.id, "tag_id": "absent"})
    elif mutation == "version":
        payload["format_version"] = 9
    elif mutation == "overlap":
        row = dict(payload["libraries"][0])
        row["id"] = "3e52a839-b720-404f-a379-86deecb13bfe"
        row["root_path"] = str(Path(row["root_path"]) / "nested")
        payload["libraries"].append(row)
    elif mutation == "encoding":
        payload["documents"][0]["encoding_override"] = "exec"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NotesError):
        store.restore_backup(target)
    assert store.get_document(doc.id) == doc
    assert store.search(SearchQuery(text="中文")).total == 1


def test_rebuild_preserves_management_and_reconcile_missing_cache(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    tag = store.create_tag("tag")
    store.set_document_tags(doc.id, (tag.id,))
    store.rebuild_index()
    assert store.get_document(doc.id).tags == (tag,)
    assert store.get_document(doc.id).index_status == "pending"
    assert store.search(SearchQuery(text="中文")).total == 0
    add_document(store, library)
    # Simulate a cache lost between app runs.
    store.search_path.unlink()
    reopened = Store(store.data_dir)
    assert reopened.get_document(doc.id).index_status == "pending"
    assert reopened.get_document(doc.id).tags == (tag,)


def test_cross_thread_operations_and_cancel(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.set_setting(f"key{n}", str(n)), range(20)))
        results = list(pool.map(lambda _: store.search(SearchQuery(text="hello")), range(20)))
    assert all(r.total == 1 for r in results)
    event = threading.Event()
    event.set()
    assert not store.search(SearchQuery(text="hello"), cancel=event).complete
    store.set_document_tags(doc.id, ())


def test_overlap_and_literal_path_boundaries(setup_store):
    store, library, root = setup_store
    child = root / "child"
    child.mkdir()
    with pytest.raises(NotesError):
        store.add_library(child)
    with pytest.raises(NotesError):
        store.add_library(root.parent)
    for name in ("../x.txt", "C:/x.txt", "/x.txt", "x.txt:stream", "a./x.txt",
                 "CON.txt", "x/aux.log", "a?.txt", "a//b.txt"):
        with pytest.raises(NotesError):
            store.apply_scan(library.id, (FileEntry(name, 0, 0),), False)
    adjacent = root.with_name(root.name + "2")
    adjacent.mkdir()
    assert store.add_library(adjacent)


def test_failed_tag_change_rolls_back(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    tag = store.create_tag("tag")
    store.set_document_tags(doc.id, (tag.id,))
    with pytest.raises(NotesError):
        store.set_document_tags(doc.id, ("invalid",))
    assert store.get_document(doc.id).tags == (tag,)


def test_stale_read_rejected_and_failed_decode_invalidates(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    assert not store.set_index(doc.id, doc.content_generation,
                               ReadResult("wrong", size_bytes=999, modified_at=999))
    assert store.set_index(doc.id, doc.content_generation, ReadResult(status="needs_encoding"))
    assert store.search(SearchQuery(text="hello")).total == 0
    assert store.get_document(doc.id).index_status == "needs_encoding"


def test_disabled_library_clears_cache_preserves_tags(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    tag = store.create_tag("keep")
    store.set_document_tags(doc.id, (tag.id,))
    store.set_library_enabled(library.id, False)
    assert store.get_document(doc.id).tags == (tag,)
    assert store.search(SearchQuery(include_unavailable=True)).total == 0
    with sqlite3.connect(store.search_path) as conn:
        assert conn.execute("SELECT count(*) FROM indexed_documents").fetchone()[0] == 0
    store.set_library_enabled(library.id, True)
    assert store.get_document(doc.id).index_status == "pending"


def test_corrupt_cache_degrades_and_rebuilds(setup_store):
    store, library, _ = setup_store
    doc = add_document(store, library)
    store.search_path.write_bytes(b"broken index")
    reopened = Store(store.data_dir)
    assert not reopened.fts_available
    result = reopened.search(SearchQuery(text="note", mode="name"))
    assert result.total == 1 and result.warning
    reopened.rebuild_index()
    assert reopened.fts_available
    assert reopened.get_document(doc.id).index_status == "pending"


def test_export_never_overwrites_target(setup_store, tmp_path):
    store, _, _ = setup_store
    target = tmp_path / "existing.json"
    target.write_text("important", encoding="utf-8")
    with pytest.raises(NotesError):
        store.export_backup(target)
    assert target.read_text(encoding="utf-8") == "important"


def test_index_batch_generations_and_invalidations(setup_store):
    store, library, _ = setup_store
    first = add_document(store, library, "first.txt")
    second = add_document(store, library, "second.txt")
    invalidated = store.invalidate_documents((first.id, second.id))
    assert len(invalidated) == 2
    read = ReadResult("replacement", "utf-8", size_bytes=first.size_bytes, modified_at=first.modified_at)
    assert store.set_index_batch((
        (first.id, first.content_generation, read),
        (second.id, invalidated[1].content_generation, read),
    )) == (False, True)
    assert store.search(SearchQuery(text="replacement")).total == 1
    assert store.search(SearchQuery(text="服务器")).total == 0
    assert store.get_document(first.id).index_status == "pending"
    assert store.get_document(second.id).index_status == "ready"


def test_index_batch_rejects_duplicate_and_missing_before_mutation(setup_store):
    store, library, _ = setup_store
    document = add_document(store, library)
    read = ReadResult(status="needs_encoding")
    item = (document.id, document.content_generation, read)
    with pytest.raises(NotesError):
        store.set_index_batch((item, item))
    with pytest.raises(NotesError):
        store.set_index_batch((item, ("missing", 1, read)))
    assert store.get_document(document.id).index_status == "ready"
    assert store.search(SearchQuery(text="服务器")).total == 1
    with pytest.raises(NotesError):
        store.invalidate_documents((document.id, "missing"))
    assert store.get_document(document.id).content_generation == document.content_generation


def test_backup_size_and_duplicate_json_keys_rejected(setup_store, tmp_path, monkeypatch):
    import notes.infrastructure.store as module
    store, _, _ = setup_store
    target = tmp_path / "malformed.json"
    target.write_text('{"format_version": 1, "format_version": 1}', encoding="utf-8")
    with pytest.raises(NotesError):
        store.inspect_backup(target)
    target.write_text('{"format_version": 1, "extra": NaN}', encoding="utf-8")
    with pytest.raises(NotesError):
        store.inspect_backup(target)
    monkeypatch.setattr(module, "MAX_BACKUP_BYTES", 32)
    target.write_bytes(b"x" * 33)
    with pytest.raises(NotesError, match="100 MiB"):
        store.inspect_backup(target)


@pytest.mark.parametrize("needle", ["😀🙂🚀", "一😀二", "x\x00y", "\u0301", '"""', "***", "a  b",
                                   "e\u0301ß", "\nhello", "AND", "NEAR(abc)", "[abc]", "^start"])
def test_additional_literal_unicode_edges(setup_store, needle):
    store, library, _ = setup_store
    add_document(store, library, text="prefix\n" + needle + "\nsuffix")
    assert store.search(SearchQuery(text=needle, mode="content")).total == 1
