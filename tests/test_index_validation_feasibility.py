"""Feasibility only: candidate is not imported by production code."""
from contextlib import closing
from dataclasses import replace
import importlib.util
import math
from pathlib import Path
import random
import sqlite3

import pytest

from notes.domain.models import NotesError, ReadResult
from notes.infrastructure.store import INDEX_STATUSES


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_index_validation.py"
SPEC = importlib.util.spec_from_file_location("notes_index_validation_feasibility", SCRIPT)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


@pytest.fixture
def prepared():
    metadata, items = experiment.prepared_metadata(123)
    with closing(metadata):
        yield metadata, items


def compare(metadata, items):
    legacy = experiment.legacy_validation(metadata, iter(items))
    candidate = experiment.candidate_validation(metadata, iter(items))
    assert candidate == legacy
    return candidate


def test_mixed_generation_status_and_source_checks_keep_input_order(prepared):
    metadata, originals = prepared
    statuses = sorted(INDEX_STATUSES)
    items = []
    expected = []
    for index, (identifier, generation, read) in enumerate(originals):
        status = statuses[index % len(statuses)]
        generation_delta = int(index % 4 == 0)
        read = replace(read, status=status, size_bytes=read.size_bytes + index % 2,
                       modified_at=read.modified_at + int(index % 5 == 0),
                       truncated=index % 7 == 0)
        valid = generation_delta == 0 and (
            status != "ready" or (not read.truncated and index % 2 == 0 and index % 5 != 0))
        items.append((identifier, generation + generation_delta, read))
        expected.append(valid)
    ordering = list(range(len(items)))
    random.Random(20260921).shuffle(ordering)
    shuffled = [items[index] for index in ordering]
    result, accepted = compare(metadata, shuffled)
    assert result == tuple(expected[index] for index in ordering)
    assert accepted == tuple(item for item, valid in zip(shuffled, result, strict=True) if valid)


@pytest.mark.parametrize("status", sorted(INDEX_STATUSES - {"ready"}))
def test_non_ready_retains_legacy_size_mtime_truncation_semantics(prepared, status):
    metadata, items = prepared
    identifier, generation, read = items[0]
    result, accepted = compare(metadata, [(identifier, generation,
        replace(read, status=status, size_bytes=-1, modified_at=-1, truncated=True))])
    assert result == (True,)
    assert len(accepted) == 1


@pytest.mark.parametrize("position", [0, 61, 122])
def test_missing_identifier_raises_before_any_publication(prepared, position):
    metadata, originals = prepared
    items = list(originals)
    items[position] = ("not-present", 1, ReadResult())
    for function in (experiment.legacy_validation, experiment.candidate_validation):
        statements = []
        metadata.set_trace_callback(statements.append)
        try:
            with pytest.raises(NotesError) as caught:
                function(metadata, items)
        finally:
            metadata.set_trace_callback(None)
        assert caught.value.code == "not_found"
        assert statements and all(sql.startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("kind,code", [
    ("duplicate_and_invalid", "duplicate_document"),
    ("missing_and_invalid", "invalid_status"),
    ("invalid_at_end", "invalid_status"),
])
def test_full_prevalidation_still_precedes_sql(prepared, kind, code):
    metadata, originals = prepared
    invalid = ("bad-status", 1, ReadResult(status="made-up"))
    items = [*originals, invalid]
    if kind == "duplicate_and_invalid":
        items.append(originals[0])
    elif kind == "missing_and_invalid":
        items.insert(0, ("not-present", 1, ReadResult()))
    for function in (experiment.legacy_validation, experiment.candidate_validation):
        statements = []
        metadata.set_trace_callback(statements.append)
        try:
            with pytest.raises(NotesError) as caught:
                function(metadata, items)
        finally:
            metadata.set_trace_callback(None)
        assert caught.value.code == code
        assert statements == []


@pytest.mark.parametrize("limit", [1, 2, 13, 50, 123, 999])
def test_chunks_honor_connection_variable_limit_and_select_count(prepared, limit):
    metadata, items = prepared
    metadata.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
    actual_limit = metadata.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    expected = experiment.legacy_validation(metadata, items)
    statements = []
    metadata.set_trace_callback(statements.append)
    try:
        actual = experiment.candidate_validation(metadata, items)
    finally:
        metadata.set_trace_callback(None)
    assert actual == expected
    assert len(statements) == math.ceil(len(items) / min(actual_limit, experiment.CHUNK_CAP))
    assert all(sql.startswith("SELECT id,content_generation,size_bytes,modified_at")
               for sql in statements)


def test_thousand_rows_chunk_cap_and_reversed_order():
    metadata, items = experiment.prepared_metadata(1000)
    with closing(metadata):
        result, accepted = compare(metadata, items[::-1])
        assert result == (True,) * 1000
        assert accepted == items[::-1]


def test_non_string_affinity_uses_legacy_fallback():
    metadata = sqlite3.connect(":memory:")
    metadata.row_factory = sqlite3.Row
    with closing(metadata):
        metadata.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, content_generation, "
                         "size_bytes, modified_at)")
        metadata.execute("INSERT INTO documents VALUES ('42',1,0,0)")
        result, accepted = compare(metadata, [(42, 1, ReadResult())])
        assert result == (True,)
        assert accepted[0][0] == 42


def test_zero_variable_limit_retains_legacy_error_on_unprepared_sql(prepared):
    metadata, items = prepared
    metadata.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 0)
    for function in (experiment.legacy_validation, experiment.candidate_validation):
        with pytest.raises(sqlite3.OperationalError, match="too many SQL variables"):
            function(metadata, items[:1])


def test_empty_batch_never_accesses_connection():
    assert experiment.legacy_validation(None, ()) == ((), ())
    assert experiment.candidate_validation(None, ()) == ((), ())


@pytest.mark.parametrize("rows,rounds,batch", [
    (0, 1, 1), (1001, 1, 50), (True, 1, 1), (1, 0, 1), (1, 6, 1),
    (1, True, 1), (1, 1, 0), (1, 1, 2), (1, 1, True),
])
def test_experiment_is_bounded(rows, rounds, batch):
    with pytest.raises(ValueError):
        experiment.validate_options(rows, rounds, batch)
