"""Explicit, shell-free external-editor invocation."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from notes.domain.models import NotesError
from notes.infrastructure.files import _absolute, _check_chain

_launch_lock = Lock()


def validate_editor(path: str) -> str:
    try:
        candidate = _absolute(path)
        _check_chain(candidate)
        if candidate.suffix.lower() != ".exe" or not candidate.is_file():
            raise NotesError("invalid_editor", "请选择可信的 Notepad3 .exe 文件。")
        return str(candidate.resolve(strict=True))
    except (OSError, NotesError) as exc:
        raise NotesError("invalid_editor", "编辑器路径无效；请选择已安装的可信 .exe 文件。") from exc


def _external_environment() -> dict[str, str]:
    env = dict(os.environ)
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        bundle = os.path.normcase(os.path.abspath(bundle))
        def external(item):
            try:
                return os.path.commonpath((bundle, os.path.normcase(os.path.abspath(item)))) != bundle
            except ValueError:
                return True
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                    if p and external(p))
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


@contextmanager
def _external_dll_directory():
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = (wintypes.LPCWSTR,)
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    count = kernel.GetDllDirectoryW(0, None)
    previous = ctypes.create_unicode_buffer(count + 1)
    kernel.GetDllDirectoryW(len(previous), previous)
    if not kernel.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.SetDllDirectoryW(previous.value or None)


def open_document(editor_path: str, document_path: str | Path, line: int | None = None) -> None:
    editor = validate_editor(editor_path)
    try:
        path = _absolute(document_path)
        _check_chain(path)
        if not path.is_file():
            raise NotesError("missing", "文件不存在或不是普通文件。")
        if line is not None and (type(line) is not int or line < 1 or line > 2_147_483_647):
            raise NotesError("invalid_line", "定位行号必须是正整数。")
        args = [editor]
        if line is not None:
            args.extend(("/g", str(line)))
        args.append(str(path))
        # This lock serializes our process-global DLL-directory transitions.
        with _launch_lock, _external_dll_directory():
            subprocess.Popen(args, shell=False, cwd=str(Path(editor).parent),
                             env=_external_environment(), close_fds=True)
    except OSError as exc:
        raise NotesError("editor_launch_failed", "无法发出编辑请求，请检查编辑器和文件路径。") from exc


def detect_editor() -> str:
    """Discover candidates only. Detection never executes or trusts a restored path."""
    candidates = []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for access in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                           winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Notepad3.exe",
                                        0, access) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if isinstance(value, str):
                            candidates.append(value.strip('"'))
                except OSError:
                    pass
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.extend((str(Path(base) / "Notepad3" / "Notepad3.exe"),
                               str(Path(base) / "Programs" / "Notepad3" / "Notepad3.exe")))
    for candidate in candidates:
        try:
            return validate_editor(candidate)
        except NotesError:
            pass
    return ""
