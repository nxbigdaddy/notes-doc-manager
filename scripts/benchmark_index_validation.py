"""Narrow, non-production metadata validation feasibility experiment.

No Store is constructed, no document bodies/GUI/editor are accessed, and no
index transaction is changed. The in-memory metadata fixture has at most 1000
rows. Timings exclude setup and measure at most five paired warm passes of
the validation prefix only, not ApplicationService throughput.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import time
import uuid

from notes.domain.models import NotesError, ReadResult
from notes.infrastructure.store import INDEX_STATUSES, SCHEMA


ROOT = Path(__file__).resolve().parents[1]
MAX_ROWS = 1000
MAX_ROUNDS = 5
CHUNK_CAP = 900


def legacy_validation(metadata, items):
    """Literal production prevalidation/read loop, with no publication suffix."""
    items = tuple(items)
    if not items:
        return (), ()
    if len({item[0] for item in items}) != len(items):
        raise NotesError("duplicate_document", "索引批次不能包含重复文档。")
    if any(read.status not in INDEX_STATUSES for _, _, read in items):
        raise NotesError("invalid_status", "索引状态无效。")
    accepted = []
    result = []
    for document_id, generation, read in items:
        row = metadata.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise NotesError("not_found", "文档不存在。")
        valid = row["content_generation"] == generation and not (
            read.status == "ready" and (read.truncated or read.size_bytes != row["size_bytes"]
                                       or read.modified_at != row["modified_at"]))
        result.append(valid)
        if valid:
            accepted.append((document_id, generation, read))
    return tuple(result), tuple(accepted)


def candidate_validation(metadata, items):
    """Experimental read substitution; NOT called by production."""
    items = tuple(items)
    if not items:
        return (), ()
    if len({item[0] for item in items}) != len(items):
        raise NotesError("duplicate_document", "索引批次不能包含重复文档。")
    if any(read.status not in INDEX_STATUSES for _, _, read in items):
        raise NotesError("invalid_status", "索引状态无效。")
    limit = metadata.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    # SQLite TEXT affinity may match a numeric parameter to a string id.
    # Preserve that legacy edge rather than changing behavior through dict keys.
    if limit < 1 or any(type(item[0]) is not str for item in items):
        return legacy_validation(metadata, items)
    chunk_size = min(CHUNK_CAP, limit)
    rows = {}
    for offset in range(0, len(items), chunk_size):
        chunk = items[offset:offset + chunk_size]
        parameters = tuple(item[0] for item in chunk)
        sql = ("SELECT id,content_generation,size_bytes,modified_at FROM documents WHERE id IN ("
               + ",".join("?" for _ in parameters) + ")")
        for row in metadata.execute(sql, parameters):
            rows[row["id"]] = row
    accepted = []
    result = []
    # SQL result order does not determine acceptance or missing-id validation.
    for document_id, generation, read in items:
        row = rows.get(document_id)
        if row is None:
            raise NotesError("not_found", "文档不存在。")
        valid = row["content_generation"] == generation and not (
            read.status == "ready" and (read.truncated or read.size_bytes != row["size_bytes"]
                                       or read.modified_at != row["modified_at"]))
        result.append(valid)
        if valid:
            accepted.append((document_id, generation, read))
    return tuple(result), tuple(accepted)


def prepared_metadata(count):
    if type(count) is not int or not 1 <= count <= MAX_ROWS:
        raise ValueError("fixture row count must be 1..1000")
    metadata = sqlite3.connect(":memory:")
    metadata.row_factory = sqlite3.Row
    try:
        metadata.execute("PRAGMA foreign_keys=ON")
        for sql in SCHEMA:
            metadata.execute(sql)
        metadata.execute(
            "INSERT INTO libraries VALUES (?,?,?,?,?,?,?,?)",
            ("generated-library", "generated", "unused-fixture-root", "unused-fixture-root",
             1, "online", '[".txt"]', "[]"),
        )
        items = []
        for index in range(count):
            identifier = f"document-{index:04}"
            size = 20971 + index
            modified = 1700000000000000000 + index
            generation = 1 + index % 7
            metadata.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, "generated-library", f"样例/{identifier}.txt",
                 f"样例/{identifier}.txt", f"{identifier}.txt", size, modified, "online",
                 1, "pending", generation, 0, None, None),
            )
            items.append((identifier, generation, ReadResult(
                status="ready", size_bytes=size, modified_at=modified,
            )))
        metadata.commit()
        metadata.execute("PRAGMA query_only=ON")
        return metadata, tuple(items)
    except BaseException:
        metadata.close()
        raise


def validate_options(count, rounds, batch_size):
    if type(count) is not int or not 1 <= count <= MAX_ROWS:
        raise ValueError("row count must be 1..1000")
    if type(rounds) is not int or not 1 <= rounds <= MAX_ROUNDS:
        raise ValueError("rounds must be 1..5")
    if type(batch_size) is not int or not 1 <= batch_size <= count:
        raise ValueError("batch size must be 1..row count")


def run(count=1000, rounds=5, batch_size=50):
    validate_options(count, rounds, batch_size)
    source_path = ROOT / "src" / "notes" / "infrastructure" / "store.py"
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    metadata, items = prepared_metadata(count)
    with closing(metadata):
        batches = tuple(items[index:index + batch_size]
                        for index in range(0, count, batch_size))
        functions = {"legacy": legacy_validation, "candidate": candidate_validation}
        traces = {}
        expected = tuple(legacy_validation(metadata, batch) for batch in batches)
        for name, function in functions.items():
            statements = []
            metadata.set_trace_callback(statements.append)
            try:
                actual = tuple(function(metadata, batch) for batch in batches)
            finally:
                metadata.set_trace_callback(None)
            if actual != expected:
                raise AssertionError("candidate acceptance differs from legacy")
            traces[name] = sum(sql.startswith("SELECT") for sql in statements)
        samples = {name: [] for name in functions}
        for round_number in range(rounds):
            order = ("legacy", "candidate") if round_number % 2 == 0 else ("candidate", "legacy")
            for name in order:
                start = time.perf_counter_ns()
                actual = tuple(functions[name](metadata, batch) for batch in batches)
                elapsed = time.perf_counter_ns() - start
                if actual != expected:
                    raise AssertionError("timed acceptance differs from legacy")
                samples[name].append(elapsed / 1_000_000)
        variable_limit = metadata.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    after = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if after != before:
        raise RuntimeError("production source changed during experiment")
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "in-memory metadata validation prefix only; no app scan, body IO or FTS writes",
        "production_enabled": False,
        "configuration": {"rows": count, "rounds": rounds, "batch_size": batch_size,
                          "chunk_cap": CHUNK_CAP, "variable_limit": variable_limit},
        "sqlite_version": sqlite3.sqlite_version,
        "store_sha256_before": before,
        "store_sha256_after": after,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "semantic_match": True,
        "select_counts_per_pass": traces,
        "samples_ms_per_pass": samples,
        "medians_ms_per_pass": {name: statistics.median(values)
                                for name, values in samples.items()},
        "limitations": [
            "warm single-connection in-memory reads, not disk metadata or end-to-end timing",
            "all-ready timing sample; mixed and failure semantics are separate unit tests",
            "no contention, external metadata writers or concurrent restore tested",
            "no prediction of GUI latency, search p95, 500MiB throughput or release benefit",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    report = run(args.rows, args.rounds, args.batch_size)
    directory = ROOT / ".artifacts"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = directory / f"index-validation-{stamp}-{uuid.uuid4().hex[:12]}.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
