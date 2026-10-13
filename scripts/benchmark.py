"""Reproducible isolated storage benchmark; never opens a real document library.

Examples:
    .venv\\Scripts\\python.exe scripts\\benchmark.py --files 1000 --total-mib 20
    .venv\\Scripts\\python.exe scripts\\benchmark.py --files 10000 --total-mib 500

Generated documents and databases live exclusively in TemporaryDirectory.
Only the JSON measurement report survives cleanup. This measures Store calls,
not GUI interaction, watcher latency, packaged startup, or Windows 11 support.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notes import __version__ as app_version  # noqa: E402
from notes.domain.models import FileEntry, ReadResult, SearchQuery  # noqa: E402
from notes.infrastructure.store import Store  # noqa: E402


def measure(call):
    started = time.perf_counter()
    result = call()
    return result, round(time.perf_counter() - started, 6)


def memory_usage():
    if os.name != "nt":
        return {"scope": "not measured on this platform"}
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return {"scope": "measurement failed"}
    return {"working_set_mib": round(counters.WorkingSetSize / 1024**2, 3),
            "peak_working_set_mib": round(counters.PeakWorkingSetSize / 1024**2, 3),
            "scope": "CLI storage process only; not GUI memory"}


def generate_corpus(root, count, target_bytes, seed):
    rng = random.Random(seed)
    words = ["文档", "管理", "服务器", "开发", "设计", "索引", "测试", "会议", "项目", "工作",
             "查询", "备份", "Windows", "Python", "hello", "world", "Straße", "café",
             "e\u0301", "emoji😀", "a_b", 'a"b', "x%y", "literal*"]
    entries = []
    # Fixed random pool creates reproducible, mixed Unicode nonuniform documents.
    lines = [
        (" ".join(rng.choices(words, k=25)) + " " + hashlib.sha256(str(i).encode()).hexdigest() + "\n").encode("utf-8")
        for i in range(2048)
    ]
    remaining = target_bytes
    for number in range(count):
        size = remaining // (count - number)
        marker = f"document {number:06d}\n服务器 管理 文档 中文\n".encode("utf-8")
        if number % 11 == 0:
            marker += "稀有标识符XYZ\n".encode("utf-8")
        body = bytearray(marker)
        while len(body) < size:
            body.extend(lines[rng.randrange(len(lines))])
        # Truncate at a valid UTF-8 boundary; ASCII pad keeps exact byte budget.
        body = bytes(body[:size]).decode("utf-8", errors="ignore").encode("utf-8")
        body += b" " * (size - len(body))
        path = root / f"folder-{number % 20:02d}" / f"document-{number:06d}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(body)
        stat = path.stat()
        entries.append(FileEntry(path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns // 1_000_000))
        remaining -= stat.st_size
    return tuple(entries)


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def run(args):
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated Store benchmark; real synthetic files, source Python; no GUI/monitor measurement",
        "app_version": app_version,
        "platform": platform.platform(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
        "dependencies": {name: package_version(name) for name in ("wxPython", "watchdog", "PyInstaller")},
        "files": args.files, "target_mib": args.total_mib, "seed": args.seed,
        "query_repetitions": args.query_repetitions, "index_batch_size": args.batch_size,
        "content_repetitions": args.content_repetitions,
        "max_query_seconds": args.max_query_seconds,
        "index_session": args.index_session,
        "store_source_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "src/notes/infrastructure/store.py").read_bytes()).hexdigest(),
        "script_source_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        "file_timestamp_unit": "UTC Unix milliseconds",
    }
    with tempfile.TemporaryDirectory(prefix="notes-benchmark-") as temporary:
        workspace = Path(temporary)
        root = workspace / "documents"
        root.mkdir()
        print("Generating isolated corpus...", flush=True)
        entries, report["generation_seconds"] = measure(
            lambda: generate_corpus(root, args.files, int(args.total_mib * 1024**2), args.seed))
        report["actual_bytes"] = sum(entry.size_bytes for entry in entries)
        store = Store(workspace / "data")
        library = store.add_library(root, "Synthetic benchmark")
        documents, report["scan_registration_seconds"] = measure(
            lambda: store.apply_scan(library.id, entries, complete=True))
        print(f"Registered {len(documents)} documents; indexing...", flush=True)

        def index_all():
            ready = 0
            batch = []
            report["peak_cache_wal_bytes"] = 0
            wal_path = Path(str(store.search_path) + "-wal")
            def flush():
                accepted = sum(store.set_index_batch(batch))
                if wal_path.exists():
                    report["peak_cache_wal_bytes"] = max(report["peak_cache_wal_bytes"], wal_path.stat().st_size)
                batch.clear()
                return accepted
            # Include anchor opening, all file reads/writes, final passive
            # checkpoint and anchor close in the measured index duration.
            with store.index_session() if args.index_session else nullcontext():
                for index, document in enumerate(documents, start=1):
                    path = root / document.relative_path
                    read = ReadResult(path.read_text(encoding="utf-8"), "utf-8",
                                      size_bytes=document.size_bytes, modified_at=document.modified_at)
                    batch.append((document.id, document.content_generation, read))
                    if len(batch) >= args.batch_size:
                        ready += flush()
                    if index % 1000 == 0:
                        print(f"Indexed {index}/{len(documents)}", flush=True)
                if batch:
                    ready += flush()
            report["last_passive_checkpoint"] = store.last_index_checkpoint
            return ready
        indexed, report["index_seconds"] = measure(index_all)
        report["index_accepted"] = indexed
        report["memory_after_index"] = memory_usage()
        report["database_bytes"] = {p.name: p.stat().st_size for p in store.data_dir.glob("*.db")}
        print(f"Index finished in {report['index_seconds']:.2f}s; querying...", flush=True)

        queries = [
            ("filename", SearchQuery(text="document-000", mode="name"), 0.2),
            ("long_common", SearchQuery(text="服务器", mode="content"), 0.5),
            ("long_rare", SearchQuery(text="稀有标识符XYZ", mode="content"), 0.5),
            ("short_one", SearchQuery(text="中", mode="content"), 3.0),
            ("short_two", SearchQuery(text="管理", mode="content"), 3.0),
            ("special_literal", SearchQuery(text='a"b', mode="content"), 0.5),
        ]
        report["queries"] = {}
        query_deadline = time.monotonic() + args.max_query_seconds
        for name, query, target in queries:
            repetitions = args.query_repetitions if name == "filename" else args.content_repetitions
            if time.monotonic() >= query_deadline:
                report["queries"][name] = {
                    "complete": False, "benchmark_complete": False, "observed_repetitions": 0,
                    "target_met": False, "warning": "Overall query benchmark time budget exhausted.",
                }
                continue
            warm = store.search(query)
            samples = []
            complete = warm.complete
            for _ in range(repetitions):
                if time.monotonic() >= query_deadline:
                    break
                result, elapsed = measure(lambda: store.search(query))
                samples.append(elapsed)
                complete = complete and result.complete
                if result.total != warm.total:
                    raise RuntimeError("Inconsistent query result counts")
                if not result.complete:
                    break
            samples.sort()
            benchmark_complete = len(samples) == repetitions and complete
            p95 = samples[min(len(samples) - 1, int(len(samples) * .95))] if samples else None
            report["queries"][name] = {
                "median_seconds": round(statistics.median(samples), 6) if samples else None,
                "p95_seconds": p95, "min_seconds": samples[0] if samples else None,
                "max_seconds": samples[-1] if samples else None,
                "total": warm.total, "covered": warm.covered, "uncovered": warm.uncovered,
                "complete": complete, "warning": warm.warning, "target_p95_seconds": target,
                "target_met": benchmark_complete and len(samples) >= 100 and p95 is not None and p95 <= target,
                "observed_timing_within_target": complete and p95 is not None and p95 <= target,
                "design_sample_count_met": len(samples) >= 100,
                "benchmark_complete": benchmark_complete, "configured_repetitions": repetitions,
                "observed_repetitions": len(samples),
            }
            print(f"{name}: p95 {p95}s; {warm.total} hits; samples={len(samples)}; complete={benchmark_complete}", flush=True)
        backup = workspace / "backup.json"
        _, report["export_seconds"] = measure(lambda: store.export_backup(backup))
        report["backup_bytes"] = backup.stat().st_size
        _, report["restore_seconds"] = measure(lambda: store.restore_backup(backup))
        report["restored_documents"] = len(store.list_documents())
        report["restored_pending"] = sum(d.index_status == "pending" for d in store.list_documents())
        report["memory_final"] = memory_usage()
        report["limitations"] = [
            "Synthetic corpus with a fixed vocabulary is not representative of every user's documents.",
            "Registration excludes filesystem scanning; index includes Python UTF-8 file read and Store writes.",
            "Query timing excludes GUI dispatch, debounce, and competing writes; database caches are warm.",
            "Peak memory includes corpus generation and Store work, not wxPython GUI or the packaged app.",
            "Backup/restore does not back up or restore document bodies.",
        ]
        report["index_target_met"] = report["index_seconds"] <= 120 if args.files == 10000 and args.total_mib == 500 else None
        store.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--total-mib", type=float, default=20)
    parser.add_argument("--query-repetitions", type=int, default=100)
    parser.add_argument("--content-repetitions", type=int, default=20)
    parser.add_argument("--max-query-seconds", type=float, default=180)
    parser.add_argument("--batch-size", type=int, default=50)
    # Experimental only: one anchored large run terminated without traceback.
    # A bounded retry completed but did not explain that earlier termination.
    # Released application behavior and the default benchmark remain unanchored.
    parser.add_argument("--index-session", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/benchmark-0.3.0.json"))
    args = parser.parse_args()
    if (not 1 <= args.files <= 100_000 or not 0 < args.total_mib <= 2000
            or not 1 <= args.query_repetitions <= 1000 or not 1 <= args.content_repetitions <= 1000
            or not 1 <= args.batch_size <= 1000 or not 1 <= args.max_query_seconds <= 3600):
        parser.error("files: 1..100000, total-mib: >0..2000, query-repetitions: 1..1000")
    if args.total_mib * 1024**2 / args.files < 128:
        parser.error("Use at least 128 bytes per document.")
    report = run(args)
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = {"benchmark_version": 1, "runs": []}
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
    existing["runs"].append(report)
    output.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()
