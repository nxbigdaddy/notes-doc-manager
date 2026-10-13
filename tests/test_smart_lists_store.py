"""Real isolated SQLite tests for portable, fail-closed saved search filters."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from notes.domain.models import FileEntry, NotesError, ReadResult, SearchQuery
from notes.domain.smart_lists import encode_query
from notes.infrastructure.store import SMART_LIST_SCHEMA, Store, normalize_text


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "documents"
    (root / "folder").mkdir(parents=True)
    bodies = {"first.txt": "alpha shared", "folder/note.txt": "literal %_* shared", "other.txt": "beta shared"}
    entries = []
    for relative, text in bodies.items():
        path = root / relative
        path.write_text(text, encoding="utf-8")
        info = path.stat()
        entries.append(FileEntry(relative, info.st_size, info.st_mtime_ns // 1_000_000))
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    documents = store.apply_scan(library.id, tuple(entries), True)
    for document in documents:
        store.set_index(document.id, document.content_generation, ReadResult(
            bodies[document.relative_path], "utf-8", size_bytes=document.size_bytes,
            modified_at=document.modified_at))
    documents = {document.relative_path: document for document in store.list_documents()}
    tag_a, tag_b = store.create_tag("A"), store.create_tag("B")
    store.set_document_tags(documents["first.txt"].id, (tag_a.id,))
    store.set_document_tags(documents["folder/note.txt"].id, (tag_a.id, tag_b.id))
    store.set_document_tags(documents["other.txt"].id, (tag_b.id,))
    store.set_favorite(documents["folder/note.txt"].id, True)
    return store, library, root, documents, tag_a, tag_b


def metadata_state(store):
    with store._connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute("SELECT 1 FROM sqlite_schema WHERE name='smart_lists'").fetchone() is not None
        return version, table


def restore_ready(store, library, root):
    entries = []
    for path in root.rglob("*.txt"):
        info = path.stat()
        entries.append(FileEntry(path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns // 1_000_000))
    documents = store.apply_scan(library.id, tuple(entries), True)
    for document in documents:
        store.set_index(document.id, document.content_generation, ReadResult(
            (root / document.relative_path).read_text(encoding="utf-8"), "utf-8",
            size_bytes=document.size_bytes, modified_at=document.modified_at))


def test_unused_feature_and_schema0_upgrade_remain_schema1_without_table(managed):
    store, _, _, _, _, _ = managed
    assert metadata_state(store) == (1, False)
    assert store.list_smart_lists() == ()
    assert metadata_state(store) == (1, False)
    with store._connect() as connection:
        connection.execute("PRAGMA user_version=0")
    reopened = Store(store.data_dir)
    assert metadata_state(reopened) == (1, False)
    assert len(reopened.list_documents()) == 3


def test_first_save_is_protected_atomic_upgrade_and_last_delete_never_downgrades(managed):
    store, library, _, documents, tag_a, _ = managed
    result = store.create_smart_list("  工作  ", {"library_id": library.id, "tag_ids": [tag_a.id]})
    assert result.name == "工作" and result.available
    assert metadata_state(store) == (2, True)
    protections = list((store.data_dir / "backups").glob("metadata-*.db"))
    assert len(protections) == 1
    with closing(sqlite3.connect(protections[0])) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name='smart_lists'").fetchone() is None
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == len(documents)
    store.create_smart_list("第二个", {})
    assert len(list((store.data_dir / "backups").glob("metadata-*.db"))) == 1
    for item in store.list_smart_lists():
        store.delete_smart_list(item.id)
    assert metadata_state(store) == (2, True) and store.list_smart_lists() == ()
    assert len(store.list_documents()) == len(documents)
    assert len(store.list_tags()) == 2


def test_transaction_fault_rolls_back_table_insert_version_but_keeps_protection(managed, monkeypatch):
    store, _, _, documents, _, _ = managed
    old = store._transaction
    @contextmanager
    def fail_before_commit(connection):
        with old(connection):
            yield
            if connection.execute("PRAGMA user_version").fetchone()[0] == 2:
                raise sqlite3.OperationalError("injected failure before commit")
    monkeypatch.setattr(store, "_transaction", fail_before_commit)
    with pytest.raises(sqlite3.OperationalError):
        store.create_smart_list("rollback", {})
    assert metadata_state(store) == (1, False)
    assert len(store.list_documents()) == len(documents)
    assert len(list((store.data_dir / "backups").glob("metadata-*.db"))) == 1
    monkeypatch.setattr(store, "_transaction", old)
    assert store.create_smart_list("after retry", {}).available


def test_failed_protective_snapshot_never_starts_upgrade(managed, monkeypatch):
    store, _, _, _, _, _ = managed
    def fail():
        raise OSError("snapshot unavailable")
    monkeypatch.setattr(store, "backup_metadata", fail)
    with pytest.raises(OSError):
        store.create_smart_list("unchanged", {})
    assert metadata_state(store) == (1, False)


@pytest.mark.parametrize("name,query", [
    ("", {}), ("bad", {"sql": "SELECT * FROM documents"}), ("bad", {"page": 1}),
    ("bad", {"library_id": "not UUID"}), ("bad", {"tag_ids": [str(uuid.uuid4())]}),
    ("bad", {"library_id": str(uuid.uuid4())}), ("bad", {"folder": "folder"}),
])
def test_invalid_saves_leave_schema1_and_no_protection_snapshot(managed, name, query):
    store, _, _, _, _, _ = managed
    with pytest.raises(NotesError):
        store.create_smart_list(name, query)
    assert metadata_state(store) == (1, False)
    assert not (store.data_dir / "backups").exists()


def test_unicode_duplicate_names_and_failed_update_preserve_existing(managed):
    store, _, _, _, _, _ = managed
    first = store.create_smart_list("Straße", {"text": "alpha"})
    second = store.create_smart_list("Cafe\u0301", {"text": "beta"})
    with pytest.raises(NotesError) as failure:
        store.create_smart_list("STRASSE", {})
    assert failure.value.code == "duplicate_smart_list"
    with pytest.raises(NotesError):
        store.create_smart_list("Café", {})
    with pytest.raises(NotesError):
        store.update_smart_list(second.id, "STRASSE", {})
    assert store.list_smart_lists() == tuple(sorted((first, second), key=lambda item: normalize_text(item.name)))
    updated = store.update_smart_list(first.id, "updated", {"text": "shared", "mode": "content"})
    assert updated.id == first.id and store.search_smart_list(first.id).total == 3


def test_list_limit_uses_real_sqlite_and_deletion_does_not_touch_documents(managed):
    store, _, _, _, _, _ = managed
    first = store.create_smart_list("item0", {})
    with store._connect() as connection, store._transaction(connection):
        connection.executemany("INSERT INTO smart_lists VALUES (?,?,?,?)",
                               [(str(uuid.uuid4()), f"item{i}", f"item{i}", encode_query({})) for i in range(1, 100)])
    assert len(store.list_smart_lists()) == 100
    with pytest.raises(NotesError) as error:
        store.create_smart_list("overflow", {})
    assert error.value.code == "smart_list_limit"
    store.delete_smart_list(first.id)
    assert store.create_smart_list("replacement", {}).available
    assert len(store.list_documents()) == 3


def test_saved_and_filters_literal_search_scope_and_pagination(managed):
    store, library, _, documents, tag_a, tag_b = managed
    saved = store.create_smart_list("scoped", {
        "library_id": library.id, "folder": "folder", "tag_ids": [tag_a.id, tag_b.id],
        "text": "%_*", "mode": "content", "favorites_only": True,
    })
    result = store.search_smart_list(saved.id, page_size=1)
    assert result.complete and result.total == result.covered == 1
    assert result.items[0].document.id == documents["folder/note.txt"].id
    assert store.search_smart_list(saved.id, page=1, page_size=1).items == ()
    with pytest.raises(NotesError):
        store.search_smart_list(saved.id, page=0.5)
    cancel = threading.Event()
    cancel.set()
    assert not store.search_smart_list(saved.id, cancel=cancel).complete


def test_deleted_tag_keeps_filter_but_blocks_current_query_even_with_unavailable(managed):
    store, _, _, _, tag_a, _ = managed
    saved = store.create_smart_list("must keep tag", {"tag_ids": [tag_a.id], "include_unavailable": True})
    assert store.search_smart_list(saved.id).total == 2
    store.delete_tag(tag_a.id)
    unavailable = store.list_smart_lists()[0]
    assert not unavailable.available and unavailable.warning
    assert unavailable.query.tag_ids == (tag_a.id,)
    with pytest.raises(NotesError) as error:
        store.search_smart_list(saved.id)
    assert error.value.code == "smart_list_unavailable"
    with pytest.raises(NotesError):
        store.update_smart_list(saved.id, "still invalid", {"tag_ids": [tag_a.id]})
    assert store.list_smart_lists()[0] == unavailable


def test_disabled_and_offline_library_policies_do_not_relax_saved_scope(managed):
    store, library, _, _, _, _ = managed
    scoped = store.create_smart_list("online only", {"library_id": library.id})
    offline = store.create_smart_list("metadata too", {"library_id": library.id, "include_unavailable": True})
    store.apply_scan(library.id, (), complete=False, availability="offline")
    availability = {item.id: item.available for item in store.list_smart_lists()}
    assert not availability[scoped.id] and availability[offline.id]
    with pytest.raises(NotesError):
        store.search_smart_list(scoped.id)
    assert store.search_smart_list(offline.id).total == 3
    store.set_library_enabled(library.id, False)
    assert not any(item.available for item in store.list_smart_lists())
    with pytest.raises(NotesError):
        store.search_smart_list(offline.id)
    disabled_saved = store.create_smart_list("disabled reference exists", {"library_id": library.id})
    assert not disabled_saved.available


def test_missing_folder_does_not_fall_back_to_root(managed):
    store, library, root, _, _, _ = managed
    visible = store.create_smart_list("folder", {"library_id": library.id, "folder": "folder"})
    historical = store.create_smart_list("folder history", {
        "library_id": library.id, "folder": "folder", "include_unavailable": True})
    path = root / "folder" / "note.txt"
    assert path.resolve().is_relative_to(root.resolve())
    path.unlink()
    path.parent.rmdir()
    entries = tuple(FileEntry(p.name, p.stat().st_size, p.stat().st_mtime_ns // 1_000_000)
                    for p in root.glob("*.txt"))
    store.apply_scan(library.id, entries, True)
    assert store.search_smart_list(visible.id).total == 0
    result = store.search_smart_list(historical.id)
    assert result.total == 1 and result.items[0].document.relative_path == "folder/note.txt"


def test_list_and_scope_checks_do_not_probe_library_filesystem(managed, monkeypatch):
    store, library, _, _, _, _ = managed
    saved = store.create_smart_list("metadata only", {"library_id": library.id, "folder": "missing"})
    def forbidden(*args, **kwargs):
        raise AssertionError("saved-filter listing must not probe document paths")
    for method in ("exists", "is_dir", "is_file", "is_symlink", "is_junction", "stat", "lstat", "resolve"):
        if hasattr(Path, method):
            monkeypatch.setattr(Path, method, forbidden)
    assert store.list_smart_lists()[0].id == saved.id
    assert store.search_smart_list(saved.id).total == 0


def test_query_rechecks_saved_conditions_instead_of_using_gui_snapshot(managed):
    store, _, _, _, _, _ = managed
    stale = store.create_smart_list("changing", {"text": "alpha", "mode": "content"})
    assert store.search_smart_list(stale.id).total == 1
    store.update_smart_list(stale.id, stale.name, {"text": "no matches", "mode": "content"})
    assert stale.query.text == "alpha"  # Simulate a retained GUI DTO.
    assert store.search_smart_list(stale.id).total == 0
    store.delete_smart_list(stale.id)
    with pytest.raises(NotesError):
        store.search_smart_list(stale.id)


def test_format1_stays_legacy_format2_roundtrip_preserves_ids_and_missing_refs(managed, tmp_path):
    store, library, root, documents, tag_a, _ = managed
    old = tmp_path / "old.json"
    store.export_backup(old)
    first = json.loads(old.read_text(encoding="utf-8"))
    assert first["format_version"] == 1 and "smart_lists" not in first
    saved = store.create_smart_list("portable", {"library_id": library.id, "tag_ids": [tag_a.id]})
    new = tmp_path / "new.json"
    store.export_backup(new)
    payload = json.loads(new.read_text(encoding="utf-8"))
    assert payload["format_version"] == 2 and payload["smart_lists"][0]["id"] == saved.id
    assert store.inspect_backup(new)["smart_lists"] == 1
    store.delete_smart_list(saved.id)
    store.restore_backup(new)
    assert metadata_state(store) == (2, True)
    restored = store.list_smart_lists()[0]
    assert restored.id == saved.id and restored.query.tag_ids == (tag_a.id,)
    restore_ready(store, library, root)
    assert store.search_smart_list(saved.id).total == 2
    assert {doc.id for doc in store.list_documents()} == {doc.id for doc in documents.values()}
    # v1 explicitly replaces management state; the table stays present and v2.
    store.restore_backup(old)
    assert metadata_state(store) == (2, True) and store.list_smart_lists() == ()
    empty = tmp_path / "empty-v2.json"
    store.export_backup(empty)
    assert json.loads(empty.read_text(encoding="utf-8"))["smart_lists"] == []


def test_format2_import_transactionally_upgrades_v1_and_preserves_invalid_references(managed, tmp_path):
    source, _, _, _, _, _ = managed
    source.create_smart_list("saved", {})
    backup = tmp_path / "portable.json"
    source.export_backup(backup)
    payload = json.loads(backup.read_text(encoding="utf-8"))
    lost_library, lost_tag = str(uuid.uuid4()), str(uuid.uuid4())
    payload["smart_lists"][0]["query_json"] = encode_query({
        "library_id": lost_library, "folder": "kept-folder", "tag_ids": [lost_tag],
        "include_unavailable": True,
    })
    backup.write_text(json.dumps(payload), encoding="utf-8")
    target = Store(tmp_path / "target-data")
    assert metadata_state(target) == (1, False)
    target.restore_backup(backup)
    assert metadata_state(target) == (2, True)
    restored = target.list_smart_lists()[0]
    assert not restored.available and restored.query.library_id == lost_library
    assert restored.query.folder == "kept-folder" and restored.query.tag_ids == (lost_tag,)
    with pytest.raises(NotesError):
        target.search_smart_list(restored.id)


def test_format2_restore_fault_rolls_back_ddl_version_and_replaced_management(managed, tmp_path, monkeypatch):
    source, _, _, _, _, _ = managed
    source.create_smart_list("source list", {})
    backup = tmp_path / "source-v2.json"
    source.export_backup(backup)
    target_root = tmp_path / "target-documents"
    target_root.mkdir()
    target = Store(tmp_path / "target-data")
    target_library = target.add_library(target_root, "keep target")
    target_tag = target.create_tag("keep target tag")
    before = target.list_libraries(), target.list_tags(), target._metadata_fingerprint()
    transaction = target._transaction
    @contextmanager
    def fail_after_import(connection):
        with transaction(connection):
            yield
            if connection.execute("PRAGMA user_version").fetchone()[0] == 2:
                raise sqlite3.OperationalError("injected restore failure")
    monkeypatch.setattr(target, "_transaction", fail_after_import)
    with pytest.raises(sqlite3.OperationalError):
        target.restore_backup(backup)
    assert metadata_state(target) == (1, False)
    assert (target.list_libraries(), target.list_tags(), target._metadata_fingerprint()) == before
    assert target.get_library(target_library.id).name == "keep target"
    assert target.list_tags() == (target_tag,)
    assert len(list((target.data_dir / "backups").glob("metadata-*.db"))) == 1


@pytest.mark.parametrize("format_version", [1, 2])
def test_restore_recomputes_scope_and_normalizes_rules_like_cold_recovery(managed, tmp_path, format_version):
    from notes.infrastructure.recovery import RecoveryManager
    store, library, root, _, _, _ = managed
    (root / ".git").mkdir()
    secret = root / ".git" / "secret.txt"
    secret.write_text("excluded fixture stays unchanged", encoding="utf-8")
    info = secret.stat()
    store.apply_scan(library.id, (FileEntry(".git/secret.txt", info.st_size, info.st_mtime_ns // 1_000_000),), False)
    if format_version == 2:
        store.create_smart_list("excluded", {"library_id": library.id, "folder": ".git", "include_unavailable": True})
    backup = tmp_path / f"scope-v{format_version}.json"
    store.export_backup(backup)
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["format_version"] == format_version
    payload["libraries"][0]["extensions"] = '[".TXT",".MD"]'
    for row in payload["documents"]:
        # Neither direction of this untrusted derived flag is authoritative.
        row["in_scope"] = int(row["relative_path"].startswith(".git/"))
    backup.write_text(json.dumps(payload), encoding="utf-8")
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.txt")}
    store.restore_backup(backup)
    assert store.get_library(library.id).extensions == (".txt", ".md")
    query = SearchQuery(library_id=library.id, include_unavailable=True)
    ordinary = store.search(query)
    assert ordinary.total == 3
    assert all(not hit.document.relative_path.startswith(".git/") for hit in ordinary.items)
    excluded = next(iter(store.list_smart_lists()), None)
    if excluded is None:
        excluded = store.create_smart_list("excluded", {
            "library_id": library.id, "folder": ".git", "include_unavailable": True})
    assert store.search_smart_list(excluded.id).total == 0
    allowed = store.create_smart_list("allowed", {
        "library_id": library.id, "folder": "folder", "include_unavailable": True})
    assert store.search_smart_list(allowed.id).total == 1
    cold_data = tmp_path / "cold-data"
    recovery = RecoveryManager(cold_data)
    current, candidate = recovery.inspect_current(), recovery.inspect_backup(backup)
    assert recovery.recover(backup, True, current["signature"], candidate["signature"])["recovered"]
    cold = Store(cold_data)
    assert {doc.relative_path: doc.in_scope for doc in cold.list_documents()} == {
        doc.relative_path: doc.in_scope for doc in store.list_documents()}
    assert cold.search(query).total == 3
    assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.txt")} == before


@pytest.mark.parametrize("mutation", [
    "v1_nonempty", "v2_missing", "bad_name_key", "duplicate_id", "duplicate_name",
    "unknown_query_version", "unknown_query_field", "extra_row_field", "malformed_id",
])
def test_malformed_smart_backup_is_rejected_without_changing_existing_state(managed, tmp_path, mutation):
    store, _, _, _, _, _ = managed
    saved = store.create_smart_list("existing", {})
    backup = tmp_path / "bad.json"
    store.export_backup(backup)
    payload = json.loads(backup.read_text(encoding="utf-8"))
    row = payload["smart_lists"][0]
    if mutation == "v1_nonempty":
        payload["format_version"] = 1
    elif mutation == "v2_missing":
        del payload["smart_lists"]
    elif mutation == "bad_name_key":
        row["name_key"] = "different"
    elif mutation == "duplicate_id":
        payload["smart_lists"].append(dict(row))
    elif mutation == "duplicate_name":
        payload["smart_lists"].append(dict(row, id=str(uuid.uuid4())))
    elif mutation == "unknown_query_version":
        row["query_json"] = '{"version":999,"query":{}}'
    elif mutation == "unknown_query_field":
        row["query_json"] = '{"version":1,"query":{"sql":"SELECT * FROM documents"}}'
    elif mutation == "extra_row_field":
        row["sql"] = "never execute"
    else:
        row["id"] = "not-an-id"
    backup.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NotesError):
        store.restore_backup(backup)
    assert store.list_smart_lists() == (saved,)


def test_invalid_running_rows_warn_without_touching_ordinary_document_navigation(managed, tmp_path):
    store, _, _, _, _, _ = managed
    saved = store.create_smart_list("corrupted after startup", {})
    with store._connect() as connection:
        connection.execute("UPDATE smart_lists SET query_json='not JSON' WHERE id=?", (saved.id,))
    with pytest.raises(NotesError):
        store.list_smart_lists()
    with pytest.raises(NotesError):
        store.search_smart_list(saved.id)
    assert len(store.list_documents()) == 3
    with pytest.raises(NotesError):
        store.export_backup(tmp_path / "bad-export.json")
    assert not (tmp_path / "bad-export.json").exists()
    # Store constructor checks structure; preflight separately validates values.
    reopened = Store(store.data_dir)
    assert len(reopened.list_documents()) == 3
    with pytest.raises(NotesError):
        reopened.list_smart_lists()


@pytest.mark.parametrize("schema", [
    None,
    "CREATE VIEW smart_lists AS SELECT 'x' id,'n' name,'n' name_key,'{}' query_json",
    "CREATE TABLE smart_lists(id TEXT PRIMARY KEY,name TEXT,name_key TEXT UNIQUE,query_json TEXT)",
    "CREATE TABLE smart_lists(id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL,query_json TEXT NOT NULL)",
])
def test_v2_missing_or_invalid_table_is_not_silently_repaired(managed, schema):
    store, _, _, _, _, _ = managed
    with store._connect() as connection:
        if schema:
            connection.execute(schema)
        connection.execute("PRAGMA user_version=2")
    before = store.metadata_path.read_bytes()
    with pytest.raises(NotesError):
        Store(store.data_dir)
    assert store.metadata_path.read_bytes() == before


def test_future_metadata_version_and_undeclared_v1_rows_are_not_ignored(managed):
    store, _, _, _, _, _ = managed
    with store._connect() as connection:
        connection.execute("PRAGMA user_version=3")
    with pytest.raises(NotesError):
        Store(store.data_dir)
    with store._connect() as connection:
        connection.execute("PRAGMA user_version=1")
        connection.execute(SMART_LIST_SCHEMA)
        connection.execute("INSERT INTO smart_lists VALUES (?,?,?,?)",
                           (str(uuid.uuid4()), "hidden", "hidden", encode_query({})))
    with pytest.raises(NotesError):
        Store(store.data_dir)
    with pytest.raises(NotesError):
        store.export_backup(store.data_dir / "must-not-drop.json")


def test_smart_changes_are_backup_dirty_and_internal_bookkeeping_is_not(managed):
    store, _, _, _, _, _ = managed
    first = store.run_automatic_backup(now=1789603200, force=True)
    assert first["status"] == "created" and not store.backup_status(now=1789603200)["dirty"]
    saved = store.create_smart_list("dirty", {})
    assert store.backup_status(now=1789603200)["dirty"]
    store.run_automatic_backup(now=1789603201, force=True)
    assert not store.backup_status(now=1789603201)["dirty"]
    store.update_smart_list(saved.id, "renamed", {"text": "changed"})
    assert store.backup_status(now=1789603201)["dirty"]
    store.run_automatic_backup(now=1789603202, force=True)
    store.set_setting("__maintenance.example", "ignored")
    assert not store.backup_status(now=1789603202)["dirty"]
    store.delete_smart_list(saved.id)
    assert store.backup_status(now=1789603202)["dirty"]
