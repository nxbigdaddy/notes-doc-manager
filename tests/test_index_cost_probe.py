import sqlite3
from collections import defaultdict

import pytest

from scripts.inspect_index_cost import TimedConnection, bucket, run


def test_classification_does_not_retain_parameters_or_statement_values():
    assert bucket("metadata", " SELECT * FROM documents WHERE id=? ") == "metadata_document_lookup"
    assert bucket("cache", "INSERT INTO indexed_documents VALUES (?,?)") == "cache_body_insert"
    assert bucket("cache", "SELECT row_id FROM indexed_documents WHERE document_id=?") == "cache_old_row_lookup"
    assert bucket("cache", "DELETE FROM indexed_documents WHERE row_id=?") == "cache_old_row_delete"
    assert bucket("metadata", "BEGIN IMMEDIATE") == "metadata_transaction"


def test_wrapper_keeps_rows_transactions_and_original_exceptions():
    measurements = defaultdict(lambda: [0, 0])
    native = sqlite3.connect(":memory:", isolation_level=None)
    native.row_factory = sqlite3.Row
    connection = TimedConnection(native, "metadata", measurements)
    try:
        connection.execute("CREATE TABLE documents(id TEXT PRIMARY KEY, body TEXT)")
        connection.executemany("INSERT INTO documents VALUES (?,?)", [("one", "private fixture")])
        row = connection.execute("SELECT * FROM documents WHERE id=?", ("one",)).fetchone()
        assert row["body"] == "private fixture"
        connection.execute("BEGIN")
        connection.execute("DELETE FROM documents")
        connection.execute("ROLLBACK")
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT * FROM nonexistent_table")
        assert connection.total_changes > 0
    finally:
        connection.close()
    assert measurements["metadata_document_lookup"][0] == 1
    assert measurements["metadata_executemany"][0] == 1
    assert measurements["metadata_close"][0] == 1
    assert "private fixture" not in str(dict(measurements))


@pytest.mark.parametrize("value", [0, 8191, 16384, True, "8192"])
def test_only_declared_hint_can_be_measured(value):
    with pytest.raises(ValueError):
        run(cache_kib=value, file_count=2, source_bytes=2048)


@pytest.mark.parametrize("hint", [None, 8192])
def test_tiny_probe_checks_scope_coverage_and_connection_configuration(hint):
    report = run(cache_kib=hint, file_count=2, source_bytes=2048)
    assert report["measurement_valid"]
    assert report["hint_is_session_local"] and report["protected_configuration_unchanged"]
    assert report["body_verification"] == {"hits": 2, "complete": True}
    assert report["experimental_cache_kib"] == hint
    baseline = next(item["before"] for item in report["connection_settings"] if item["scope"] == "cache")
    assert report["reopened_cache_settings"]["cache_size"] == baseline["cache_size"]
    assert report["memory_before"]["scope"] == "CLI storage process only; not GUI memory"
