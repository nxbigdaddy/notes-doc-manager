# Build: .venv\Scripts\python.exe -m PyInstaller --noconfirm packaging\notes.spec
from pathlib import Path
import hashlib
import json
import tomllib
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
provenance_file = root / "build" / "build-provenance.json"
provenance_file.parent.mkdir(parents=True, exist_ok=True)
provenance_file.write_text(
    json.dumps({
        "version": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "source_sha256": {
            str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src").rglob("*.py"))
        },
        "private_runtime": {
            name: json.loads((root / ".tools" / name).read_text(encoding="utf-8"))
            for name in ("python-runtime.json", "sqlite-runtime.json")
        },
    }, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
hidden = [
    "wx.stc", "sqlite3", "watchdog.observers.read_directory_changes",
    "encodings.gbk", "encodings.gb18030", "encodings.utf_16_le", "encodings.utf_16_be",
    "encodings.utf_32_le", "encodings.utf_32_be",
] + collect_submodules("notes")
metadata = copy_metadata("wxPython") + copy_metadata("watchdog") + [(str(provenance_file), ".")]
a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=[], datas=metadata, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Notes",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Notes")
