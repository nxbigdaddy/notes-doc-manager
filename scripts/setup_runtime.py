"""Create a pinned, project-private Windows x64 runtime; no registry/PATH changes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import struct
import subprocess
import urllib.request
import zipfile

PYTHON_VERSION = "3.14.7"
PYTHON_URL = "https://api.nuget.org/v3-flatcontainer/python/3.14.7/python.3.14.7.nupkg"
PYTHON_SHA256 = "46a4da5529a92d18ff894911f6e6033a8253198d705b8161bf28c9123c87d46b"
SQLITE_VERSION = "3.53.4"
SQLITE_URL = "https://sqlite.org/2026/sqlite-dll-win-x64-3530400.zip"
# Mechanically verified against SQLite's official PRODUCT download record.
SQLITE_SHA3 = "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a"


def obtain(url, path, expected, algorithm):
    if not path.exists():
        with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    digest = hashlib.new(algorithm, path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(f"Pinned {algorithm} check failed for {path.name}; no installation attempted")


def checked_members(archive, target):
    root = target.resolve()
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or PureWindowsPath(name).drive
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise RuntimeError("Unsafe archive member")
        destination = (target / name).resolve()
        if not destination.is_relative_to(root):
            raise RuntimeError("Archive escaped private runtime")
        yield info, destination


def main():
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("This pinned runtime is for Windows x64")
    root = Path(__file__).resolve().parents[1]
    tools = root / ".tools"
    tools.mkdir(exist_ok=True)
    python_package = tools / f"python.{PYTHON_VERSION}.nupkg"
    sqlite_package = tools / "sqlite-dll-win-x64-3530400.zip"
    obtain(PYTHON_URL, python_package, PYTHON_SHA256, "sha256")
    obtain(SQLITE_URL, sqlite_package, SQLITE_SHA3, "sha3_256")
    runtime = tools / f"python-{PYTHON_VERSION}"
    dll_path = runtime / "tools" / "DLLs" / "sqlite3.dll"
    with zipfile.ZipFile(python_package) as package:
        members = list(checked_members(package, runtime))
        if not runtime.exists():
            package.extractall(runtime)
        for info, destination in members:
            if info.is_dir() or destination == dll_path:
                continue
            if not destination.is_file() or destination.read_bytes() != package.read(info):
                raise RuntimeError("Existing private runtime differs from the pinned official package")
        original = package.read("tools/DLLs/sqlite3.dll")
    with zipfile.ZipFile(sqlite_package) as package:
        list(checked_members(package, tools / "sqlite-package-check"))
        updated = package.read("sqlite3.dll")
    if updated[:2] != b"MZ":
        raise RuntimeError("SQLite artifact is not a Windows DLL")
    pe = struct.unpack_from("<I", updated, 0x3C)[0]
    if updated[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", updated, pe + 4)[0] != 0x8664:
        raise RuntimeError("SQLite artifact architecture is not x64")
    prior = tools / f"sqlite3-cpython-{PYTHON_VERSION}-original.dll"
    if not prior.exists():
        prior.write_bytes(original)
    elif prior.read_bytes() != original:
        raise RuntimeError("Original SQLite archive copy changed")
    if dll_path.read_bytes() not in (original, updated):
        raise RuntimeError("Private SQLite DLL was modified; do not overwrite an unknown artifact")
    if dll_path.read_bytes() != updated:
        dll_path.write_bytes(updated)
    manifests = {
        "python-runtime.json": {
            "version": PYTHON_VERSION, "url": PYTHON_URL, "sha256": PYTHON_SHA256,
            "bytes": python_package.stat().st_size,
            "scope": "project-private official CPython NuGet; no registry/PATH changes",
        },
        "sqlite-runtime.json": {
            "version": SQLITE_VERSION, "url": SQLITE_URL, "archive_sha3_256": SQLITE_SHA3,
            "dll_sha256": hashlib.sha256(updated).hexdigest(),
            "original_dll_sha256": hashlib.sha256(original).hexdigest(),
            "scope": "official SQLite DLL in new private CPython; no system changes",
        },
    }
    for filename, payload in manifests.items():
        (tools / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    executable = runtime / "tools" / "python.exe"
    subprocess.run(
        [str(executable), "-c",
         "import sys,sqlite3; print(sys.version); print('SQLite',sqlite3.sqlite_version); "
         "assert sqlite3.sqlite_version_info == (3,53,4)"],
        check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"Verified private runtime: {executable}")
    print("Create the project venv using this executable; no system installation was changed.")


if __name__ == "__main__":
    main()
