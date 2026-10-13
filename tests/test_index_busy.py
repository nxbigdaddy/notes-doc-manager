"""Transient cache locks must not be published as permanent index failure."""

from contextlib import closing, contextmanager
import sqlite3

import pytest

from notes.domain.models import FileEntry, NotesError, ReadResult, SearchQuery
from notes.infrastructure.store import Store


@pytest.fixture
def pending_batch(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    documents = store.apply_scan(
        library.id,
        (FileEntry("one.txt", 12, 100), FileEntry("two.txt", 12, 200)),
        complete=True,
    )
    for document in documents:
        store.set_index(
            document.id, document.content_generation,
            ReadResult("originalword", "utf-8", size_bytes=12,
                       modified_at=document.modified_at),
        )
    documents = store.invalidate_documents(tuple(document.id for document in documents))
    items = tuple(
        (document.id, document.content_generation,
         ReadResult("modifiedword", "utf-8", size_bytes=12,
                    modified_at=document.modified_at))
        for document in documents
    )
    return store, documents, items


def cache_snapshot(store):
    with closing(sqlite3.connect(store.search_path)) as connection:
        return connection.execute(
            "SELECT document_id,generation,body,encoding FROM indexed_documents "
            "ORDER BY document_id"
        ).fetchall()


def short_cache_timeout(store, monkeypatch, statements):
    original = store._new_connection

    def connect(path=None):
        connection = original(path)
        if path == store.search_path:
            connection.execute("PRAGMA busy_timeout=20")
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_new_connection", connect)


def assert_pending_and_retryable(store, documents, items, previous_cache):
    assert store.fts_available
    assert store.search_warning == "previous warning"
    assert tuple(store.get_document(document.id) for document in documents) == documents
    assert cache_snapshot(store) == previous_cache
    result = store.search(SearchQuery(text="originalword", mode="content"))
    assert result.total == result.covered == 0
    assert result.uncovered == len(documents)
    # Query completion and content coverage are separate contracts: the query
    # finished, but no previous-generation body can cover the pending batch.
    assert result.complete
    assert store.set_index_batch(items) == (True,) * len(documents)
    assert all(store.get_document(document.id).index_status == "ready"
               for document in documents)
    result = store.search(SearchQuery(text="modifiedword", mode="content"))
    assert result.total == result.covered == len(documents)
    assert result.uncovered == 0
    with closing(sqlite3.connect(store.search_path)) as connection, connection:
        connection.execute(
            "INSERT INTO document_fts(document_fts,rank) VALUES ('integrity-check',1)"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_real_cache_begin_busy_preserves_health_and_pending_generation(pending_batch, monkeypatch):
    store, documents, items = pending_batch
    previous_cache = cache_snapshot(store)
    store.search_warning = "previous warning"
    statements = []
    short_cache_timeout(store, monkeypatch, statements)
    holder = sqlite3.connect(store.search_path, isolation_level=None)
    try:
        assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(NotesError) as failure:
            store.set_index_batch(items)
        assert failure.value.code == "database_busy"
        assert "BEGIN IMMEDIATE" in statements
        assert not any(sql.startswith("DELETE FROM indexed_documents")
                       for sql in statements)
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert_pending_and_retryable(store, documents, items, previous_cache)


def test_real_cache_commit_busy_rolls_back_all_writes_before_retry(pending_batch, monkeypatch):
    store, documents, items = pending_batch
    previous_cache = cache_snapshot(store)
    store.search_warning = "previous warning"
    statements = []
    short_cache_timeout(store, monkeypatch, statements)
    holder = sqlite3.connect(store.search_path, isolation_level=None)
    try:
        # A held rollback-journal reader lets BEGIN IMMEDIATE and batch writes
        # succeed, but makes COMMIT return real SQLITE_BUSY. Only this isolated
        # generated cache changes journal mode; the production WAL policy stays.
        assert holder.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM indexed_documents").fetchall()
        with pytest.raises(NotesError) as failure:
            store.set_index_batch(items)
        assert failure.value.code == "database_busy"
        assert any(sql.startswith("INSERT INTO indexed_documents") for sql in statements)
        assert "COMMIT" in statements and "ROLLBACK" in statements
        assert statements.index("COMMIT") < statements.index("ROLLBACK")
        assert cache_snapshot(store) == previous_cache
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert_pending_and_retryable(store, documents, items, previous_cache)


@pytest.mark.parametrize("code", [
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_BUSY | (2 << 8),
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_LOCKED | (1 << 8),
])
def test_extended_busy_codes_after_writes_roll_back_without_disabling_cache(
    pending_batch, monkeypatch, code,
):
    store, documents, items = pending_batch
    previous_cache = cache_snapshot(store)
    store.search_warning = "previous warning"
    original_transaction = store._transaction
    original_connect = store._connect
    cache_connections = set()

    @contextmanager
    def connect(path=None):
        with original_connect(path) as connection:
            if path == store.search_path:
                cache_connections.add(connection)
            try:
                yield connection
            finally:
                cache_connections.discard(connection)

    @contextmanager
    def transaction(connection):
        with original_transaction(connection):
            yield
            if connection in cache_connections:
                error = sqlite3.OperationalError("controlled extended lock")
                error.sqlite_errorcode = code
                raise error

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", connect)
        patch.setattr(store, "_transaction", transaction)
        with pytest.raises(NotesError) as failure:
            store.set_index_batch(items)
        assert failure.value.code == "database_busy"
    assert_pending_and_retryable(store, documents, items, previous_cache)


def test_missing_fts_table_still_disables_broken_cache(pending_batch):
    store, documents, items = pending_batch
    previous_cache = cache_snapshot(store)
    with closing(sqlite3.connect(store.search_path)) as connection, connection:
        connection.execute("DROP TABLE document_fts")
    assert store.set_index_batch(items) == (True, True)
    assert not store.fts_available
    assert store.search_warning == "正文索引不可用，请重建索引。"
    assert cache_snapshot(store) == previous_cache
    assert all(store.get_document(document.id).index_status == "error"
               for document in documents)
    result = store.search(SearchQuery(text="one.txt", mode="name"))
    assert result.total == 1 and result.complete


def test_corrupt_cache_still_downgrades_to_metadata_navigation(pending_batch):
    store, documents, items = pending_batch
    # No connection is open. This cache was generated only for this fixture.
    store.search_path.write_bytes(b"controlled invalid SQLite cache")
    assert store.set_index_batch(items) == (True, True)
    assert not store.fts_available
    assert store.search_warning == "正文索引不可用，请重建索引。"
    assert all(store.get_document(document.id).index_status == "error"
               for document in documents)
    result = store.search(SearchQuery(text="two.txt", mode="name"))
    assert result.total == 1 and result.complete
