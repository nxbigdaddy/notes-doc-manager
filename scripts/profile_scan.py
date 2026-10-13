"""One bounded, instrumented ApplicationService scan of generated documents.

Run from the project venv: python scripts/profile_scan.py
Only .artifacts/profile-scan-* workspaces are generated and then cleaned.
The default/maximum corpus is 1000 files / 20 MiB; this is not a 500 MiB test.
No GUI, watcher, editor, user library, index_session, or automatic repeated run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future
from contextlib import contextmanager
import cProfile
import codecs
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import marshal
import math
import os
from pathlib import Path
import platform
import pstats
import random
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from types import ModuleType
import uuid

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
sys.path.insert(0, str(ROOT / "src"))

from notes import __version__  # noqa: E402
from notes.application.service import ApplicationService  # noqa: E402
from notes.infrastructure import files  # noqa: E402
from notes.infrastructure.runtime import require_safe_runtime  # noqa: E402
from notes.infrastructure.store import Store  # noqa: E402

MAX_FILES = 1000
MAX_BYTES = 20 * 1024 * 1024
ANCHOR = "profile-shared-anchor"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_chain(path):
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Profiling paths must not traverse symlinks or reparse points")


def prepare_artifacts():
    _plain_chain(ROOT)
    if ARTIFACTS.exists():
        _plain_chain(ARTIFACTS)
    else:
        ARTIFACTS.mkdir()
    return ARTIFACTS.resolve(strict=True)


def verify_workspace(path, artifacts):
    """Validate the exact recursive-cleanup target, both before use and cleanup."""
    path = Path(path).absolute()
    artifacts = Path(artifacts).resolve(strict=True)
    if not path.name.startswith("profile-scan-") or path.parent != artifacts:
        raise ValueError("Refusing a profiling workspace outside the fixed artifacts directory")
    _plain_chain(path)
    resolved = path.resolve(strict=True)
    if resolved.parent != artifacts or not resolved.is_dir():
        raise ValueError("Refusing an unexpected resolved cleanup target")
    return resolved


@contextmanager
def isolated_workspace():
    artifacts = prepare_artifacts()
    # Disable the default finalizer's deletion: if validation fails, leaving an
    # unverified path untouched is safer than recursively deleting it later.
    temporary = tempfile.TemporaryDirectory(prefix="profile-scan-", dir=artifacts, delete=False)
    try:
        workspace = verify_workspace(temporary.name, artifacts)
        yield workspace
    finally:
        verify_workspace(temporary.name, artifacts)
        temporary.cleanup()


def validate_options(count, total_bytes, seed, timeout):
    if type(count) is not int or not 1 <= count <= MAX_FILES:
        raise ValueError("files must be an integer in 1..1000")
    if type(total_bytes) is not int or not count * 128 <= total_bytes <= MAX_BYTES:
        raise ValueError("corpus must have at least 128 bytes/file and no more than 20 MiB")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError("scan timeout must be in 1..300 seconds")


def generate_corpus(root, count, total_bytes, seed):
    """Deterministic mixed Unicode text, exact byte budget, regular files only."""
    root.mkdir()
    randomizer = random.Random(seed)
    words = ("文档", "管理", "服务器", "设计", "索引", "测试", "会议", "工作", "备份",
             "Windows", "Python", "Straße", "café", "e\u0301", "emoji😀", "a_b", 'a"b', "x%y")
    lines = [
        (" ".join(randomizer.choices(words, k=25)) + " "
         + hashlib.sha256(str(index).encode("ascii")).hexdigest() + "\n").encode("utf-8")
        for index in range(2048)
    ]
    remaining = total_bytes
    for index in range(count):
        size = remaining // (count - index)
        content = bytearray(f"{ANCHOR}\ndocument {index:06d}\n".encode("utf-8"))
        while len(content) < size:
            content.extend(lines[randomizer.randrange(len(lines))])
        encoded = bytes(content[:size]).decode("utf-8", errors="ignore").encode("utf-8")
        encoded += b" " * (size - len(encoded))
        directory = root / f"folder-{index % 20:02d}"
        directory.mkdir(exist_ok=True)
        with (directory / f"document-{index:06d}.txt").open("xb") as stream:
            stream.write(encoded)
        remaining -= len(encoded)


def corpus_fingerprint(root):
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(root.rglob("*.txt")):
        _plain_chain(path)
        count += 1
        total += path.stat().st_size
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(sha256_file(path).encode("ascii") + b"\n")
    return {"files": count, "bytes": total, "sha256": digest.hexdigest()}


def source_fingerprint():
    source = {path.relative_to(ROOT).as_posix(): sha256_file(path)
              for path in sorted((ROOT / "src").rglob("*.py"))}
    harness = {Path(__file__).resolve().relative_to(ROOT).as_posix(): sha256_file(__file__)}
    configuration = {path.relative_to(ROOT).as_posix(): sha256_file(path)
                     for path in sorted(ROOT.glob("requirements-*.lock.txt"))}
    configuration["pyproject.toml"] = sha256_file(ROOT / "pyproject.toml")
    result = {"source_sha256": source, "harness_sha256": harness, "configuration_sha256": configuration}
    result["aggregate_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return result


def loaded_windows_module(name):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    handle = kernel.GetModuleHandleW(name)
    if not handle:
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("Could not identify an actually loaded runtime module")
    return Path(buffer.value)


def runtime_fingerprint():
    require_safe_runtime()
    # The normal reader lazily imports its supported encodings. Probe those
    # capabilities before hashing the loaded runtime so later module discovery
    # is not confused with a changed DLL/source. This startup cost is explicitly
    # outside the measured scan, just like service/schema initialization.
    for encoding in files.ENCODINGS:
        codecs.lookup(encoding)
    installed = dict(sorted((distribution.metadata["Name"], distribution.version)
                            for distribution in importlib.metadata.distributions()
                            if distribution.metadata["Name"]))
    with sqlite3.connect(":memory:") as connection:
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    connection.close()
    loaded_sqlite = loaded_windows_module("sqlite3.dll")
    loaded_python = loaded_windows_module(f"python{sys.version_info.major}{sys.version_info.minor}.dll")
    binaries = {str(Path(sys.executable).resolve())}
    loaded_sources = {}
    unavailable_modules = {}
    if getattr(sys, "_base_executable", None):
        binaries.add(str(Path(sys._base_executable).resolve()))
    for module in list(sys.modules.values()):
        if isinstance(module, ModuleType):
            filename = vars(module).get("__file__")
            if isinstance(filename, str):
                path = Path(filename)
                if path.suffix.lower() in {".pyd", ".so", ".dll"}:
                    binaries.add(str(path.resolve()))
                elif path.suffix.lower() == ".py":
                    if path.is_file():
                        loaded_sources[str(path.resolve())] = sha256_file(path)
                    else:
                        unavailable_modules[module.__name__] = filename
    for path in (loaded_sqlite, loaded_python):
        if path:
            binaries.add(str(path.resolve()))
    hashes = {path: sha256_file(path) for path in sorted(binaries)}
    manifests = {}
    for name in ("python-runtime.json", "sqlite-runtime.json"):
        path = ROOT / ".tools" / name
        if path.is_file():
            manifests[name] = {"sha256": sha256_file(path), "content": json.loads(path.read_text(encoding="utf-8"))}
    expected = manifests.get("sqlite-runtime.json", {}).get("content", {}).get("dll_sha256")
    return {
        "python": sys.version, "implementation": platform.python_implementation(),
        "executable": sys.executable, "base_prefix": sys.base_prefix, "prefix": sys.prefix,
        "sqlite": sqlite3.sqlite_version, "sqlite_threadsafety": sqlite3.threadsafety,
        "sqlite_compile_options": options, "loaded_sqlite_path": str(loaded_sqlite) if loaded_sqlite else None,
        "loaded_sqlite_matches_manifest": hashes.get(str(loaded_sqlite.resolve())) == expected
        if loaded_sqlite and expected else None,
        "loaded_binary_sha256": hashes, "private_runtime_manifests": manifests,
        "loaded_python_source_sha256": loaded_sources, "unhashed_loaded_modules": unavailable_modules,
        "installed_packages": installed,
    }


def physical_memory():
    if os.name != "nt":
        return {"measured": False}
    import ctypes
    from ctypes import wintypes
    class Status(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                    ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                    ("extended", ctypes.c_ulonglong)]
    info = Status()
    info.length = ctypes.sizeof(info)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(Status),)
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(info)):
        return {"measured": False}
    return {"measured": True, "total_bytes": info.total, "available_bytes": info.available,
            "load_percent": info.load}


PHASES = {
    "filesystem_enumeration": files.scan_directory,
    "metadata_registration": Store.apply_scan,
    "generation_invalidation": Store.invalidate_documents,
    "read_and_validate": ApplicationService._read_for_index,
    "index_batch_commit": Store.set_index_batch,
    "status_notification": ApplicationService._notify,
}
DETAILS = {
    "validate_document_path": files.validate_document_path,
    "read_document": files.read_document,
    "new_sqlite_connection": Store._new_connection,
    "index_session": Store.index_session,
}


def function_key(function):
    code = inspect.unwrap(function).__code__
    return code.co_filename, code.co_firstlineno, code.co_name


def loaded_code_fingerprint():
    return {name: hashlib.sha256(marshal.dumps(inspect.unwrap(function).__code__)).hexdigest()
            for name, function in {**PHASES, **DETAILS}.items()}


def function_record(key, values):
    filename, line, name = key
    primitive, calls, self_time, cumulative, _callers = values
    if filename not in {"~"} and not filename.startswith("<"):
        try:
            filename = Path(filename).relative_to(ROOT).as_posix()
        except ValueError:
            pass
    return {"file": filename, "line": line, "function": name, "primitive_calls": primitive,
            "calls": calls, "self_seconds": self_time, "cumulative_seconds": cumulative}


def profile_summary(raw):
    functions = [function_record(key, values) for key, values in raw.items()]
    self_ranked = sorted(functions, key=lambda row: row["self_seconds"], reverse=True)
    cumulative_ranked = sorted(functions, key=lambda row: row["cumulative_seconds"], reverse=True)
    def selected(definitions):
        return {label: function_record(function_key(function),
                                       raw.get(function_key(function), (0, 0, 0.0, 0.0, {})))
                for label, function in definitions.items()}
    phases = selected(PHASES)
    caller_wait = raw.get(function_key(Future.result), (0, 0, 0.0, 0.0, {}))
    return {
        "phases": phases, "nested_details": selected(DETAILS),
        "top_self": self_ranked[:30], "top_cumulative": cumulative_ranked[:30],
        "top_phase_bottlenecks": sorted(
            (dict(phase=name, **values) for name, values in phases.items()),
            key=lambda row: row["cumulative_seconds"], reverse=True)[:5],
        "functions": cumulative_ranked,
        "sampling_scope": {
            "enabled_inside_scan_worker": True,
            "worker_only_proven": False,
            "future_result_calls": caller_wait[1],
            "future_result_cumulative_seconds": caller_wait[3],
            "note": "This runtime can include caller Future.result waiting in cProfile statistics. "
                    "Enabling from the write thread does not prove thread-exclusive sampling. "
                    "Do not treat synchronization wait rows as an optimization bottleneck.",
        },
        "timing_note": "cProfile elapsed timings include profiler overhead and IO; not CPU utilization. "
                       "Phase entries are sequential scan stages. Nested details and cumulative rows overlap; "
                       "do not sum them as independent costs.",
    }


def run_profile(count=1000, total_bytes=MAX_BYTES, seed=20260917, timeout=180):
    validate_options(count, total_bytes, seed, timeout)
    # platform.platform() lazily imports socket/_socket in a fresh CLI process.
    # Complete harness host probing before snapshotting the loaded runtime.
    host = {"platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count()}
    before_source = source_fingerprint()
    before_runtime = runtime_fingerprint()
    report = {
        "report_version": 1, "app_version": __version__, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_root": str(ROOT), "scope": "one instrumented ApplicationService.call('scan') on generated files",
        "configuration": {"files": count, "source_bytes": total_bytes, "seed": seed, "timeout_seconds": timeout,
                          "monitoring": False, "index_session": False, "gui_subscribers": False},
        "host": host,
        "source_before": before_source, "runtime_before": before_runtime,
        "loaded_code_sha256": loaded_code_fingerprint(), "memory_before": physical_memory(),
    }
    profiler = cProfile.Profile()
    caller_id = threading.get_ident()
    worker = {"calls": 0}
    service = None
    scan_error = None
    with isolated_workspace() as workspace:
        report["temporary_workspace"] = str(workspace)
        root = workspace / "documents"
        generated_at = time.perf_counter()
        generate_corpus(root, count, total_bytes, seed)
        report["generation_seconds"] = time.perf_counter() - generated_at
        report["corpus_before"] = corpus_fingerprint(root)
        try:
            service = ApplicationService(workspace / "metadata", monitoring=False)
            # Deliberately avoid ApplicationService.add_library, whose automatic
            # scan would contaminate the one measured first-scan request.
            library = service.store.add_library(root, "Isolated profile corpus")
            original = service._perform_scan
            def profiled(*args, **kwargs):
                worker["calls"] += 1
                worker["id"] = threading.get_ident()
                worker["name"] = threading.current_thread().name
                started = time.perf_counter()
                profiler.enable()
                try:
                    return original(*args, **kwargs)
                finally:
                    profiler.disable()
                    worker["seconds"] = time.perf_counter() - started
            service._perform_scan = profiled
            started = time.perf_counter()
            try:
                report["scan_result"] = service.call("scan", {"library_id": library.id}, timeout=timeout)
            except Exception as error:
                scan_error = {"type": type(error).__name__, "code": getattr(error, "code", None)}
                # Cancellation bypasses the command queue. close() below waits
                # for native IO to safely finish; no active workspace is deleted.
                service.call("cancel_scan", timeout=2)
            report["caller_scan_seconds"] = time.perf_counter() - started
            if scan_error is None:
                coverage = service.call("query", {"mode": "name", "page_size": 1}, timeout=30)
                body = service.call("query", {"mode": "content", "text": ANCHOR, "page_size": 1}, timeout=30)
                report["verification"] = {
                    "documents": coverage.total, "covered": coverage.covered, "uncovered": coverage.uncovered,
                    "query_complete": coverage.complete, "body_anchor_hits": body.total, "body_complete": body.complete,
                }
                report["database_bytes"] = {path.name: path.stat().st_size
                                            for path in service.data_dir.glob("*.db")}
        finally:
            if service is not None:
                service.close()
        report["worker"] = worker
        report["profiled_off_caller_thread"] = worker.get("id") != caller_id
        report["corpus_after"] = corpus_fingerprint(root)
    report["workspace_cleaned"] = not Path(report["temporary_workspace"]).exists()
    raw = pstats.Stats(profiler).stats
    report["profile"] = profile_summary(raw)
    report["source_after"] = source_fingerprint()
    report["runtime_after"] = runtime_fingerprint()
    report["memory_after"] = physical_memory()
    report["source_unchanged"] = report["source_before"] == report["source_after"]
    report["runtime_unchanged"] = report["runtime_before"] == report["runtime_after"]
    report["documents_unchanged"] = report["corpus_before"] == report["corpus_after"]
    report["index_session_calls"] = report["profile"]["nested_details"]["index_session"]["calls"]
    verification = report.get("verification", {})
    complete = (scan_error is None and worker["calls"] == 1 and report["profiled_off_caller_thread"]
                and verification.get("documents") == count and verification.get("covered") == count
                and verification.get("body_anchor_hits") == count and verification.get("query_complete")
                and verification.get("body_complete") and report.get("scan_result", {}).get("failures") == 0
                and not report.get("scan_result", {}).get("cancelled"))
    report["measurement_valid"] = bool(complete and report["source_unchanged"] and report["runtime_unchanged"]
                                       and report["documents_unchanged"] and report["index_session_calls"] == 0)
    report["eligible_1000_20_baseline"] = report["measurement_valid"] and count == MAX_FILES and total_bytes == MAX_BYTES
    report["status"] = ("failed" if not complete else "complete" if report["measurement_valid"] else "incomparable")
    if scan_error:
        report["error"] = scan_error
    report["limitations"] = [
        "One instrumented sample only; no p95, GUI latency or unprofiled throughput claim.",
        "Corpus generation and verification warm filesystem caches; no cache flush or OS tuning.",
        "No watcher/GUI subscribers; notification calls use the no-listener path.",
        "Supported codec imports are warmed during runtime fingerprinting; their one-time import cost is excluded.",
        "cProfile may observe caller-thread Future.result waiting; thread-exclusive sampling is not claimed.",
        "The mixed fixed-vocabulary corpus does not represent every document collection.",
        "Do not extrapolate this 20 MiB sample to 500 MiB or larger libraries.",
        "Timeout uses cooperative cancellation; shutdown may wait for native IO, never delete active data.",
    ]
    return report, raw


def write_report(report, raw):
    artifacts = prepare_artifacts()
    stem = "profile-scan-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    profile_path = artifacts / (stem + ".prof")
    report_path = artifacts / (stem + ".json")
    with profile_path.open("xb") as stream:
        marshal.dump(raw, stream)
    report["raw_profile_path"] = str(profile_path)
    report["raw_profile_sha256"] = sha256_file(profile_path)
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--total-mib", type=float, default=20)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not math.isfinite(args.total_mib) or not 0 < args.total_mib <= 20:
        parser.error("total-mib must be a finite positive number no larger than 20")
    try:
        validate_options(args.files, int(args.total_mib * 1024**2), args.seed, args.timeout)
    except ValueError as error:
        parser.error(str(error))
    print(f"One isolated profiled service scan: {args.files} files / {args.total_mib:g} MiB", flush=True)
    report, raw = run_profile(args.files, int(args.total_mib * 1024**2), args.seed, args.timeout)
    path = write_report(report, raw)
    print(f"Status: {report['status']}; caller scan {report['caller_scan_seconds']:.3f}s", flush=True)
    print("Sampling is not proven thread-exclusive; Future.result calls: "
          f"{report['profile']['sampling_scope']['future_result_calls']}", flush=True)
    for row in report["profile"]["top_phase_bottlenecks"]:
        print(f"{row['phase']}: {row['cumulative_seconds']:.6f}s, {row['calls']} calls", flush=True)
    print(f"Report: {path}", flush=True)
    return 0 if report["measurement_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
