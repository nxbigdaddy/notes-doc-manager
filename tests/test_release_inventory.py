"""Inventory uses synthetic local packages; tests never download or load native images."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_inventory.py"
SPEC = importlib.util.spec_from_file_location("notes_release_inventory_test", SCRIPT)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    runtime = root / ".tools" / "python-test" / "tools"
    dlls = runtime / "DLLs"
    dlls.mkdir(parents=True)
    python_license = b"Synthetic PSF-like permission notice for a test fixture only.\n"
    (runtime / "LICENSE.txt").write_bytes(python_license)
    (runtime / "python314.dll").write_bytes(b"synthetic private runtime bytes")
    (dlls / "sqlite3.dll").write_bytes(b"synthetic sqlite bytes")
    venv = root / ".venv"
    site = venv / "Lib" / "site-packages"
    distribution = site / "demo-1.0.dist-info"
    distribution.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {runtime}\nversion = 3.14.7\n", encoding="utf-8")
    (site / "demo.pyd").write_bytes(b"synthetic extension bytes")
    demo_license = b"Synthetic MIT permission notice for a test fixture only.\r\n"
    (distribution / "LICENSE.txt").write_bytes(demo_license)
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nLicense: MIT\n", encoding="utf-8",
    )
    (distribution / "RECORD").write_text(
        "demo.pyd,,\ndemo-1.0.dist-info/METADATA,,\n"
        "demo-1.0.dist-info/RECORD,,\ndemo-1.0.dist-info/LICENSE.txt,,\n", encoding="utf-8",
    )
    package = root / "dist" / "Notes"
    internal = package / "_internal"
    internal.mkdir(parents=True)
    shutil.copyfile(runtime / "python314.dll", internal / "python314.dll")
    shutil.copyfile(dlls / "sqlite3.dll", internal / "sqlite3.dll")
    shutil.copyfile(site / "demo.pyd", internal / "demo.pyd")
    (internal / "unknown.dll").write_bytes(b"no proved source for this binary")
    (package / "Notes.exe").write_bytes(b"synthetic combined application")
    shutil.copytree(distribution, internal / distribution.name)
    tools = root / ".tools"
    write_json(tools / "python-runtime.json", {"version": "3.14.7", "url": "https://example.invalid/python"})
    write_json(tools / "sqlite-runtime.json", {
        "version": "3.53.4", "dll_sha256": checksum((dlls / "sqlite3.dll").read_bytes()),
        "url": "https://example.invalid/sqlite",
    })
    catalog = root / "packaging" / "third-party" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    write_json(catalog, {
        "format_version": 1,
        "components": [
            {"id": "cpython", "expected_version": "3.14.7",
             "license_declaration": "test declaration", "sources": [], "pending": []},
            {"id": "demo", "expected_version": "1.0",
             "license_declaration": "MIT metadata only until original is matched", "sources": [], "pending": []},
            {"id": "sqlite", "expected_version": "3.53.4",
             "license_declaration": "upstream declaration; local original absent", "sources": [], "pending": []},
        ],
        "approved_notice_copies": [
            {"component": "cpython", "source_root": "runtime", "relative_path": "LICENSE.txt",
             "sha256": checksum(python_license), "reason": "synthetic test approval"},
            {"component": "demo", "source_root": "venv",
             "relative_path": "Lib/site-packages/demo-1.0.dist-info/LICENSE.txt",
             "sha256": checksum(demo_license), "reason": "synthetic test approval"},
        ],
        "native_followups": ["Licensing completeness is not established."],
    })
    monkeypatch.setattr(inventory, "pe_metadata", lambda path: ({}, "synthetic image, not loaded"))
    return root, package, runtime, venv, catalog


def all_hashes(root):
    return {path.relative_to(root).as_posix(): checksum(path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def test_inventory_is_read_only_reproducible_and_copies_original_bytes(project, tmp_path):
    root, package, runtime, venv, _ = project
    before = all_hashes(root)
    output = tmp_path / "external-inventory"
    report = inventory.generate_inventory(root, package, output)
    assert all_hashes(root) == before
    assert (output / "licenses/cpython/LICENSE.txt").read_bytes() == (runtime / "LICENSE.txt").read_bytes()
    assert (output / "licenses/demo/LICENSE.txt").read_bytes() == (
        venv / "Lib/site-packages/demo-1.0.dist-info/LICENSE.txt"
    ).read_bytes()
    assert inventory.verify_inventory(package, output) == report
    second = tmp_path / "second-inventory"
    inventory.generate_inventory(root, package, second)
    assert all_hashes(output) == all_hashes(second)
    assert report["summary"]["native_files"] == 5
    assert report["summary"]["matched_native_files"] == 3
    assert not report["summary"]["legal_audit_complete"]
    assert any("Unmatched native artifact: _internal/unknown.dll" in item for item in report["pending"])
    components = {row["id"]: row for row in report["components"]}
    assert components["demo"]["license_evidence"] == "original_text_found"
    assert components["sqlite"]["license_evidence"] == "metadata_declaration_only"
    assert "not a legal audit" in (output / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")


def test_native_hashes_and_exact_sources_are_correct(project, tmp_path):
    root, package, runtime, _, _ = project
    report = inventory.generate_inventory(root, package, tmp_path / "out")
    native = {row["path"]: row for row in report["native_files"]}
    python = native["_internal/python314.dll"]
    assert python["sha256"] == checksum((runtime / "python314.dll").read_bytes())
    assert python["source_matches"][0]["component"] == "cpython"
    assert native["_internal/sqlite3.dll"]["source_matches"][0]["component"] == "sqlite"
    assert native["_internal/unknown.dll"]["source_matches"] == []
    assert all(row["notice_coverage"] == "not_established" for row in native.values())


def test_pe_metadata_key_order_is_stable_after_json_roundtrip(project, tmp_path, monkeypatch):
    root, package, _, _, _ = project
    monkeypatch.setattr(inventory, "pe_metadata", lambda path: (
        {"ProductVersion": "1.0", "FileDescription": "Synthetic", "ProductName": "Demo"}, "",
    ))
    output = tmp_path / "out"
    report = inventory.generate_inventory(root, package, output)
    assert inventory.verify_inventory(package, output) == report


def test_fresh_package_output_requires_explicit_option_and_is_read_only_on_reverify(project):
    root, package, _, _, _ = project
    output = package / "third-party"
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, output)
    report = inventory.generate_inventory(root, package, output, allow_package_output=True)
    before = all_hashes(package)
    assert inventory.verify_inventory(package) == report
    assert all_hashes(package) == before
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, output, allow_package_output=True)


def test_entire_package_verifies_after_copy_without_access_to_original_project(project, tmp_path, monkeypatch):
    root, package, _, _, _ = project
    report = inventory.generate_inventory(
        root, package, package / "third-party", allow_package_output=True,
    )
    copied = tmp_path / "isolated-location" / "Notes"
    shutil.copytree(package, copied)
    original_read = inventory.read_bytes
    def portable_read(path, **kwargs):
        assert not path.is_relative_to(root), "Verification must not read original runtime/venv/package"
        return original_read(path, **kwargs)
    monkeypatch.setattr(inventory, "read_bytes", portable_read)
    before = all_hashes(copied)
    assert inventory.verify_inventory(copied) == report
    assert all_hashes(copied) == before


@pytest.mark.parametrize("target", ["package", "package_other", "runtime", "venv", "project", "ancestor"])
def test_output_cannot_overlap_protected_input_trees(project, tmp_path, target):
    root, package, runtime, venv, _ = project
    output = {
        "package": package, "package_other": package / "unexpected",
        "runtime": runtime / "output", "venv": venv / "output",
        "project": root, "ancestor": tmp_path,
    }[target]
    before = all_hashes(root)
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, output, allow_package_output=True)
    assert all_hashes(root) == before


def test_verified_archived_package_refuses_embedded_output_even_with_opt_in(project):
    root, package, _, _, _ = project
    write_json(package / "BUILD-INFO.json", {"application": "archived"})
    before = all_hashes(package)
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, package / "third-party", allow_package_output=True)
    assert all_hashes(package) == before


@pytest.mark.parametrize("unsafe", ["../LICENSE.txt", "/LICENSE.txt", r"C:\outside\LICENSE.txt", "a/../LICENSE.txt"])
def test_catalog_paths_cannot_escape_declared_source_root(project, tmp_path, unsafe):
    root, package, _, _, catalog = project
    value = json.loads(catalog.read_text())
    value["approved_notice_copies"][0]["relative_path"] = unsafe
    write_json(catalog, value)
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unapproved_license_hash_is_recorded_pending_not_silently_copied(project, tmp_path):
    root, package, runtime, _, _ = project
    (runtime / "LICENSE.txt").write_text("different licensing text", encoding="utf-8")
    output = tmp_path / "out"
    report = inventory.generate_inventory(root, package, output)
    assert not (output / "licenses/cpython/LICENSE.txt").exists()
    assert any("cpython: version/hash differs" in item for item in report["pending"])
    assert inventory.verify_inventory(package, output)["summary"]["copied_notices"] == 1


def test_bundled_version_mismatch_does_not_copy_another_versions_notice(project, tmp_path):
    root, package, _, _, _ = project
    metadata = package / "_internal/demo-1.0.dist-info/METADATA"
    metadata.write_text("Metadata-Version: 2.1\nName: demo\nVersion: 2.0\nLicense: MIT\n")
    output = tmp_path / "out"
    report = inventory.generate_inventory(root, package, output)
    assert any("bundled version 2.0 differs" in item for item in report["pending"])
    assert not (output / "licenses/demo/LICENSE.txt").exists()
    assert inventory.verify_inventory(package, output)


def test_unknown_native_distribution_is_explicitly_pending(project, tmp_path):
    root, package, _, venv, _ = project
    site = venv / "Lib/site-packages"
    dist = site / "mystery-1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: mystery\nVersion: 1\n")
    (dist / "RECORD").write_text("mystery.pyd,,\nmystery-1.dist-info/METADATA,,\n")
    (site / "mystery.pyd").write_bytes(b"unknown owner")
    shutil.copyfile(site / "mystery.pyd", package / "_internal/mystery.pyd")
    report = inventory.generate_inventory(root, package, tmp_path / "out")
    unknown = next(row for row in report["installed_distributions"] if row["id"] == "mystery")
    assert unknown["license_evidence"] == "uncovered"
    assert any("Unreviewed native owner: mystery" in item for item in report["pending"])


@pytest.mark.parametrize("mutation", ["native_changed", "native_missing", "native_added",
                                      "notice_changed", "notice_missing", "notice_added", "nested_report"])
def test_read_only_verifier_rejects_unknown_missing_or_changed_files(project, tmp_path, mutation):
    root, package, _, _, _ = project
    output = tmp_path / "out"
    inventory.generate_inventory(root, package, output)
    if mutation == "native_changed":
        (package / "_internal/demo.pyd").write_bytes(b"changed")
    elif mutation == "native_missing":
        (package / "_internal/demo.pyd").unlink()
    elif mutation == "native_added":
        (package / "_internal/new.dll").write_bytes(b"unregistered native")
    elif mutation == "notice_changed":
        (output / "licenses/demo/LICENSE.txt").write_bytes(b"changed")
    elif mutation == "notice_missing":
        (output / "licenses/demo/LICENSE.txt").unlink()
    elif mutation == "notice_added":
        (output / "licenses/demo/EXTRA.txt").write_text("not declared")
    else:
        (output / "extra").mkdir()
        (output / "extra/release-inventory.json").write_text("{}")
    before_package, before_output = all_hashes(package), all_hashes(output)
    with pytest.raises((inventory.InventoryError, FileNotFoundError)):
        inventory.verify_inventory(package, output)
    assert all_hashes(package) == before_package and all_hashes(output) == before_output


@pytest.mark.parametrize("unsafe", ["../outside.dll", "/outside.dll", r"C:\outside.dll", "sub/CON"])
def test_verifier_rejects_unsafe_native_paths_before_reading_them(project, tmp_path, unsafe):
    root, package, _, _, _ = project
    output = tmp_path / "out"
    inventory.generate_inventory(root, package, output)
    report_path = output / "release-inventory.json"
    report = json.loads(report_path.read_text())
    report["native_files"][0]["path"] = unsafe
    write_json(report_path, report)
    with pytest.raises(inventory.InventoryError):
        inventory.verify_inventory(package, output)


def test_manifest_escaping_venv_is_not_probed(project, tmp_path):
    root, package, _, venv, _ = project
    record = venv / "Lib/site-packages/demo-1.0.dist-info/RECORD"
    record.write_text(record.read_text() + "../../../../outside.pyd,,\n")
    (package / "_internal/outside.pyd").write_bytes(b"request matching basename")
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction protection")
def test_release_or_output_junction_is_rejected(project, tmp_path):
    import _winapi
    root, package, _, _, _ = project
    alias = root / "release-alias"
    _winapi.CreateJunction(str(package), str(alias))
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, alias, tmp_path / "out")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    output_alias = root / "output-alias"
    _winapi.CreateJunction(str(outside), str(output_alias))
    with pytest.raises(inventory.InventoryError):
        inventory.generate_inventory(root, package, output_alias / "new")
    assert tuple(outside.iterdir()) == ()
