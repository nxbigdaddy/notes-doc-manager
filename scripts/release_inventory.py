"""Offline, read-only release/dependency evidence; writes only a new output directory."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from email import message_from_bytes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

NATIVE_SUFFIXES = {".dll", ".pyd", ".exe"}
NOTICE_NAME = re.compile(r"^(?:licen[cs]e|copying|copyright|notice)(?:[._-].*)?$", re.I)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REPORT_KEYS = {
    "format_version", "boundary", "inputs", "runtime_provenance", "release_build_evidence",
    "pe_self_reported_component_versions", "bundled_distribution_metadata", "components",
    "installed_distributions", "native_files", "installed_bootloader_candidates",
    "notice_files_found", "pending", "summary", "output_files",
}
DISCLAIMER = (
    "Technical evidence inventory only: not a legal audit, exhaustive dependency/SBOM analysis, "
    "or approval for commercial/public redistribution. Finding license text or matching bytes "
    "does not establish that every licensing obligation is satisfied."
)
MAX_FILES = 100_000
MAX_FILE_BYTES = 512 * 1024 * 1024


class InventoryError(ValueError):
    pass


def absolute(path):
    value = os.fspath(path)
    if "\x00" in value or value.startswith(("\\\\", "//")):
        raise InventoryError("Network/device paths are not accepted")
    return Path(os.path.abspath(value))


def plain_chain(path, *, missing=False):
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                return
            raise InventoryError("Required input does not exist") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InventoryError("Symlinks and reparse points are not accepted")


def bounded(path, root, *, missing=False):
    path = absolute(path)
    if not path.is_relative_to(root):
        raise InventoryError("Path escaped its declared input root")
    plain_chain(path, missing=missing)
    return path


def read_bytes(path, *, limit=MAX_FILE_BYTES):
    plain_chain(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise InventoryError("Input must be a bounded, ordinary, single-link file")
    value = path.read_bytes()
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if len(value) != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
        raise InventoryError("Input changed while being read")
    return value


def sha256(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def walk_files(root):
    plain_chain(root)
    directories = [root]
    count = 0
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                count += 1
                if count > MAX_FILES:
                    raise InventoryError("Inventory file count limit exceeded")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise InventoryError("Input tree contains a link/reparse point")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    directories.append(path)
                elif stat.S_ISREG(info.st_mode):
                    yield path
                else:
                    raise InventoryError("Input tree contains a nonregular object")


def relative(path, root):
    return path.relative_to(root).as_posix()


def safe_relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or any(not part or part in (".", "..") or part.endswith((" ", "."))
                   or any(ord(char) < 32 or char in ':*?"<>|' for char in part)
                   for part in value.split("/"))):
        raise InventoryError("Unsafe relative artifact path")
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
               *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
    if any(part.split(".")[0].upper() in devices for part in value.split("/")):
        raise InventoryError("Reserved device path in artifact")
    return value


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InventoryError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(read_bytes(path, limit=4 * 1024 * 1024).decode("utf-8"),
                          object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError):
        raise InventoryError("Invalid UTF-8 JSON input") from None


def pe_metadata(path):
    """Optional data-only parser; never load or execute the inspected native image."""
    try:
        import pefile
    except ImportError:
        return {}, "PE resource parser unavailable"
    try:
        image = pefile.PE(str(path), fast_load=True)
        try:
            image.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
            values = {}
            for entries in getattr(image, "FileInfo", []):
                for entry in entries:
                    for table in getattr(entry, "StringTable", []):
                        for key, value in table.entries.items():
                            key = key.decode("utf-8", errors="replace")
                            if key in {"ProductName", "ProductVersion", "FileDescription",
                                       "FileVersion", "LegalCopyright"}:
                                values[key] = value.decode("utf-8", errors="replace")
            return values, ""
        finally:
            image.close()
    except (pefile.PEFormatError, OSError, ValueError, KeyError):
        return {}, "PE resource metadata unavailable; no vendor/version inferred"


def _runtime_root(project, venv):
    config = bounded(venv / "pyvenv.cfg", venv)
    values = {}
    for line in read_bytes(config, limit=64 * 1024).decode("utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if not values.get("home"):
        raise InventoryError("venv must declare its private runtime home")
    return bounded(values["home"], project), values


def _catalog_path(item, roots):
    name = item.get("relative_path")
    if (not isinstance(name, str) or "\\" in name or not name
            or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise InventoryError("Unsafe catalog notice path")
    key = item.get("source_root")
    if key not in roots:
        raise InventoryError("Unknown catalog source root")
    return bounded(roots[key].joinpath(*name.split("/")), roots[key], missing=True)


def _metadata_inventory(project, venv, native_names, notices, sources):
    site_packages = bounded(venv / "Lib" / "site-packages", venv)
    installed = []
    # Parse RECORD ourselves: Distribution.files may probe every declared path
    # before the caller can reject traversal outside the venv.
    directories = sorted(
        (path for path in site_packages.iterdir() if path.name.endswith(".dist-info")),
        key=lambda path: path.name.lower(),
    )
    for directory in directories:
        bounded(directory, venv)
        metadata_path = bounded(directory / "METADATA", venv)
        metadata = message_from_bytes(read_bytes(metadata_path, limit=4 * 1024 * 1024))
        name = metadata.get("Name", "")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise InventoryError("Distribution has an invalid name")
        identifier = re.sub(r"[-_.]+", "-", name).lower()
        record = {
            "id": identifier, "name": name, "version": metadata.get("Version", ""),
            "scope": "installed_environment; presence alone does not prove bundling",
            "license_declaration": (metadata.get("License-Expression") or metadata.get("License") or ""),
            "project_urls": list(metadata.get_all("Project-URL") or []),
            "license_files": [], "metadata_files": [],
        }
        record_path = bounded(directory / "RECORD", venv, missing=True)
        members = [relative(metadata_path, site_packages)]
        if record_path.exists():
            rows = csv.reader(io.StringIO(read_bytes(record_path, limit=8 * 1024 * 1024).decode("utf-8")))
            members.extend(row[0] for row in rows if row)
        if len(members) > MAX_FILES:
            raise InventoryError("Distribution RECORD is too large")
        for member in dict.fromkeys(members):
            member_name = Path(member).name
            if (not NOTICE_NAME.fullmatch(member_name) and member_name not in ("METADATA", "RECORD")
                    and member_name.casefold() not in native_names):
                continue
            path = bounded(site_packages / member, venv)
            if not path.is_file():
                continue
            if NOTICE_NAME.fullmatch(member_name):
                evidence = {"source": relative(path, project), "sha256": sha256(path),
                            "bytes": path.stat().st_size, "component": identifier}
                notices[str(path)] = evidence
                record["license_files"].append(evidence)
            elif member_name in ("METADATA", "RECORD"):
                record["metadata_files"].append({"source": relative(path, project), "sha256": sha256(path)})
            if member_name.casefold() in native_names and path.suffix.lower() in NATIVE_SUFFIXES:
                sources[sha256(path)].append(
                    {"component": identifier, "source": relative(path, project),
                     "attribution": "installed distribution RECORD membership and exact SHA256"})
        record["license_evidence"] = (
            "original_text_found" if record["license_files"] else
            "metadata_declaration_only" if record["license_declaration"] else "uncovered")
        installed.append(record)
    return installed


def build_inventory(project_root, release_dir, *, venv_dir=None, catalog_path=None):
    """Read inputs and return (report, approved_copy_bytes), without writing any files."""
    project = absolute(project_root)
    plain_chain(project)
    release = bounded(release_dir, project)
    venv = bounded(venv_dir or project / ".venv", project)
    runtime, config = _runtime_root(project, venv)
    catalog_file = bounded(catalog_path or project / "packaging/third-party/catalog.json", project)
    catalog = read_json(catalog_file)
    if catalog.get("format_version") != 1:
        raise InventoryError("Unsupported notice catalog")
    components = {}
    for item in catalog.get("components", []):
        identifier = item.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in components:
            raise InventoryError("Invalid or duplicate catalog component")
        components[identifier] = dict(item, notice_files=[])
    release_paths = list(walk_files(release))
    native_paths = [path for path in release_paths if path.suffix.lower() in NATIVE_SUFFIXES]
    names = {path.name.casefold() for path in native_paths}
    notices, sources = {}, defaultdict(list)
    installed = _metadata_inventory(project, venv, names, notices, sources)
    versions = {record["id"]: record["version"] for record in installed}
    versions["cpython"] = config.get("version", "")
    bundled_metadata = []
    release_versions = {}
    for path in release_paths:
        if path.name != "METADATA" or not path.parent.name.endswith(".dist-info"):
            continue
        data = read_bytes(path, limit=4 * 1024 * 1024)
        metadata = message_from_bytes(data)
        name, version = metadata.get("Name", ""), metadata.get("Version", "")
        if not IDENTIFIER.fullmatch(name):
            raise InventoryError("Bundled distribution metadata has an invalid name")
        identifier = re.sub(r"[-_.]+", "-", name).lower()
        if identifier in release_versions and release_versions[identifier] != version:
            raise InventoryError("Conflicting bundled dependency versions")
        release_versions[identifier] = version
        bundled_metadata.append({
            "id": identifier, "version": version, "source": relative(path, release),
            "sha256": hashlib.sha256(data).hexdigest(),
            "license_declaration": metadata.get("License-Expression") or metadata.get("License") or "",
        })
    build_evidence = {"available": False, "executable_hash_matches": False}
    build_path = release / "BUILD-INFO.json"
    if build_path.exists():
        build_info = read_json(build_path)
        executable = release / "Notes.exe"
        matches = executable.is_file() and build_info.get("executable_sha256") == sha256(executable)
        build_evidence = {"available": True, "source": "BUILD-INFO.json",
                          "sha256": sha256(build_path), "executable_hash_matches": matches}
        if matches:
            reported = build_info.get("reports", {}).get("selftest", {})
            for name, key in (("cpython", "python"), ("sqlite", "sqlite")):
                if reported.get(key):
                    release_versions[name] = reported[key]
            build_evidence["reported_versions"] = {
                key: reported.get(key) for key in ("application", "python", "sqlite", "wx")
            }
    provenance = {}
    for filename in ("python-runtime.json", "sqlite-runtime.json"):
        path = bounded(project / ".tools" / filename, project, missing=True)
        if path.exists():
            provenance[filename] = {"source": relative(path, project), "sha256": sha256(path),
                                    "value": read_json(path)}
    sqlite_manifest = provenance.get("sqlite-runtime.json", {}).get("value", {})
    versions["sqlite"] = sqlite_manifest.get("version", "")
    native_metadata = {path: pe_metadata(path) for path in native_paths}
    pe_versions = defaultdict(set)
    for fields, _ in native_metadata.values():
        name = fields.get("ProductName", "")
        component = {"Python": "cpython", "SQLite": "sqlite", "wxWidgets": "wxwidgets"}.get(name)
        if component and fields.get("ProductVersion"):
            pe_versions[component].add(fields["ProductVersion"])
    for component, observed in pe_versions.items():
        if len(observed) == 1 and component not in release_versions:
            release_versions[component] = next(iter(observed))
    versions["wxwidgets"] = release_versions.get("wxwidgets", "")
    for path in walk_files(runtime):
        if path.suffix.lower() in NATIVE_SUFFIXES and path.name.casefold() in names:
            digest = sha256(path)
            owner = ("sqlite" if path.name.lower() == "sqlite3.dll"
                     and digest == sqlite_manifest.get("dll_sha256") else "cpython")
            sources[digest].append({"component": owner, "source": relative(path, project),
                                    "attribution": "exact private-runtime SHA256; not a transitive-license claim"})
    copies = {}
    pending = list(catalog.get("native_followups", []))
    if build_evidence["available"] and not build_evidence["executable_hash_matches"]:
        pending.append("BUILD-INFO.json does not match Notes.exe; its reported versions were not used")
    for identifier, version in release_versions.items():
        if versions.get(identifier) and versions[identifier] != version:
            pending.append(f"{identifier}: bundled version {version} differs from installed version {versions[identifier]}")
    for item in catalog.get("approved_notice_copies", []):
        identifier = item.get("component")
        if identifier not in components or not HEX_SHA256.fullmatch(item.get("sha256", "")):
            raise InventoryError("Invalid notice-copy approval")
        path = _catalog_path(item, {"runtime": runtime, "venv": venv, "project": project})
        if not path.exists():
            pending.append(f"{identifier}: approved local notice is missing")
            continue
        data = read_bytes(path, limit=2 * 1024 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        evidence = {"source": relative(path, project), "sha256": digest, "bytes": len(data),
                    "component": identifier, "copied_as": None}
        notices[str(path)] = evidence
        if (digest == item["sha256"]
                and versions.get(identifier) == components[identifier].get("expected_version")
                and release_versions.get(identifier, versions.get(identifier)) == versions.get(identifier)):
            destination = f"licenses/{identifier}/{path.name}"
            safe_relative(destination)
            if (not NOTICE_NAME.fullmatch(path.name)
                    or path.suffix.lower() in NATIVE_SUFFIXES or b"\x00" in data):
                raise InventoryError("Approved notice is not a plain license text artifact")
            data.decode("utf-8", errors="strict")
            if destination in copies and copies[destination] != data:
                raise InventoryError("Conflicting notice-copy destination")
            copies[destination] = data
            evidence["copied_as"] = destination
            evidence["copy_basis"] = item.get("reason", "")
        else:
            pending.append(f"{identifier}: version/hash differs from approved notice; no automatic copy")
        components[identifier]["notice_files"].append(evidence)
    for identifier, item in components.items():
        item["observed_version"] = versions.get(identifier, "")
        item["release_reported_version"] = release_versions.get(identifier)
        item["version_scope"] = "installed environment; release version recorded separately when evidenced"
        item["license_evidence"] = (
            "original_text_found" if item["notice_files"] else
            "metadata_declaration_only" if item.get("license_declaration") else "uncovered")
        item["redistribution_assessment"] = "not_assessed"
        pending.extend(f"{identifier}: {message}" for message in item.get("pending", []))
    native = []
    for path in sorted(native_paths):
        digest = sha256(path)
        fields, warning = native_metadata[path]
        matches = sorted(sources.get(digest, []), key=lambda item: item["source"])
        native.append({"path": relative(path, release), "sha256": digest, "bytes": path.stat().st_size,
                       "pe_self_reported": fields, "source_matches": matches,
                       "notice_coverage": "not_established", "parser_warning": warning})
        if not matches:
            pending.append(f"Unmatched native artifact: {relative(path, release)}")
        for match in matches:
            if match["component"] not in components:
                pending.append(f"Unreviewed native owner: {match['component']}: {relative(path, release)}")
    # Bootloader provenance is separate: the combined EXE necessarily differs.
    bootloaders = []
    directory = bounded(venv / "Lib/site-packages/PyInstaller/bootloader", venv, missing=True)
    if directory.exists():
        for path in walk_files(directory):
            if path.name.lower() in ("runw.exe", "run.exe"):
                bootloaders.append({"source": relative(path, project), "sha256": sha256(path),
                                    "version": versions.get("pyinstaller", ""),
                                    "bundled_executable_match": "not_proved"})
    report = {
        "format_version": 1, "boundary": DISCLAIMER,
        "inputs": {"release": relative(release, project), "venv": relative(venv, project),
                   "runtime": relative(runtime, project), "catalog": relative(catalog_file, project),
                   "catalog_sha256": sha256(catalog_file)},
        "runtime_provenance": provenance,
        "release_build_evidence": build_evidence,
        "pe_self_reported_component_versions": {key: sorted(value) for key, value in pe_versions.items()},
        "bundled_distribution_metadata": sorted(bundled_metadata, key=lambda item: item["id"]),
        "components": sorted(components.values(), key=lambda item: item["id"]),
        "installed_distributions": installed,
        "native_files": native, "installed_bootloader_candidates": sorted(bootloaders, key=lambda item: item["source"]),
        "notice_files_found": sorted(notices.values(), key=lambda item: item["source"]),
        "pending": sorted(set(pending)),
        "summary": {"native_files": len(native), "matched_native_files": sum(bool(row["source_matches"]) for row in native),
                    "copied_notices": len(copies), "pending_items": len(set(pending)),
                    "legal_audit_complete": False},
    }
    return report, copies


def render_notices(report):
    lines = ["THIRD-PARTY TECHNICAL NOTICES", "=============================", "",
             DISCLAIMER, "", "Evidence levels: original_text_found / metadata_declaration_only / uncovered.",
             "A level describes observed evidence, not completeness or legal approval.", ""]
    for item in report["components"]:
        lines.extend([f"{item['id']} {item['observed_version'] or '(version unresolved)'}",
                      f"  Release-reported version: {item.get('release_reported_version') or 'NOT ESTABLISHED'}",
                      "  Above observed version belongs to the installed build environment.",
                      f"  Evidence: {item['license_evidence']}",
                      f"  Declaration: {item.get('license_declaration', '')}"])
        for source in item.get("sources", []):
            lines.append(f"  Upstream reference (not downloaded): {source}")
        for notice in item["notice_files"]:
            lines.extend([f"  Local original: {notice['source']}",
                          f"  SHA256: {notice['sha256']}",
                          f"  Verbatim copy: {notice.get('copied_as') or 'NOT COPIED; REVIEW PENDING'}"])
        lines.append("")
    lines.extend(["NATIVE FILE INVENTORY", "---------------------"])
    for item in report["native_files"]:
        matches = ", ".join(row["source"] for row in item["source_matches"]) or "NO VERIFIED SOURCE MATCH"
        lines.extend([item["path"], f"  SHA256: {item['sha256']}", f"  Identical local bytes: {matches}",
                      f"  PE self-report (not licensing permission): {json.dumps(item['pe_self_reported'], ensure_ascii=False, sort_keys=True)}",
                      "  Complete notice coverage: NOT ESTABLISHED"])
    lines.extend(["", "OPEN ITEMS / LIMITATIONS", "------------------------"])
    lines.extend("- " + message for message in report["pending"])
    lines.extend(["", "No inspected binaries or dependency applications were executed; no downloads were made.",
                  "Only exact, locally approved notice originals are copied. Runtime inputs remain unchanged.", ""])
    return "\n".join(lines)


def generate_inventory(project_root, release_dir, output_dir, *, venv_dir=None, catalog_path=None,
                       allow_package_output=False):
    project = absolute(project_root)
    output = absolute(output_dir)
    plain_chain(output, missing=True)
    release = bounded(release_dir, project)
    inputs = [release, bounded(venv_dir or project / ".venv", project),
              bounded(project / ".tools", project)]
    if output == project or project.is_relative_to(output):
        raise InventoryError("Output must not replace the project or an ancestor")
    for source in inputs:
        if output.is_relative_to(source) or source.is_relative_to(output):
            if (source == release and allow_package_output is True
                    and output == release / "third-party"
                    and not (release / "BUILD-INFO.json").exists()):
                continue
            raise InventoryError("Output overlaps a read-only release/runtime/venv input")
    if output.exists():
        raise InventoryError("Output directory must be new; existing output is never overwritten")
    report, copies = build_inventory(project, release_dir, venv_dir=venv_dir, catalog_path=catalog_path)
    output.mkdir(parents=True, exist_ok=False)
    plain_chain(output)
    outputs = {"THIRD-PARTY-NOTICES.txt": render_notices(report).encode("utf-8"), **copies}
    report["output_files"] = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in sorted(outputs.items())
    }
    outputs["release-inventory.json"] = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    for name, data in outputs.items():
        destination = bounded(output / name, output, missing=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        plain_chain(destination.parent)
        with destination.open("xb") as stream:
            stream.write(data)
    return report


def verify_inventory(release_dir, output_dir=None):
    """Read-only self-consistency verification; a trusted external hash/signature is separate."""
    release = absolute(release_dir)
    plain_chain(release)
    output = absolute(output_dir or release / "third-party")
    plain_chain(output)
    report = read_json(output / "release-inventory.json")
    if (not isinstance(report, dict) or set(report) != REPORT_KEYS
            or type(report["format_version"]) is not int or report["format_version"] != 1
            or not isinstance(report["native_files"], list) or len(report["native_files"]) > MAX_FILES
            or not isinstance(report["output_files"], dict)
            or not isinstance(report["summary"], dict)
            or report["summary"].get("legal_audit_complete") is not False
            or not isinstance(report["pending"], list)
            or not all(isinstance(item, str) for item in report["pending"])
            or not isinstance(report["components"], list)):
        raise InventoryError("Invalid or unsupported inventory schema")
    inputs = report["inputs"]
    if (not isinstance(inputs, dict)
            or set(inputs) != {"release", "venv", "runtime", "catalog", "catalog_sha256"}):
        raise InventoryError("Invalid inventory input references")
    for key in ("release", "venv", "runtime", "catalog"):
        safe_relative(inputs[key])
    # Source references are provenance only. Check their syntax but never probe
    # the original build machine while verifying a relocated package.
    source_groups = [
        report["notice_files_found"], report["installed_bootloader_candidates"],
        report["bundled_distribution_metadata"], list(report["runtime_provenance"].values()),
    ]
    for component in report["components"]:
        source_groups.append(component["notice_files"])
    for distribution in report["installed_distributions"]:
        source_groups.extend((distribution["license_files"], distribution["metadata_files"]))
    for native in report["native_files"]:
        source_groups.append(native["source_matches"])
    for group in source_groups:
        if not isinstance(group, list):
            raise InventoryError("Invalid inventory source group")
        for entry in group:
            if not isinstance(entry, dict):
                raise InventoryError("Invalid inventory source reference")
            safe_relative(entry.get("source"))

    def verify_file(path, descriptor):
        if (not isinstance(descriptor, dict) or not isinstance(descriptor.get("sha256"), str)
                or not HEX_SHA256.fullmatch(descriptor["sha256"])
                or type(descriptor.get("bytes")) is not int or not 0 <= descriptor["bytes"] <= MAX_FILE_BYTES):
            raise InventoryError("Invalid artifact fingerprint")
        data = read_bytes(path)
        if len(data) != descriptor["bytes"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise InventoryError("Inventory artifact size/hash mismatch")

    native_entries = {}
    for item in report["native_files"]:
        if not isinstance(item, dict):
            raise InventoryError("Invalid native-file entry")
        name = safe_relative(item.get("path"))
        key = name.casefold() if os.name == "nt" else name
        if key in native_entries or Path(name).suffix.lower() not in NATIVE_SUFFIXES:
            raise InventoryError("Duplicate or nonnative inventory entry")
        native_entries[key] = name
        verify_file(bounded(release / name, release), item)
    actual_native = {
        relative(path, release).casefold() if os.name == "nt" else relative(path, release)
        for path in walk_files(release) if path.suffix.lower() in NATIVE_SUFFIXES
    }
    if actual_native != set(native_entries):
        raise InventoryError("Native file set differs: missing or unregistered dependency")
    expected_outputs = {}
    for name, descriptor in report["output_files"].items():
        safe_relative(name)
        if name != "THIRD-PARTY-NOTICES.txt" and not name.startswith("licenses/"):
            raise InventoryError("Unknown notice artifact")
        key = name.casefold() if os.name == "nt" else name
        if key in expected_outputs:
            raise InventoryError("Duplicate notice artifact")
        expected_outputs[key] = name
        verify_file(bounded(output / name, output), descriptor)
    actual_outputs = {
        relative(path, output).casefold() if os.name == "nt" else relative(path, output)
        for path in walk_files(output) if path != output / "release-inventory.json"
    }
    if actual_outputs != set(expected_outputs) or "THIRD-PARTY-NOTICES.txt" not in report["output_files"]:
        raise InventoryError("Notice file set differs from inventory")
    if read_bytes(output / "THIRD-PARTY-NOTICES.txt") != render_notices(report).encode("utf-8"):
        raise InventoryError("Human notices do not match the machine inventory")
    copied = set()
    for component in report["components"]:
        for notice in component["notice_files"]:
            name = notice.get("copied_as")
            if name:
                safe_relative(name)
                if name not in report["output_files"] or report["output_files"][name]["sha256"] != notice["sha256"]:
                    raise InventoryError("Copied notice lacks its original fingerprint")
                copied.add(name)
    if copied != {name for name in report["output_files"] if name.startswith("licenses/")}:
        raise InventoryError("Unattributed or missing copied license")
    if (report["summary"].get("native_files") != len(native_entries)
            or report["summary"].get("copied_notices") != len(copied)):
        raise InventoryError("Inventory summary disagrees with artifact set")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--venv-dir", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--allow-package-output", action="store_true",
                        help="Only fresh package/third-party; refused once BUILD-INFO.json exists")
    args = parser.parse_args(argv)
    try:
        report = generate_inventory(args.project_root, args.release_dir, args.output_dir,
                                    venv_dir=args.venv_dir, catalog_path=args.catalog,
                                    allow_package_output=args.allow_package_output)
    except (InventoryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Inventory failed ({type(exc).__name__}): {exc}")
        return 1
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
