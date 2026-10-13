"""Small correctness samples only; the 1000-file baseline is an explicit CLI run."""
import importlib.util
import json
import os
from pathlib import Path
import pstats
import sqlite3
import subprocess
import sys
import tempfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "profile_scan.py"
SPEC = importlib.util.spec_from_file_location("notes_profile_scan_test_module", SCRIPT)
profile_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile_scan)


@pytest.mark.parametrize("count,total,seed,timeout", [
    (0, 1024, 1, 30), (1001, 1024, 1, 30), (True, 1024, 1, 30),
    (1, 127, 1, 30), (1, 21 * 1024 * 1024, 1, 30), (1, 128.0, 1, 30),
    (1, 128, "seed", 30), (1, 128, 1, float("nan")), (1, 128, 1, 301),
])
def test_hard_bounds_reject_large_or_invalid_workloads(count, total, seed, timeout):
    with pytest.raises(ValueError):
        profile_scan.validate_options(count, total, seed, timeout)


def test_workspace_is_owned_under_artifacts_and_is_cleaned():
    with profile_scan.isolated_workspace() as workspace:
        assert workspace.resolve().parent == profile_scan.ARTIFACTS.resolve()
        assert workspace.name.startswith("profile-scan-")
        (workspace / "owned.txt").write_text("fixture", encoding="utf-8")
    assert not workspace.exists()


def test_unverified_temp_target_is_never_cleaned(monkeypatch):
    class UnverifiedTemporary:
        name = str(profile_scan.ROOT / "not-a-profile-workspace")
        cleaned = False
        def cleanup(self):
            self.cleaned = True
            raise AssertionError("unverified target must not be recursively deleted")
    instance = UnverifiedTemporary()
    def fake_temp(**kwargs):
        assert kwargs["delete"] is False
        assert Path(kwargs["dir"]) == profile_scan.ARTIFACTS.resolve()
        return instance
    monkeypatch.setattr(profile_scan.tempfile, "TemporaryDirectory", fake_temp)
    with pytest.raises(ValueError):
        with profile_scan.isolated_workspace():
            pytest.fail("invalid workspace was yielded")
    assert not instance.cleaned


def test_validated_cleanup_order_uses_the_exact_created_directory(monkeypatch):
    real_factory = tempfile.TemporaryDirectory
    real_verify = profile_scan.verify_workspace
    events = []
    def factory(**kwargs):
        result = real_factory(**kwargs)
        cleanup = result.cleanup
        def checked_cleanup():
            assert events[-1] == ("verified", Path(result.name))
            events.append(("cleanup", Path(result.name)))
            cleanup()
        result.cleanup = checked_cleanup
        return result
    def verify(path, artifacts):
        result = real_verify(path, artifacts)
        events.append(("verified", Path(path)))
        return result
    monkeypatch.setattr(profile_scan.tempfile, "TemporaryDirectory", factory)
    monkeypatch.setattr(profile_scan, "verify_workspace", verify)
    with profile_scan.isolated_workspace() as workspace:
        (workspace / "owned.txt").write_text("fixture", encoding="utf-8")
    assert [event[0] for event in events] == ["verified", "verified", "cleanup"]
    assert not workspace.exists()


def test_corpus_is_deterministic_exact_utf8_and_contains_no_links():
    with profile_scan.isolated_workspace() as workspace:
        first, second = workspace / "one", workspace / "two"
        for target in (first, second):
            profile_scan.generate_corpus(target, 3, 4096, 20260917)
        expected = profile_scan.corpus_fingerprint(first)
        assert expected == profile_scan.corpus_fingerprint(second)
        assert expected["files"] == 3 and expected["bytes"] == 4096
        for path in first.rglob("*.txt"):
            body = path.read_bytes()
            assert profile_scan.ANCHOR in body.decode("utf-8")
            assert not path.is_symlink()


def test_real_service_scan_runs_once_on_writer_and_profiles_actual_stages():
    report, raw = profile_scan.run_profile(count=4, total_bytes=8192, seed=20260917, timeout=30)
    assert report["status"] == "complete", {
        "source_unchanged": report["source_unchanged"], "runtime_unchanged": report["runtime_unchanged"],
        "changed_runtime_fields": [key for key in report["runtime_before"]
                                   if report["runtime_before"][key] != report["runtime_after"][key]],
        "added_python_modules": sorted(set(report["runtime_after"]["loaded_python_source_sha256"])
                                       - set(report["runtime_before"]["loaded_python_source_sha256"])),
        "added_binaries": sorted(set(report["runtime_after"]["loaded_binary_sha256"])
                                - set(report["runtime_before"]["loaded_binary_sha256"])),
        "error": report.get("error"),
    }
    assert report["measurement_valid"] and not report["eligible_1000_20_baseline"]
    assert report["worker"]["calls"] == 1
    assert report["worker"]["name"].startswith("notes-write")
    assert report["profiled_off_caller_thread"] and report["workspace_cleaned"]
    assert report["index_session_calls"] == 0 and report["configuration"]["index_session"] is False
    assert report["profile"]["sampling_scope"]["worker_only_proven"] is False
    assert report["scan_result"]["discovered"] == report["scan_result"]["indexed"] == 4
    assert report["verification"]["covered"] == report["verification"]["body_anchor_hits"] == 4
    assert report["documents_unchanged"]
    phases = report["profile"]["phases"]
    assert phases["filesystem_enumeration"]["calls"] == 1
    assert phases["metadata_registration"]["calls"] == 1
    assert phases["read_and_validate"]["calls"] == 4
    assert phases["index_batch_commit"]["calls"] == 1
    assert report["source_before"]["source_sha256"]["src/notes/application/service.py"]
    assert report["runtime_before"]["sqlite"] == sqlite3.sqlite_version
    path = profile_scan.write_report(report, raw)
    native = Path(report["raw_profile_path"])
    try:
        assert path.parent == native.parent == profile_scan.ARTIFACTS.resolve()
        assert pstats.Stats(str(native)).stats
        assert report["raw_profile_sha256"] == profile_scan.sha256_file(native)
    finally:
        # Exact new report files only; never enumerate/delete prior artifacts.
        path.unlink()
        native.unlink()


def test_source_change_invalidates_measurement_instead_of_retrying(monkeypatch):
    original = profile_scan.source_fingerprint
    calls = 0
    def changing():
        nonlocal calls
        calls += 1
        result = original()
        if calls == 2:
            result["aggregate_sha256"] = "simulated changed source"
        return result
    monkeypatch.setattr(profile_scan, "source_fingerprint", changing)
    report, _ = profile_scan.run_profile(count=1, total_bytes=512, timeout=30)
    assert report["status"] == "incomparable"
    assert report["worker"]["calls"] == 1 and calls == 2
    assert not report["measurement_valid"] and not report["eligible_1000_20_baseline"]
    assert report["workspace_cleaned"]


def test_fresh_tiny_cli_has_stable_runtime_and_reports_sampling_scope():
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--files", "1", "--total-mib", str(1024 / 1024**2), "--timeout", "30"],
        cwd=profile_scan.ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    report_line = next((line for line in completed.stdout.splitlines() if line.startswith("Report: ")), None)
    assert report_line, (completed.returncode, completed.stdout, completed.stderr)
    report_path = Path(report_line.removeprefix("Report: "))
    assert report_path.parent == profile_scan.ARTIFACTS.resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    raw = Path(report["raw_profile_path"])
    assert raw.parent == profile_scan.ARTIFACTS.resolve()
    try:
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        assert report["status"] == "complete" and report["runtime_unchanged"]
        assert report["source_unchanged"] and report["documents_unchanged"]
        assert report["worker"]["calls"] == 1 and report["index_session_calls"] == 0
        assert report["configuration"]["files"] == 1 and report["configuration"]["source_bytes"] == 1024
        assert not report["eligible_1000_20_baseline"]
        sampling = report["profile"]["sampling_scope"]
        assert sampling["worker_only_proven"] is False
        assert type(sampling["future_result_calls"]) is int
        assert "not proven thread-exclusive" in completed.stdout
        assert report["workspace_cleaned"]
    finally:
        report_path.unlink()
        raw.unlink()
