"""Storage-only identity/observed-move regressions using private temporary files."""
from dataclasses import replace
import json
import os
import sqlite3
import uuid

import pytest

from notes.domain.models import NotesError, ReadResult, SearchQuery
from notes.infrastructure import files
from notes.infrastructure.store import Store


@pytest.fixture
def move_store(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "old.txt").write_text("content42", encoding="utf-8")
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    entry = files.get_file_entry(root, "old.txt")
    document = store.apply_scan(library.id, (entry,), complete=True)[0]
    store.set_index(document.id, document.content_generation, ReadResult(
        "content42", "utf-8", size_bytes=entry.size_bytes, modified_at=entry.modified_at))
    tag = store.create_tag("retain")
    store.set_document_tags(document.id, (tag.id,))
    store.set_favorite(document.id, True)
    store.mark_opened(document.id)
    store.set_encoding(document.id, "utf-8")
    return store, library, root, store.get_document(document.id), entry


def identity(store, document_id):
    with store._connect() as connection:
        return store._get_file_identity(connection, document_id)


def move(source, target, root):
    assert source.resolve().is_relative_to(root.resolve()) and source.is_file()
    assert target.resolve().is_relative_to(root.resolve()) and not target.exists()
    source.rename(target)


def require_identity(entry):
    if not entry.file_identity:
        pytest.skip("Stable single-link Windows NTFS identity unavailable")


def test_optional_identity_table_keeps_schema1_and_legacy_document_insert_compatible(move_store):
    store, library, _, document, _ = move_store
    with store._connect() as connection, store._transaction(connection):
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert len(connection.execute("PRAGMA table_info(documents)").fetchall()) == 14
        # The old executable's positional INSERT remains valid.
        other_id = str(uuid.uuid4())
        connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (other_id, library.id, "legacy.txt", "legacy.txt", "legacy.txt",
                            0, 0, "missing", 1, "pending", 1, 0, None, None))
        store._set_file_identity(connection, other_id, "derived-only")
        connection.execute("DELETE FROM documents WHERE id=?", (other_id,))
        assert store._get_file_identity(connection, other_id) == ""
    assert store.get_document(document.id).tags == document.tags


def test_upgrade_without_derived_table_preserves_documents_and_annotations(move_store):
    store, _, _, document, _ = move_store
    with store._connect() as connection:
        connection.execute("DROP TABLE document_file_identities")
    reopened = Store(store.data_dir)
    assert reopened.get_document(document.id) == document
    assert identity(reopened, document.id) == ""


def test_missing_preserves_identity_but_unknown_scan_clears_it(move_store):
    store, library, _, document, entry = move_store
    supplied = replace(entry, file_identity="derived-proof")
    store.apply_scan(library.id, (supplied,), complete=False)
    store.apply_scan(library.id, (), complete=True)
    assert identity(store, document.id) == "derived-proof"
    assert store.get_document(document.id).availability == "missing"
    store.apply_scan(library.id, (replace(entry, file_identity=""),), complete=True)
    assert identity(store, document.id) == ""
    assert store.get_document(document.id).tags == document.tags


def test_derived_identity_is_not_management_dirty_or_json_trusted(move_store, tmp_path):
    store, library, _, document, entry = move_store
    before = store._metadata_fingerprint()
    proof = "win-ntfs-v1:aaaa:bbbb:cccc"
    store.apply_scan(library.id, (replace(entry, file_identity=proof),), complete=True)
    assert store._metadata_fingerprint() == before
    target = tmp_path / "backup.json"
    store.export_backup(target)
    text = target.read_text(encoding="utf-8")
    assert proof not in text and "document_file_identities" not in text
    payload = json.loads(text)
    payload["document_file_identities"] = [{"document_id": document.id, "identity": "untrusted"}]
    payload["documents"][0]["file_identity"] = "untrusted"
    target.write_text(json.dumps(payload), encoding="utf-8")
    store.restore_backup(target)
    assert identity(store, document.id) == ""
    assert store.get_document(document.id).tags == document.tags


def test_disable_and_scope_change_clear_identity_without_erasing_annotations(move_store):
    store, library, _, document, entry = move_store
    entry = replace(entry, file_identity="known")
    store.apply_scan(library.id, (entry,), complete=True)
    store.set_library_enabled(library.id, False)
    assert identity(store, document.id) == ""
    store.apply_scan(library.id, (entry,), complete=True)
    assert identity(store, document.id) == ""
    store.set_library_enabled(library.id, True)
    store.apply_scan(library.id, (entry,), complete=True)
    assert identity(store, document.id) == "known"
    store.update_library_rules(library.id, (".md",), ())
    assert identity(store, document.id) == ""
    assert store.get_document(document.id).tags == document.tags


def test_same_path_atomic_replacement_updates_identity_and_invalidates_same_stat_cache(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    path = root / "old.txt"
    old_stat = path.stat()
    replacement = root / "replacement.txt"
    replacement.write_text("CONTENT42", encoding="utf-8")
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert replacement.resolve().is_relative_to(root.resolve())
    assert path.resolve().is_relative_to(root.resolve())
    os.replace(replacement, path)
    updated_entry = files.get_file_entry(root, "old.txt")
    assert updated_entry.file_identity != entry.file_identity
    assert (updated_entry.size_bytes, updated_entry.modified_at) == (entry.size_bytes, entry.modified_at)
    before = store.get_document(document.id)
    updated = store.apply_scan(library.id, (updated_entry,), complete=True)[0]
    assert updated.id == document.id and updated.tags == document.tags
    assert updated.content_generation == before.content_generation + 1 and updated.index_status == "pending"
    assert identity(store, document.id) == updated_entry.file_identity


def test_real_move_after_missing_scan_keeps_id_every_annotation_and_bytes(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    source, target = root / "old.txt", root / "new.txt"
    content = source.read_bytes()
    move(source, target, root)
    store.apply_scan(library.id, (), complete=True)
    missing = store.get_document(document.id)
    target_entry = files.get_file_entry(root, "new.txt")
    assert target_entry.file_identity == entry.file_identity
    relocated = store.apply_observed_move(library.id, "old.txt", "new.txt", target_entry)
    assert relocated is not None and relocated.id == document.id and relocated.relative_path == "new.txt"
    assert relocated.tags == document.tags and relocated.is_favorite
    assert relocated.encoding_override == document.encoding_override
    assert relocated.last_open_requested_at == document.last_open_requested_at
    assert relocated.content_generation == missing.content_generation + 1
    assert relocated.index_status == "pending" and target.read_bytes() == content
    assert identity(store, document.id) == target_entry.file_identity
    assert store.search(SearchQuery(text="content42", mode="content")).total == 0
    with store._connect(store.search_path) as connection:
        assert connection.execute("SELECT count(*) FROM indexed_documents").fetchone()[0] == 0


def test_matching_unannotated_scan_placeholder_absorbed_and_foreign_key_identity_removed(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    target_entry = files.get_file_entry(root, "new.txt")
    records = store.apply_scan(library.id, (target_entry,), complete=True)
    placeholder = next(item for item in records if item.id != document.id)
    assert identity(store, placeholder.id) == entry.file_identity
    result = store.apply_observed_move(library.id, "old.txt", "new.txt", target_entry)
    assert result is not None and result.id == document.id and len(store.list_documents()) == 1
    assert identity(store, placeholder.id) == ""
    assert result.tags == document.tags


@pytest.mark.parametrize("target_identity", ["", "different-derived-identity"])
def test_placeholder_without_same_identity_is_not_absorbed(move_store, target_identity):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    actual = files.get_file_entry(root, "new.txt")
    store.apply_scan(library.id, (replace(actual, file_identity=target_identity),), complete=True)
    assert store.apply_observed_move(library.id, "old.txt", "new.txt", actual) is None
    assert store.get_document(document.id).relative_path == "old.txt"
    assert len(store.list_documents()) == 2


@pytest.mark.parametrize("annotation", ["tag", "favorite", "recent", "encoding"])
def test_any_target_annotation_blocks_automatic_absorption(move_store, annotation):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    actual = files.get_file_entry(root, "new.txt")
    records = store.apply_scan(library.id, (actual,), complete=True)
    target = next(item for item in records if item.id != document.id)
    if annotation == "tag":
        store.set_document_tags(target.id, (store.create_tag("target").id,))
    elif annotation == "favorite":
        store.set_favorite(target.id, True)
    elif annotation == "recent":
        store.mark_opened(target.id)
    else:
        store.set_encoding(target.id, "utf-8")
    before = store.list_documents()
    assert store.apply_observed_move(library.id, "old.txt", "new.txt", actual) is None
    assert store.list_documents() == before


def test_forged_hint_identity_cannot_replace_real_target_identity(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    (root / "old.txt").unlink()
    (root / "new.txt").write_text("content42", encoding="utf-8")
    actual = files.get_file_entry(root, "new.txt")
    assert actual.file_identity != entry.file_identity
    forged = replace(actual, file_identity=entry.file_identity)
    before = store.get_document(document.id)
    assert store.apply_observed_move(library.id, "old.txt", "new.txt", forged) is None
    assert store.get_document(document.id) == before


def test_empty_first_scan_identity_never_authorizes_automatic_move(move_store):
    store, library, root, document, entry = move_store
    store.apply_scan(library.id, (replace(entry, file_identity=""),), complete=True)
    move(root / "old.txt", root / "new.txt", root)
    target = files.get_file_entry(root, "new.txt")
    assert store.apply_observed_move(library.id, "old.txt", "new.txt", target) is None
    assert store.get_document(document.id).relative_path == "old.txt"


@pytest.mark.parametrize("mutation", ["second_identity", "reappearing_source", "permission_failure"])
def test_transaction_revalidation_rejects_races_and_permission_failures(move_store, monkeypatch, mutation):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    actual = files.get_file_entry(root, "new.txt")
    real_entry = files.get_file_entry
    calls = 0
    def changing(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_entry(*args, **kwargs)
        if mutation == "permission_failure":
            raise NotesError("unavailable", "simulated access denial")
        if mutation == "reappearing_source" and calls == 1:
            (root / "old.txt").write_text("new unrelated object", encoding="utf-8")
        if mutation == "second_identity" and calls == 2:
            return replace(result, file_identity="different object")
        return result
    monkeypatch.setattr(files, "get_file_entry", changing)
    before = store.get_document(document.id)
    assert store.apply_observed_move(library.id, "old.txt", "new.txt", actual) is None
    assert store.get_document(document.id) == before


def test_manual_relocation_updates_identity_for_next_observed_move(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    (root / "old.txt").unlink()
    (root / "manual.txt").write_text("replacement", encoding="utf-8")
    manual = store.relocate_document(document.id, "manual.txt")
    actual = files.get_file_entry(root, "manual.txt")
    assert actual.file_identity != entry.file_identity
    assert identity(store, document.id) == actual.file_identity
    move(root / "manual.txt", root / "later.txt", root)
    result = store.apply_observed_move(library.id, manual.relative_path, "later.txt",
                                       files.get_file_entry(root, "later.txt"))
    assert result is not None and result.id == document.id and result.tags == document.tags


def test_cache_cleanup_failure_cannot_revive_old_generation(move_store, monkeypatch):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    actual = files.get_file_entry(root, "new.txt")
    original = store._new_connection
    def fail_cache(path=None):
        if path == store.search_path:
            raise sqlite3.DatabaseError("simulated cache damage")
        return original(path)
    monkeypatch.setattr(store, "_new_connection", fail_cache)
    result = store.apply_observed_move(library.id, "old.txt", "new.txt", actual)
    assert result is not None and result.index_status == "pending"
    assert not store.fts_available
    assert store.search(SearchQuery(text="content42", mode="content")).total == 0
    assert result.tags == document.tags


def test_metadata_failure_rolls_back_placeholder_absorption_and_derived_identity(move_store):
    store, library, root, document, entry = move_store
    require_identity(entry)
    move(root / "old.txt", root / "new.txt", root)
    actual = files.get_file_entry(root, "new.txt")
    records = store.apply_scan(library.id, (actual,), complete=True)
    target = next(item for item in records if item.id != document.id)
    before = store.list_documents()
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER test_abort_move BEFORE UPDATE OF relative_path ON documents
            WHEN new.relative_path='new.txt' BEGIN SELECT RAISE(ABORT,'simulated metadata failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_observed_move(library.id, "old.txt", "new.txt", actual)
    assert store.list_documents() == before
    assert identity(store, document.id) == entry.file_identity
    assert identity(store, target.id) == entry.file_identity
    assert (root / "new.txt").read_text(encoding="utf-8") == "content42"
