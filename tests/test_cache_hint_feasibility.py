"""Test-only 8MiB cache hint; no production policy or large workload changes."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import threading

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError, ReadResult, SearchQuery
from notes.infrastructure.store import Store


BASE_TIME = 1790000000.0


def install_hint(monkeypatch):
    original = Store._new_connection

    def connection(store, path=None):
        result = original(store, path)
        if path == store.search_path:
            try:
                result.execute("PRAGMA cache_size=-8192")
            except BaseException:
                result.close()
                raise
        return result

    monkeypatch.setattr(Store, "_new_connection", connection)
    return original


def policy(connection):
    return {
        name: connection.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("cache_size", "synchronous", "journal_mode", "foreign_keys",
                     "busy_timeout", "wal_autocheckpoint")
    }


def cache_snapshot(store):
    with closing(sqlite3.connect(store.search_path)) as connection:
        return connection.execute(
            "SELECT document_id,generation,body,encoding FROM indexed_documents "
            "ORDER BY document_id"
        ).fetchall()


def assert_action_gate_released(store):
    entered = threading.Event()
    failures = []

    def action():
        try:
            with store.document_action_gate():
                entered.set()
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=action, daemon=True)
    worker.start()
    try:
        # A watchdog for deadlock, not an elapsed-time/performance assertion.
        assert entered.wait(5), "another thread could not obtain the action gate"
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive() and failures == []


@pytest.fixture
def managed(tmp_path, monkeypatch):
    install_hint(monkeypatch)
    root = tmp_path / "documents"
    root.mkdir()
    sources = {}
    for name in ("one", "two"):
        path = root / f"{name}.txt"
        sources[path] = f"originaltoken {name}".encode()
        path.write_bytes(sources[path])
    clock = [BASE_TIME]
    service = ApplicationService(
        tmp_path / "data", monitoring=False, wall_clock=lambda: clock[0],
    )
    try:
        def forbidden_anchor():
            pytest.fail("test-only cache hint must not enable an anchor")

        monkeypatch.setattr(service.store, "index_session", forbidden_anchor)
        library = service.store.add_library(root)
        assert service.call("scan", {"library_id": library.id})["indexed"] == 2
        documents = service.store.list_documents(library.id)
        tag = service.store.create_tag("保留管理信息")
        service.store.set_document_tags(documents[0].id, [tag.id])
        service.store.set_favorite(documents[0].id, True)
        yield service, library, sources, documents[0].id, tag, clock
    finally:
        service.close()


def assert_preserved(managed):
    service, _, sources, annotated_id, tag, _ = managed
    assert all(path.read_bytes() == original for path, original in sources.items())
    document = service.store.get_document(annotated_id)
    assert document.tags == (tag,) and document.is_favorite
    assert service.store._index_sessions == 0


def pending_items(managed):
    service, library, sources, _, _, _ = managed
    documents = service.store.list_documents(library.id)
    documents = service.store.invalidate_documents(tuple(doc.id for doc in documents))
    items = tuple(
        (document.id, document.content_generation,
         ReadResult(sources[next(path for path in sources if path.name == document.file_name)]
                    .decode(), "utf-8", size_bytes=document.size_bytes,
                    modified_at=document.modified_at))
        for document in documents
    )
    return documents, items


def test_hint_is_connection_local_and_other_policy_is_unchanged(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    original = Store._new_connection
    with closing(original(store)) as metadata, \
            closing(original(store, store.search_path)) as cache:
        baseline_metadata = policy(metadata)
        baseline_cache = policy(cache)
        assert baseline_metadata["synchronous"] == 2  # FULL
        assert baseline_cache["synchronous"] == 1  # NORMAL
        for values in (baseline_metadata, baseline_cache):
            assert values["journal_mode"] == "wal"
            assert values["foreign_keys"] == 1
            assert values["busy_timeout"] == 5000
            assert values["wal_autocheckpoint"] == 1000
        cache.execute("PRAGMA cache_size=-8192")
        assert policy(cache)["cache_size"] == -8192
        with closing(original(store, store.search_path)) as independent:
            assert policy(independent) == baseline_cache
    # Closing the modified connection does not persist the hint to the DB.
    with closing(original(store, store.search_path)) as reopened:
        assert policy(reopened) == baseline_cache
    install_hint(monkeypatch)
    for _ in range(2):
        with store._connect() as metadata, store._connect(store.search_path) as cache:
            assert policy(metadata) == baseline_metadata
            expected_cache = {**baseline_cache, "cache_size": -8192}
            assert policy(cache) == expected_cache
    assert store._index_sessions == 0 and store.last_index_checkpoint is None


def test_ready_and_stale_generations_preserve_coverage_and_management(managed):
    service, _, _, _, _, _ = managed
    documents, items = pending_items(managed)
    stale = (items[1][0], items[1][1] - 1, items[1][2])
    assert service.store.set_index_batch((items[0], stale)) == (True, False)
    result = service.store.search(SearchQuery(text="originaltoken", mode="content"))
    assert result.complete and result.total == result.covered == 1 and result.uncovered == 1
    assert service.store.get_document(documents[0].id).index_status == "ready"
    assert service.store.get_document(documents[1].id).index_status == "pending"
    assert_action_gate_released(service.store)
    assert_preserved(managed)


def test_actual_writer_busy_keeps_batch_unchanged_and_manual_retry_succeeds(managed):
    service, _, _, _, _, _ = managed
    store = service.store
    documents, items = pending_items(managed)
    before = cache_snapshot(store)
    with closing(sqlite3.connect(store.search_path, isolation_level=None)) as holder:
        assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        holder.execute("BEGIN IMMEDIATE")
        try:
            # Keep the actual production 5000ms wait policy, no timeout shortcut.
            with pytest.raises(NotesError) as failed:
                store.set_index_batch(items)
            assert failed.value.code == "database_busy"
            assert store.fts_available and not store.search_warning
            assert cache_snapshot(store) == before
            assert tuple(store.get_document(doc.id) for doc in documents) == documents
            assert_action_gate_released(store)
            assert_preserved(managed)
        finally:
            holder.rollback()
    # The failed BEGIN publishes nothing; release and explicitly retry once.
    assert store.set_index_batch(items) == (True, True)
    result = store.search(SearchQuery(text="originaltoken", mode="content"))
    assert result.complete and result.covered == result.total == 2 and result.uncovered == 0
    assert_action_gate_released(store)
    assert_preserved(managed)


def test_injected_postwrite_busy_rolls_back_without_changing_wal_policy(managed, monkeypatch):
    service, _, _, _, _, _ = managed
    store = service.store
    documents, items = pending_items(managed)
    before = cache_snapshot(store)
    original = store._transaction
    reached_postwrite = threading.Event()

    @contextmanager
    def fault_at_commit(connection):
        is_cache = Path(connection.execute("PRAGMA database_list").fetchone()[2]) == \
            store.search_path
        with original(connection):
            yield
            if is_cache:
                # Controlled rollback coverage, distinct from actual writer
                # contention above. Do not switch the cache to DELETE journal.
                reached_postwrite.set()
                error = sqlite3.OperationalError("controlled postwrite busy")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

    with monkeypatch.context() as patch:
        patch.setattr(store, "_transaction", fault_at_commit)
        with pytest.raises(NotesError) as failed:
            store.set_index_batch(items)
    assert failed.value.code == "database_busy" and reached_postwrite.is_set()
    assert store.fts_available and not store.search_warning
    assert cache_snapshot(store) == before
    assert tuple(store.get_document(doc.id) for doc in documents) == documents
    with store._connect(store.search_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert_action_gate_released(store)
    assert store.set_index_batch(items) == (True, True)
    assert_preserved(managed)


def test_corrupt_generated_cache_falls_back_without_losing_management(managed):
    service, _, _, _, _, _ = managed
    _, items = pending_items(managed)
    # This fixture owns the generated cache and has no open Store connection.
    service.store.search_path.write_bytes(b"controlled cache corruption")
    assert service.store.set_index_batch(items) == (True, True)
    assert not service.store.fts_available
    assert service.store.search_warning == "正文索引不可用，请重建索引。"
    result = service.store.search(SearchQuery(text=".txt", mode="name"))
    assert result.complete and result.total == 2
    assert all(document.index_status == "error"
               for document in service.store.list_documents())
    assert_action_gate_released(service.store)
    assert_preserved(managed)


def test_cancel_after_one_read_commits_only_completed_work_without_verified_stamp(
    managed, monkeypatch,
):
    service, library, _, _, _, clock = managed
    key = f"__maintenance.hash_verified.{library.id}"
    previous_verified = service.store.get_setting(key)
    assert previous_verified == str(BASE_TIME)
    clock[0] += 86401
    original = service._read_for_index
    completed = []

    def read_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.status == "ready"
        completed.append(args[1].id)
        service.call("cancel_scan")
        return result

    monkeypatch.setattr(service, "_read_for_index", read_then_cancel)
    result = service.call("scan", {"library_id": library.id, "force": True})
    assert len(completed) == 1
    assert result["cancelled"] and result["indexed"] == 1
    assert service.store.get_setting(key) == previous_verified
    documents = service.store.list_documents(library.id)
    assert {document.id for document in documents if document.index_status == "ready"} == \
        set(completed)
    assert len([document for document in documents if document.index_status == "pending"]) == 1
    coverage = service.store.search(SearchQuery(text="originaltoken", mode="content"))
    assert coverage.covered == 1 and coverage.uncovered == 1
    assert not service.call("state").busy
    assert_action_gate_released(service.store)
    assert_preserved(managed)
