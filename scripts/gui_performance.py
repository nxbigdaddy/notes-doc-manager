"""Small isolated GUI responsiveness samples, not a release performance claim.

Default: 1000 short UTF-8 documents, one fresh GUI Python process and five more
frame/service starts in that same process. The filesystem cache is NOT flushed.
The child never shows a window or opens Notepad3. Corpus and databases are test
owned and deleted; only the JSON report remains. No user document roots, desktop
capture, modal UI, or application global-data directory is accessed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def memory_usage():
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    if os.name != "nt":
        return {"scope": "not measured on this platform"}
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"working_set_mib": round(counters.WorkingSetSize / 1024**2, 3),
            "peak_working_set_mib": round(counters.PeakWorkingSetSize / 1024**2, 3),
            "scope": "whole GUI benchmark Python child process, native wx + service + harness; "
                     "peak is process-lifetime, not per-window"}


def worker(data_dir, report_path, files, warm_runs):
    worker_started = time.perf_counter()
    import wx
    from notes import __version__
    from notes.application.service import ApplicationService
    from notes.domain.models import AppState
    from notes.ui.main_frame import MainFrame

    class CountedService(ApplicationService):
        def __init__(self, *args, **kwargs):
            self.command_counts = Counter()
            super().__init__(*args, **kwargs)

        def submit(self, command, *args, **kwargs):
            self.command_counts[command] += 1
            return super().submit(command, *args, **kwargs)

    audit = {"active": False, "io_events": []}
    main_thread = threading.get_ident()

    def audit_hook(event, args):
        if (audit["active"] and threading.get_ident() == main_thread
                and event in {"open", "os.listdir", "os.scandir", "sqlite3.connect",
                              "subprocess.Popen", "os.system"}):
            audit["io_events"].append(event)
            raise AssertionError(f"Virtual list callback attempted IO: {event}")
    sys.addaudithook(audit_hook)
    app = wx.App(False)
    samples = []
    service = frame = None
    try:
        for run in range(warm_runs + 1):
            errors = []
            baseline = {thread.ident for thread in threading.enumerate()}
            started = time.perf_counter()
            service = CountedService(data_dir, dispatcher=wx.CallAfter, monitoring=True)
            frame = MainFrame(service)
            frame._error = lambda error: errors.append(str(error))
            assert not frame.IsShown()
            dispatch = []
            pump_gaps = []
            last_pump = time.perf_counter()
            wx.CallAfter(lambda: dispatch.append(time.perf_counter()))

            def pump_until(predicate, timeout=30):
                nonlocal last_pump
                deadline = time.perf_counter() + timeout
                while time.perf_counter() < deadline:
                    now = time.perf_counter()
                    pump_gaps.append(now - last_pump)
                    last_pump = now
                    app.Yield()
                    if predicate():
                        return
                    time.sleep(.002)
                raise TimeoutError(f"GUI did not reach expected state; errors={errors}")

            pump_until(lambda: bool(dispatch) and frame.list.GetItemCount() == 100)
            ready = time.perf_counter()
            assert len(frame.list.hits) == 100
            sample = {
                "run": run,
                "kind": "fresh Python GUI process" if run == 0 else "same-process warm frame/service",
                "service_frame_to_dispatch_ready_s": round(dispatch[0] - started, 6),
                "service_frame_to_first_100_rows_s": round(ready - started, 6),
                "cached_rows": len(frame.list.hits),
                "visible": frame.IsShown(),
            }
            if run == 0:
                sample["worker_entry_to_ready_s"] = round(ready - worker_started, 6)
                print(json.dumps({"event": "first_page_ready"}), flush=True)
            frame._load_preview(frame.list.hits[0].document)
            preview_started = time.perf_counter()
            pump_until(lambda: frame._preview is not None)
            sample["preview_request_to_ready_s"] = round(time.perf_counter() - preview_started, 6)
            assert "GUI性能样例" in frame.preview_control.GetText()
            assert frame.preview_control.GetReadOnly()

            # Complete current startup scan before the pure-cache/audit section;
            # an independent background scan may legitimately perform IO.
            pump_until(lambda: not frame.state.busy)
            settle_until = time.perf_counter() + .15
            pump_until(lambda: time.perf_counter() >= settle_until)
            commands_before = sum(service.command_counts.values())
            cache_started = time.perf_counter()
            audit["active"] = True
            try:
                for _ in range(20):
                    for row in range(100):
                        for column in range(6):
                            frame.list.OnGetItemText(row, column)
            finally:
                audit["active"] = False
            sample["virtual_callback_12000_calls_s"] = round(time.perf_counter() - cache_started, 6)
            sample["virtual_callback_audited_io_events"] = list(audit["io_events"])
            sample["virtual_callback_service_submissions"] = (
                sum(service.command_counts.values()) - commands_before)
            assert sample["virtual_callback_service_submissions"] == 0

            # Same revision status chatter is deliberately not a refresh signal.
            query_before = service.command_counts["query"]
            stable = frame.state
            for number in range(100):
                frame._on_state(AppState(
                    libraries=stable.libraries, tags=stable.tags, editor_path=stable.editor_path,
                    status=f"测试状态 {number}", busy=False, data_revision=stable.data_revision,
                    backup_status=stable.backup_status, last_backup_at=stable.last_backup_at))
            sample["queries_from_100_same_revision_state_updates"] = (
                service.command_counts["query"] - query_before)
            assert sample["queries_from_100_same_revision_state_updates"] == 0
            sample["memory_while_frame_alive"] = memory_usage()
            sample["event_pump_max_gap_s"] = round(max(pump_gaps), 6)
            sample["event_pump_scope"] = (
                "manual wx event-pump intervals during ready/preview/startup-scan settle only; "
                "not input-to-render latency, no visible rendering")
            sample["command_counts_before_close"] = dict(service.command_counts)
            assert not errors
            close_started = time.perf_counter()
            frame.Close()
            pump_until(lambda: not frame)
            pump_until(lambda: not [thread for thread in threading.enumerate()
                                     if thread.ident not in baseline and thread.is_alive()], timeout=10)
            sample["ui_close_to_destroy_and_threads_joined_s"] = round(
                time.perf_counter() - close_started, 6)
            sample["new_threads_still_alive_after_close"] = []
            samples.append(sample)
            service = frame = None
        report = {
            "application": __version__, "python": sys.version.split()[0], "wx": wx.version(),
            "platform": platform.platform(), "samples": samples, "corpus_files": files,
            "warm_first_page_median_s": round(statistics.median(
                sample["service_frame_to_first_100_rows_s"] for sample in samples[1:]), 6)
                if warm_runs else None,
            "scope": "hidden developer Python GUI process, real ApplicationService with monitoring, "
                     "prepared short-document local corpus; no clean-machine or release acceptance",
            "cache_disclaimer": "fresh process is not cold OS disk cache; preparation warmed filesystem",
            "statistics_disclaimer": "raw samples + median only; no p95 estimate or SLA claim",
            "sample_threads_leaked": False,
            "final_process_memory": memory_usage(),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        audit["active"] = False
        if frame:
            frame._closing = True
        if service:
            service.close()
        if frame:
            frame.Destroy()
            app.Yield()
        app.Destroy()


def run(output, files=1000, warm_runs=5):
    if os.name != "nt":
        raise RuntimeError("This developer GUI benchmark requires Windows")
    if files < 100 or files > 10000 or not 0 <= warm_runs <= 20:
        raise ValueError("Expected 100..10000 files and 0..20 warm runs")
    from notes.application.service import ApplicationService

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = (ROOT / ".artifacts").resolve()
    artifact_root.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="gui-performance-", dir=artifact_root)
    sample_root = Path(temporary.name).resolve()
    if not sample_root.is_relative_to(artifact_root):
        raise RuntimeError("Unexpected temporary directory boundary")
    service = None
    process = None
    try:
        library = sample_root / "library"
        library.mkdir()
        total_bytes = 0
        for number in range(files):
            path = library / f"分组{number % 10:02d}" / f"样例{number:05d}.txt"
            path.parent.mkdir(exist_ok=True)
            text = (f"GUI性能样例 {number:05d}\n中文搜索与只读预览。😀\n"
                    "这只是隔离的短文档测试，不是真实用户资料。\n") * 3
            raw = text.encode("utf-8")
            path.write_bytes(raw)
            total_bytes += len(raw)
        prepare_started = time.perf_counter()
        service = ApplicationService(sample_root / "metadata", monitoring=False)
        lib = service.call("add_library", {"root_path": str(library), "name": "GUI性能测试"})
        service.call("scan", {"library_id": lib.id}, timeout=120)
        assert service.call("query").total == files
        service.close()
        service = None
        prepare_seconds = time.perf_counter() - prepare_started
        worker_report = sample_root / "worker-report.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--data-dir", str(sample_root / "metadata"),
                   "--output", str(worker_report), "--files", str(files),
                   "--warm-runs", str(warm_runs)]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        child_started = time.perf_counter()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", startupinfo=startup,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        # Reader observes the one structured readiness event, without polling or
        # taking screenshots. communicate enforces a bounded child lifetime.
        ready_latency = []
        stdout_lines = []

        def read_stdout():
            for line in process.stdout:
                stdout_lines.append(line)
                if '"event": "first_page_ready"' in line:
                    ready_latency.append(time.perf_counter() - child_started)
        reader = threading.Thread(target=read_stdout, name="gui-performance-output", daemon=True)
        reader.start()
        process.wait(timeout=180)
        reader.join(timeout=5)
        stderr = process.stderr.read()
        if process.returncode != 0:
            raise RuntimeError(f"GUI worker failed ({process.returncode}): {stderr}")
        if not ready_latency:
            raise RuntimeError("GUI worker did not report readiness")
        report = json.loads(worker_report.read_text(encoding="utf-8"))
        report.update({
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "fresh_process_launch_to_first_page_s": round(ready_latency[0], 6),
            "corpus_total_bytes": total_bytes,
            "corpus_preparation_and_index_s": round(prepare_seconds, 6),
            "corpus_preparation_scope": "separate controller Python process; excluded from GUI memory",
            "worker_stderr": stderr.strip(),
        })
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if service:
            service.close()
        if sample_root.resolve().is_relative_to(artifact_root) and sample_root != artifact_root:
            temporary.cleanup()
        else:
            raise RuntimeError("Unsafe temporary cleanup boundary")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts" /
                        "gui-performance-0.3.0.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.data_dir, args.output, args.files, args.warm_runs)
    else:
        result = run(args.output, args.files, args.warm_runs)
        print(json.dumps({"report": str(args.output), "samples": len(result["samples"]),
                          "fresh_process_launch_to_first_page_s":
                          result["fresh_process_launch_to_first_page_s"],
                          "warm_first_page_median_s": result["warm_first_page_median_s"],
                          "final_process_memory": result["final_process_memory"]},
                         ensure_ascii=False, indent=2))
