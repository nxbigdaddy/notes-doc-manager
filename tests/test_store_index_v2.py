from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import uuid

import pytest

from notes.domain.models import FileEntry, NotesError, ReadResult, SearchQuery
from notes.infrastructure.store import Store


@pytest.fixture
def indexed_store(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    documents = store.apply_scan(library.id, (
        FileEntry("one.txt", 12, 100), FileEntry("two.txt", 12, 200)), complete=True)
    for document in documents:
        store.set_index(document.id, document.content_generation,
                        ReadResult("originalword", "utf-8", size_bytes=12, modified_at=document.modified_at))
    tag = store.create_tag("keep")
    store.set_document_tags(documents[0].id, (tag.id,))
    store.set_favorite(documents[0].id, True)
    return store, library, store.list_documents(), tag


def integrity_check(store):
    with sqlite3.connect(store.search_path) as connection:
        connection.execute("INSERT INTO document_fts(document_fts,rank) VALUES ('integrity-check',1)")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_external_content_has_one_body_copy_and_triggers_cover_update_rollback(indexed_store):
    store, _, documents, _ = indexed_store
    assert store.search_path.name == "search-v2.db"
    with sqlite3.connect(store.search_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "document_fts_content" not in names
        connection.execute("UPDATE indexed_documents SET body='changedword' WHERE document_id=?", (documents[0].id,))
    assert store.search(SearchQuery(text="changedword", mode="content")).total == 1
    assert store.search(SearchQuery(text="originalword", mode="content")).total == 1
    with pytest.raises(RuntimeError):
        with sqlite3.connect(store.search_path) as connection:
            connection.execute("UPDATE indexed_documents SET body='rolledbackword'")
            raise RuntimeError("simulate interrupted transaction")
    assert store.search(SearchQuery(text="rolledbackword", mode="content")).total == 0
    integrity_check(store)


def test_batch_replacements_deletions_disable_and_rebuild_keep_fts_consistent(indexed_store):
    store, library, documents, tag = indexed_store
    for index in range(4):
        text = f"replacement{index}"
        assert store.set_index_batch([
            (d.id, d.content_generation, ReadResult(text, "utf-8", size_bytes=d.size_bytes, modified_at=d.modified_at))
            for d in documents
        ]) == (True, True)
        integrity_check(store)
        assert store.search(SearchQuery(text=text)).total == 2
    assert store.search(SearchQuery(text="originalword")).total == 0
    store.set_index(documents[0].id, documents[0].content_generation, ReadResult(status="needs_encoding"))
    integrity_check(store)
    assert store.search(SearchQuery(text="replacement3")).total == 1
    store.set_library_enabled(library.id, False)
    integrity_check(store)
    with sqlite3.connect(store.search_path) as connection:
        assert connection.execute("SELECT count(*) FROM indexed_documents").fetchone()[0] == 0
    store.rebuild_index()
    integrity_check(store)
    assert store.get_document(documents[0].id).tags == (tag,)


def test_legacy_cache_is_untouched_and_all_generations_invalidate_including_pending(indexed_store):
    store, _, documents, tag = indexed_store
    store.invalidate_document(documents[1].id)
    original = store.list_documents()
    store.search_path.unlink()
    legacy = store.data_dir / "search.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE indexed_documents(row_id INTEGER PRIMARY KEY,document_id TEXT,generation INTEGER,body TEXT,encoding TEXT)")
        connection.execute("CREATE VIRTUAL TABLE document_fts USING fts5(body,tokenize='trigram')")
        for row_id, document in enumerate(original, 1):
            connection.execute("INSERT INTO indexed_documents VALUES (?,?,?,?,?)",
                               (row_id, document.id, document.content_generation, "legacybody", "utf-8"))
            connection.execute("INSERT INTO document_fts(rowid,body) VALUES (?,?)", (row_id, "legacybody"))
    original_bytes = legacy.read_bytes()
    upgraded = Store(store.data_dir)
    for document in original:
        current = upgraded.get_document(document.id)
        assert current.content_generation == document.content_generation + 1
        assert current.index_status == "pending"
    assert upgraded.get_document(documents[0].id).tags == (tag,)
    assert upgraded.get_document(documents[0].id).is_favorite
    assert legacy.read_bytes() == original_bytes
    assert upgraded.search(SearchQuery(text="legacybody")).total == 0
    generation = upgraded.get_document(documents[0].id).content_generation
    reopened = Store(store.data_dir)
    assert reopened.get_document(documents[0].id).content_generation == generation
    assert legacy.read_bytes() == original_bytes


@pytest.mark.parametrize("damage", ["normalization", "missing_trigger", "old_version"])
def test_known_cache_migration_rebuilds_only_cache_and_metadata_generations(indexed_store, damage):
    store, _, documents, tag = indexed_store
    with sqlite3.connect(store.search_path) as connection:
        if damage == "normalization":
            connection.execute("UPDATE index_info SET value='obsolete' WHERE key='normalization'")
        elif damage == "missing_trigger":
            connection.execute("DROP TRIGGER indexed_documents_ad")
        else:
            connection.execute("PRAGMA user_version=1")
    reopened = Store(store.data_dir)
    assert reopened.fts_available
    assert reopened.search(SearchQuery(text="originalword")).total == 0
    for document in documents:
        assert reopened.get_document(document.id).content_generation == document.content_generation + 1
    assert reopened.get_document(documents[0].id).tags == (tag,)
    integrity_check(reopened)


def test_interrupted_cache_migration_rolls_back_cache_and_retries_without_metadata_loss(indexed_store, monkeypatch):
    store, _, documents, tag = indexed_store
    with sqlite3.connect(store.search_path) as connection:
        connection.execute("UPDATE index_info SET value='obsolete' WHERE key='normalization'")
    real_reset = Store._reset_index_schema
    def interrupted(self, connection):
        with self._transaction(connection):
            connection.execute("DROP TRIGGER indexed_documents_ad")
            connection.execute("DROP TABLE document_fts")
            raise sqlite3.OperationalError("simulated interrupted DDL")
    monkeypatch.setattr(Store, "_reset_index_schema", interrupted)
    failed = Store(store.data_dir)
    assert not failed.fts_available
    assert failed.get_document(documents[0].id).tags == (tag,)
    assert failed.get_document(documents[0].id).content_generation > documents[0].content_generation
    with sqlite3.connect(failed.search_path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='document_fts'").fetchone()
    monkeypatch.setattr(Store, "_reset_index_schema", real_reset)
    recovered = Store(store.data_dir)
    assert recovered.fts_available and recovered.get_document(documents[0].id).tags == (tag,)
    integrity_check(recovered)


def test_future_cache_version_preserved_with_metadata_navigation(indexed_store):
    store, _, documents, tag = indexed_store
    with sqlite3.connect(store.search_path) as connection:
        connection.execute("PRAGMA user_version=99")
    original_bytes = store.search_path.read_bytes()
    reopened = Store(store.data_dir)
    assert not reopened.fts_available and reopened.search_warning
    result = reopened.search(SearchQuery(text="one", mode="name"))
    assert result.total == 1 and result.complete
    assert reopened.get_document(documents[0].id).tags == (tag,)
    assert reopened.get_document(documents[0].id).content_generation == documents[0].content_generation
    with pytest.raises(NotesError) as error:
        reopened.rebuild_index()
    assert error.value.code == "index_version"
    assert reopened.search_path.read_bytes() == original_bytes


def test_search_constructs_only_page_dtos_and_fetches_only_page_snippets(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    documents = store.apply_scan(library.id, tuple(FileEntry(f"file{i}.txt", 4, i) for i in range(120)), True)
    store.set_index_batch([(d.id, d.content_generation, ReadResult("body", "utf-8", size_bytes=4, modified_at=d.modified_at))
                           for d in documents])
    constructed = []
    original_document = store._document
    def document(*args, **kwargs):
        result = original_document(*args, **kwargs)
        constructed.append(result.id)
        return result
    monkeypatch.setattr(store, "_document", document)
    statements = []
    original_connect = store._connect
    @contextmanager
    def traced(*args, **kwargs):
        with original_connect(*args, **kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection
    monkeypatch.setattr(store, "_connect", traced)
    result = store.search(SearchQuery(text="body", page_size=7, page=2))
    assert result.total == result.covered == 120 and len(result.items) == 7
    assert len(constructed) == 7
    assert len([sql for sql in statements if sql.startswith("SELECT body FROM cache.indexed_documents")]) == 7


def test_sql_progress_cancellation_preserves_cache_and_next_query(indexed_store):
    store, _, _, _ = indexed_store
    calls = 0
    def cancellation():
        nonlocal calls
        calls += 1
        return calls >= 2
    result = store.search(SearchQuery(text="originalword"), cancellation)
    assert not result.complete
    assert store.search(SearchQuery(text="originalword")).total == 2
    integrity_check(store)


@pytest.mark.parametrize("values", [
    {"page": 0.5}, {"page_size": 1.5}, {"page": True}, {"page_size": False},
    {"page": 2**63}, {"text": None}, {"folder": None}, {"mode": []},
    {"tag_ids": "tag"}, {"tag_ids": [None]}, {"tag_ids": [""]},
    {"tag_ids": [1]}, {"tag_ids": ["\ud800"]}, {"library_id": []},
    {"favorites_only": "false"},
])
def test_invalid_query_never_disables_healthy_cache(indexed_store, values):
    store, _, _, _ = indexed_store
    with pytest.raises(NotesError) as failure:
        store.search(SearchQuery(**values))
    assert failure.value.code == "invalid_query"
    assert store.fts_available
    assert store.search(SearchQuery(text="originalword")).total == 2


def test_thousand_tags_use_and_semantics_without_expression_depth_overflow(indexed_store):
    store, _, documents, _ = indexed_store
    tags = [str(uuid.uuid4()) for _ in range(1000)]
    with store._connect() as connection, store._transaction(connection):
        connection.executemany("INSERT INTO tags VALUES (?,?,?)",
                               [(identifier, f"tag{index}", f"tag{index}") for index, identifier in enumerate(tags)])
        connection.executemany("INSERT INTO document_tags VALUES (?,?)", [(documents[0].id, tag) for tag in tags])
    result = store.search(SearchQuery(tag_ids=tuple(tags), text="originalword"))
    assert result.total == result.covered == 1 and result.complete
    assert result.items[0].document.id == documents[0].id
    assert store.fts_available
    with pytest.raises(NotesError):
        store.search(SearchQuery(tag_ids=tuple(tags) + ("extra",)))
    assert store.fts_available


def test_actual_sqlite_busy_is_retryable_and_not_cache_corruption(indexed_store, monkeypatch):
    store, _, _, _ = indexed_store
    holder = sqlite3.connect(store.metadata_path, isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    def short_timeout(path=None):
        connection = sqlite3.connect(path or store.metadata_path, timeout=0.02, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection
    monkeypatch.setattr(store, "_new_connection", short_timeout)
    try:
        with pytest.raises(NotesError) as failure:
            store.search(SearchQuery(text="originalword"))
        assert failure.value.code == "database_busy"
        assert store.fts_available and not store.search_warning
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert store.search(SearchQuery(text="originalword")).total == 2


def test_extended_locked_error_code_preserves_cache_health(indexed_store, monkeypatch):
    store, _, _, _ = indexed_store
    def locked(*args, **kwargs):
        error = sqlite3.OperationalError("locked shared-cache simulation")
        error.sqlite_errorcode = sqlite3.SQLITE_LOCKED | (1 << 8)
        raise error
    monkeypatch.setattr(store, "_search_sql", locked)
    with pytest.raises(NotesError) as failure:
        store.search(SearchQuery())
    assert failure.value.code == "database_busy" and store.fts_available


def test_index_session_does_not_hold_store_gate_or_read_transaction(indexed_store, monkeypatch):
    store, _, documents, _ = indexed_store
    opened = []
    original = store._new_connection
    def track(path=None):
        connection = original(path)
        opened.append(connection)
        return connection
    monkeypatch.setattr(store, "_new_connection", track)
    with store.index_session():
        anchor = opened[0]
        assert not anchor.in_transaction
        assert anchor.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(store.search, SearchQuery(text="originalword")).result(timeout=2)
        assert result.total == 2
        before = store.get_document(documents[0].id)
        with pytest.raises(NotesError) as failure:
            store.rebuild_index()
        assert failure.value.code == "index_busy"
        assert store.get_document(documents[0].id) == before
    assert store._index_sessions == 0 and store.last_index_checkpoint is not None
    integrity_check(store)


def test_index_session_does_not_force_out_external_wal_readers(indexed_store):
    store, _, documents, _ = indexed_store
    reader = None
    try:
        with store.index_session():
            reader = sqlite3.connect(store.search_path, isolation_level=None)
            reader.execute("BEGIN")
            old = reader.execute("SELECT body FROM indexed_documents WHERE document_id=?", (documents[0].id,)).fetchone()[0]
            document = documents[0]
            store.set_index(document.id, document.content_generation, ReadResult(
                "renewedwords", "utf-8", size_bytes=document.size_bytes, modified_at=document.modified_at))
        assert reader.execute("SELECT body FROM indexed_documents WHERE document_id=?", (document.id,)).fetchone()[0] == old
        assert store.last_index_checkpoint[1] > store.last_index_checkpoint[2]
        assert store.search(SearchQuery(text="renewedwords")).total == 1
    finally:
        if reader:
            reader.close()
    integrity_check(store)


def test_disabled_or_future_cache_index_session_is_noop(indexed_store):
    store, _, _, _ = indexed_store
    with sqlite3.connect(store.search_path) as connection:
        connection.execute("PRAGMA user_version=99")
    original_bytes = store.search_path.read_bytes()
    future = Store(store.data_dir)
    with future.index_session():
        assert future._index_sessions == 0
    assert future.search_path.read_bytes() == original_bytes
