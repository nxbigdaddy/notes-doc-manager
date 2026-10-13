"""One bounded SQL-call cost observation on an isolated real service scan."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from notes.application.service import ApplicationService  # noqa: E402
from scripts.benchmark import memory_usage  # noqa: E402
from scripts.profile_scan import (  # noqa: E402
    ANCHOR, corpus_fingerprint, generate_corpus, isolated_workspace, prepare_artifacts,
    runtime_fingerprint, source_fingerprint, validate_options,
)


def bucket(scope, statement):
    text = " ".join(statement.upper().split())
    if scope == "metadata" and text.startswith("SELECT * FROM DOCUMENTS WHERE ID="):
        # This SQL shape is also used by generation invalidation, so it must
        # not be labelled as exclusively set_index_batch prevalidation.
        return "metadata_document_lookup"
    if scope == "cache":
        if text.startswith("INSERT INTO INDEXED_DOCUMENTS"):
            return "cache_body_insert"
        if text.startswith("SELECT ROW_ID FROM INDEXED_DOCUMENTS"):
            return "cache_old_row_lookup"
        if text.startswith("DELETE FROM INDEXED_DOCUMENTS"):
            return "cache_old_row_delete"
    if text.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
        return scope + "_transaction"
    return scope + "_other"


class TimedConnection:
    """Forward to the original connection; retain no SQL text or parameters."""
    def __init__(self, connection, scope, measurements):
        self.connection = connection
        self.scope = scope
        self.measurements = measurements

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def _call(self, label, function, *args):
        started = time.perf_counter_ns()
        try:
            return function(*args)
        finally:
            values = self.measurements[label]
            values[0] += 1
            values[1] += time.perf_counter_ns() - started

    def execute(self, statement, parameters=()):
        return self._call(bucket(self.scope, statement), self.connection.execute, statement, parameters)

    def executemany(self, statement, parameters):
        return self._call(self.scope + "_executemany", self.connection.executemany, statement, parameters)

    def close(self):
        return self._call(self.scope + "_close", self.connection.close)


def connection_settings(connection):
    return {name: connection.execute("PRAGMA " + name).fetchone()[0] for name in (
        "cache_size", "cache_spill", "page_size", "synchronous", "journal_mode",
        "foreign_keys", "busy_timeout", "wal_autocheckpoint",
    )}


def run(*, cache_kib=None, file_count=1000, source_bytes=20 * 1024 * 1024):
    if cache_kib is not None and (type(cache_kib) is not int or cache_kib != 8192):
        raise ValueError("Only the unchanged baseline or the declared8192KiB experiment is allowed")
    validate_options(file_count, source_bytes, 20260917, 180)
    # Warm the read-only memory API before comparing loaded runtime identities.
    memory_before = memory_usage()
    before_source = source_fingerprint()
    before_runtime = runtime_fingerprint()
    measurements = defaultdict(lambda: [0, 0])
    settings = []
    report = {
        "format_version": 3, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "one instrumented service scan; original SQL and connection initialization retained",
        "files": file_count, "source_bytes": source_bytes, "seed": 20260917,
        "experimental_cache_kib": cache_kib, "memory_before": memory_before,
    }
    with isolated_workspace() as workspace:
        root = workspace / "documents"
        generate_corpus(root, report["files"], report["source_bytes"], report["seed"])
        before_corpus = corpus_fingerprint(root)
        service = ApplicationService(workspace / "metadata", monitoring=False)
        original = service.store._new_connection
        try:
            library = service.store.add_library(root, "Isolated statement-cost sample")

            def timed_connection(path=None):
                connection = original(path)
                scope = "cache" if path == service.store.search_path else "metadata"
                try:
                    before = connection_settings(connection)
                    if scope == "cache" and cache_kib is not None:
                        connection.execute("PRAGMA cache_size=-8192")
                    after = connection_settings(connection)
                    settings.append({"scope": scope, "before": before, "after": after})
                except BaseException:
                    connection.close()
                    raise
                return TimedConnection(connection, scope, measurements)

            service.store._new_connection = timed_connection
            started = time.perf_counter()
            report["scan"] = service.call("scan", {"library_id": library.id}, timeout=180)
            report["scan_seconds"] = time.perf_counter() - started
            service.store._new_connection = original
            result = service.call("query", {"mode": "name", "page_size": 1})
            report["coverage"] = {
                "total": result.total, "covered": result.covered, "uncovered": result.uncovered,
            }
            body = service.call("query", {"mode": "content", "text": ANCHOR, "page_size": 1})
            report["body_verification"] = {"hits": body.total, "complete": body.complete}
            with closing(original(service.store.search_path)) as connection:
                report["reopened_cache_settings"] = connection_settings(connection)
            defaults = next(item["before"] for item in settings if item["scope"] == "cache")
            report["hint_is_session_local"] = (
                report["reopened_cache_settings"]["cache_size"] == defaults["cache_size"]
            )
            report["corpus_unchanged"] = before_corpus == corpus_fingerprint(root)
            report["corpus"] = before_corpus
        finally:
            service.store._new_connection = original
            service.close()
    report["calls"] = {
        name: {"count": values[0], "seconds": values[1] / 1_000_000_000}
        for name, values in sorted(measurements.items())
    }
    report["source_before"] = before_source
    report["source_unchanged"] = before_source == source_fingerprint()
    report["runtime_before"] = before_runtime
    report["runtime_unchanged"] = before_runtime == runtime_fingerprint()
    report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["connection_settings"] = settings
    protected = ("page_size", "synchronous", "journal_mode", "foreign_keys",
                 "busy_timeout", "wal_autocheckpoint")
    report["protected_configuration_unchanged"] = bool(settings) and all(
        all(item["before"][name] == item["after"][name] for name in protected)
        and item["after"]["synchronous"] == (1 if item["scope"] == "cache" else 2)
        and item["after"]["foreign_keys"] == 1 and item["after"]["journal_mode"] == "wal"
        and item["after"]["busy_timeout"] == 5000
        and item["after"]["wal_autocheckpoint"] == 1000
        and (item["before"]["cache_size"] == item["after"]["cache_size"]
             if item["scope"] == "metadata" or cache_kib is None
             else item["after"]["cache_size"] == -8192)
        for item in settings
    )
    report["memory_after"] = memory_usage()
    report["memory_measured"] = all(
        "working_set_mib" in report[name] and "peak_working_set_mib" in report[name]
        for name in ("memory_before", "memory_after")
    )
    report["measurement_valid"] = (
        report["source_unchanged"] and report["runtime_unchanged"] and report["corpus_unchanged"]
        and report["scan"]["indexed"] == file_count and report["scan"]["failures"] == 0
        and report["coverage"] == {"total": file_count, "covered": file_count, "uncovered": 0}
        and report["body_verification"] == {"hits": file_count, "complete": True}
        and report["hint_is_session_local"] and report["protected_configuration_unchanged"]
        and report["memory_measured"]
    )
    report["limitations"] = [
        "No cProfile, but per-call wrappers/timers add overhead; not a throughput or p95 benchmark.",
        "execute timing excludes cursor.fetchone/fetchall and Python validation after the call.",
        "Categories distinguish SQL shape, not Python call site; document lookup includes invalidation reads.",
        "Only scan-phase execute/executemany/close are measured, not connection initialization.",
        "No GUI subscribers, watcher, editor, index_session, or 500MiB extrapolation.",
        "Only coarse statement categories are retained; no SQL parameters or bodies are recorded.",
        "cache_size is a per-connection suggestion, not a hard bound on process memory.",
        "Memory counters cover this CLI process, not SQLite allocations or the wx GUI.",
        "Connection configuration observations add overhead, equally to baseline and candidate.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-kib", type=int, choices=[8192], default=None)
    args = parser.parse_args()
    report = run(cache_kib=args.cache_kib)
    path = prepare_artifacts() / (
        "index-call-cost-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-" + uuid.uuid4().hex[:12] + ".json"
    )
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"Report: {path}", flush=True)
    print(json.dumps({"valid": report["measurement_valid"], "seconds": report["scan_seconds"],
                      "cache_kib": args.cache_kib, "memory": report["memory_after"],
                      "calls": report["calls"]}, ensure_ascii=False), flush=True)
    return 0 if report["measurement_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
