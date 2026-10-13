"""Release staging must not borrow stale files from a previous verification."""

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def verifier_module():
    source = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("notes_release_verifier_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arrange(tmp_path, monkeypatch):
    module = verifier_module()
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "verify_release.py"))
    version = module.__version__
    package = tmp_path / "dist" / version / "Notes"
    package.mkdir(parents=True)
    (package / "Notes.exe").write_bytes(b"synthetic-executable-never-run")
    (package / "sqlite3.dll").write_bytes(b"synthetic-sqlite-never-load")
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("# Fixture", encoding="utf-8")
    inventory = {"summary": {"legal_audit_complete": False}, "pending": ["synthetic pending review"]}

    def generate(_root, _release, output, **_kwargs):
        output.mkdir()
        (output / "release-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        return inventory

    def verify(release):
        return json.loads((release / "third-party" / "release-inventory.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(module, "generate_inventory", generate)
    monkeypatch.setattr(module, "verify_inventory", verify)
    return module, version


def successful_report(version):
    return {
        "application": version, "frozen": True, "fts5_trigram": True, "wx_stc": True,
        "sqlite_wal_reset_fixed": True,
        "build_provenance": {
            "version": version, "source_sha256": {},
            "private_runtime": {"sqlite-runtime.json": {
                "dll_sha256": hashlib.sha256(b"synthetic-sqlite-never-load").hexdigest()
            }},
        },
        "self_test": {"fixture": True}, "gui_smoke": True, "quick_smoke": True,
    }


def test_every_verification_uses_empty_staging_and_preserves_versioned_record(tmp_path, monkeypatch):
    module, version = arrange(tmp_path, monkeypatch)
    seen = []

    def run(arguments, **kwargs):
        directory = Path(kwargs["cwd"])
        seen.append(directory)
        assert not (directory / "Notes" / "stale.dll").exists()
        assert "VIRTUAL_ENV" not in kwargs["env"]
        assert all(not key.upper().startswith("PYTHON") for key in kwargs["env"])
        report = Path(arguments[-1])
        report.write_text(json.dumps(successful_report(version)), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    (seen[0] / "Notes" / "stale.dll").write_bytes(b"old dependency")
    module.main()
    assert seen[0] == seen[1] == seen[2]
    assert seen[3] == seen[4] == seen[5] and seen[0] != seen[3]
    manifest = tmp_path / ".artifacts" / "release-records" / version / "release-verification.json"
    assert json.loads(manifest.read_text())["reports"]["selftest"]["application"] == version


def test_stale_executable_cannot_publish_current_source_manifest(tmp_path, monkeypatch):
    module, version = arrange(tmp_path, monkeypatch)

    def run(arguments, **kwargs):
        report = successful_report(version)
        report["application"] = "stale"
        Path(arguments[-1]).write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(AssertionError, match="version is stale"):
        module.main()
    assert not (tmp_path / ".artifacts" / "release-verification.json").exists()


def test_standalone_guide_has_no_missing_repository_links(tmp_path):
    module = verifier_module()
    module.write_user_guide(tmp_path, module.__version__)
    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Notes.exe" in text and "BUILD-INFO.json" in text
    assert "备份不包含正文" in text
    assert "](docs/" not in text and ".venv" not in text
    assert "third-party" in text and "不等于法律审核" in text


def test_changed_previously_verified_inventory_cannot_be_silently_republished(tmp_path, monkeypatch):
    module, version = arrange(tmp_path, monkeypatch)

    def run(arguments, **kwargs):
        Path(arguments[-1]).write_text(json.dumps(successful_report(version)), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    package = tmp_path / "dist" / version / "Notes"
    recorded = (package / "BUILD-INFO.json").read_bytes()
    (package / "third-party" / "release-inventory.json").write_text(
        json.dumps({"summary": {}, "pending": []}), encoding="utf-8")
    with pytest.raises(AssertionError, match="third-party"):
        module.main()
    assert (package / "BUILD-INFO.json").read_bytes() == recorded
