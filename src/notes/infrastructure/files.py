"""Conservative local-file adapters. Existing document bytes are never written."""

from __future__ import annotations

import codecs
import ctypes
import ntpath
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Sequence

from notes.domain.models import FileEntry, NotesError, ReadResult, ScanResult

PREVIEW_LIMIT = 256 * 1024
INDEX_LIMIT = 5 * 1024 * 1024
INITIAL_TEXT_LIMIT = 64 * 1024
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
             "utf-32", "utf-32-le", "utf-32-be", "gbk", "gb18030")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_OFFLINE = 0x1000 | 0x40000 | 0x400000
_BINARY_CONTROLS = re.compile(r"[\x01-\x08\x0b\x0e-\x1f]")


def _looks_binary(text: str) -> bool:
    """Preserve the original whole-text, strictly-greater-than-2% C0 rule.

    NUL always qualifies. TAB/LF/CR/FF, DEL/C1 and other Unicode characters do
    not count. Keep the same floating-point division as the original expression;
    a monotone hit count can stop only once the final result is already certain.
    """
    if "\x00" in text:
        return True
    if not text:
        return False
    length = len(text)
    for count, _match in enumerate(_BINARY_CONTROLS.finditer(text), start=1):
        if count / length > 0.02:
            return True
    return False


def _error(code: str, message: str) -> NotesError:
    return NotesError(code, message)


def _component(value: str) -> None:
    if (not isinstance(value, str) or not value or value in (".", "..") or value[-1:] in (" ", ".")
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>:"/\\|?*' for c in value)
            or _RESERVED.match(value)):
        raise _error("invalid_path", "文件名包含保留名称或非法字符。")
    # This application supports NTFS only. Count Windows WCHAR units rather
    # than Python code points so supplementary characters do not evade the cap.
    if len(value) > 255 or len(value.encode("utf-16-le")) > 510:
        raise _error("invalid_path", "文件名超过 NTFS 的单个名称长度上限。")


def validate_document_name(filename: str) -> str:
    """Pure lexical validation shared by template preview and exclusive creation."""
    _component(filename)
    return filename


def _initial_document_bytes(initial_text: str) -> bytes:
    if not isinstance(initial_text, str):
        raise _error("invalid_content", "新文档内容必须是纯文本。")
    # CRLF normalization can at most halve the number of characters. Bound
    # allocation before encoding while measuring the final limit in UTF-8 bytes.
    if len(initial_text) > INITIAL_TEXT_LIMIT * 2:
        raise _error("content_too_large", "新文档初始内容不能超过 64 KiB UTF-8 字节。")
    if initial_text.startswith("\ufeff") or any(
        (ord(character) < 32 and character not in "\t\n\r")
        or 0x7F <= ord(character) <= 0x9F
        or character in "\u2028\u2029"
        for character in initial_text
    ):
        raise _error("invalid_content", "新文档不能以 BOM 开头，也不能包含非文本控制或 Unicode 行分隔字符。")
    text = initial_text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > INITIAL_TEXT_LIMIT:
        raise _error("content_too_large", "新文档初始内容不能超过 64 KiB UTF-8 字节。")
    try:
        data = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _error("invalid_content", "新文档内容包含不能编码为 UTF-8 的字符。") from exc
    if len(data) > INITIAL_TEXT_LIMIT:
        raise _error("content_too_large", "新文档初始内容不能超过 64 KiB UTF-8 字节。")
    return data


def _parts(relative_path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise _error("invalid_path", "路径必须是文本。")
    if not relative_path and allow_empty:
        return ()
    if ntpath.isabs(relative_path) or ntpath.splitdrive(relative_path)[0]:
        raise _error("path_escape", "仅允许文档库内的相对路径。")
    parts = tuple(relative_path.replace("\\", "/").split("/"))
    for part in parts:
        _component(part)
    return parts


def _check_chain(path: Path, *, allow_missing_leaf: bool = False) -> None:
    """Check lexical ancestors before resolve() can hide junctions."""
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and item == path:
                return
            raise
        attrs = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE:
            raise _error("reparse_point", "不支持符号链接、目录联接或云占位文件。")
        if attrs & _OFFLINE:
            raise _error("offline", "文件尚未在本地完整可用。")


def _absolute(path: Path | str) -> Path:
    text = str(path)
    if not text or text.startswith(("\\\\", "//")) or "\x00" in text:
        raise _error("invalid_path", "请选择本地绝对路径，不支持网络或设备路径。")
    result = Path(text)
    if not result.is_absolute():
        raise _error("invalid_path", "请选择本地绝对路径。")
    return Path(os.path.abspath(result))


def _overlap(left: Path, right: Path) -> bool:
    try:
        a, b = os.path.normcase(str(left)), os.path.normcase(str(right))
        return os.path.commonpath((a, b)) in (a, b)
    except ValueError:
        return False


def _local_volume(path: Path) -> None:
    if os.name != "nt":
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor) != 3:  # DRIVE_FIXED
        raise _error("unsupported_volume", "首版仅支持本地固定磁盘上的 NTFS 文档库。")
    fs = ctypes.create_unicode_buffer(32)
    kernel.GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
    )
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    if not kernel.GetVolumeInformationW(path.anchor, None, 0, None, None, None, fs, len(fs)):
        raise ctypes.WinError(ctypes.get_last_error())
    if fs.value.upper() != "NTFS":
        raise _error("unsupported_volume", "首版仅支持 NTFS 文档库。")


def validate_library_root(root_path: str, data_dir: Path,
                          existing_roots: Sequence[str] = ()) -> Path:
    try:
        root = _absolute(root_path)
        _local_volume(root)
        _check_chain(root)
        if not root.is_dir():
            raise _error("invalid_path", "文档库必须是已存在的文件夹。")
        root = root.resolve(strict=True)
        data = _absolute(data_dir).resolve()
        if _overlap(root, data):
            raise _error("app_data_overlap", "文档库不能包含应用数据，也不能位于应用数据中。")
        # Registered roots were canonicalized on entry. Do not probe them again:
        # an offline drive letter may now resolve to a network mapping.
        if any(_overlap(root, _absolute(other)) for other in existing_roots):
            raise _error("library_overlap", "文档库不能重复或互相嵌套。")
        return root
    except OSError as exc:
        raise _error("unavailable", "文档库不可用或无访问权限。") from exc


def validate_document_path(root: Path, relative_path: str, must_exist: bool = True) -> Path:
    try:
        root = _absolute(root)
        parts = _parts(relative_path)
        _local_volume(root)
        _check_chain(root)
        if not root.is_dir():
            raise _error("offline", "文档库不可用。")
        path = root.joinpath(*parts)
        _check_chain(path, allow_missing_leaf=not must_exist)
        resolved = path.resolve(strict=must_exist)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            raise _error("path_escape", "路径超出文档库范围。")
        if must_exist and not resolved.is_file():
            raise _error("invalid_path", "目标不是普通文件。")
        return resolved
    except FileNotFoundError as exc:
        raise _error("missing", "文件或所在目录不存在。") from exc
    except OSError as exc:
        raise _error("unavailable", "文件不可用或无访问权限。") from exc


def _identity_supported(root: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        _local_volume(root)
        return True
    except (OSError, NotesError):
        return False


def _file_identity(info: os.stat_result, supported: bool) -> str:
    """Short-lived identity evidence, never proof of a rename by itself.

    Windows directory-entry stat can return zero dev/ino/nlink. Only a real
    Path.stat/lstat result from a checked local NTFS root should be passed here.
    Birth time is not ctime: Windows path stat and fstat ctime disagree.
    """
    if not supported or os.name != "nt" or getattr(info, "st_nlink", 0) != 1:
        return ""
    fields = tuple(getattr(info, key, None) for key in ("st_dev", "st_ino", "st_birthtime_ns"))
    if any(type(value) is not int or value <= 0 for value in fields):
        return ""
    return "win-ntfs-v1:" + ":".join(f"{value:x}" for value in fields)


def _plain_file(info: os.stat_result):
    attrs = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE:
        raise _error("reparse_point", "不支持符号链接、目录联接或云占位文件。")
    if attrs & _OFFLINE:
        raise _error("offline", "文件尚未在本地完整可用。")
    if not stat.S_ISREG(info.st_mode):
        raise _error("invalid_path", "目标不是普通文件。")


def _entry(path: Path, root: Path, info: os.stat_result, identity_supported=False) -> FileEntry:
    return FileEntry(
        path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns // 1_000_000,
        _file_identity(info, identity_supported),
    )


def get_file_entry(root: Path, relative_path: str) -> FileEntry:
    """Revalidate a current regular file and collect metadata without reading its body."""
    path = validate_document_path(root, relative_path)
    root = _absolute(root).resolve(strict=True)
    identity_supported = _identity_supported(root)
    try:
        before = path.stat(follow_symlinks=False)
        _plain_file(before)
        _check_chain(path)
        after = path.stat(follow_symlinks=False)
        _plain_file(after)
        stable_fields = (
            "st_dev", "st_ino", "st_nlink", "st_birthtime_ns", "st_size", "st_mtime_ns",
        )
        if any(getattr(before, name, None) != getattr(after, name, None) for name in stable_fields):
            raise _error("changed", "文件元信息正在变化，请稍后重试。")
        return _entry(path, root, after, identity_supported)
    except FileNotFoundError as exc:
        raise _error("missing", "文件已被移动或删除。") from exc
    except OSError as exc:
        raise _error("unavailable", "文件元信息暂时不可读取。") from exc


def scan_directory(root: Path, extensions=(".txt", ".md"),
                   excluded_dirs=(".git", ".venv", "__pycache__"),
                   cancel: Event | None = None) -> ScanResult:
    entries: list[FileEntry] = []
    errors: list[str] = []
    try:
        root = _absolute(root)
        _local_volume(root)
        _check_chain(root)
        if not root.is_dir():
            return ScanResult(complete=False, availability="offline", errors=("root_unavailable",))
    except (OSError, NotesError):
        return ScanResult(complete=False, availability="offline", errors=("root_unavailable",))
    extensions = {ext.lower() for ext in extensions}
    excluded = {os.path.normcase(name) for name in excluded_dirs}
    identity_supported = _identity_supported(root)
    pending = [root]
    while pending:
        if cancel is not None and cancel.is_set():
            return ScanResult(tuple(entries), False, "online", tuple(errors + ["cancelled"]))
        directory = pending.pop()
        try:
            _check_chain(directory)
            with os.scandir(directory) as children:
                for child in children:
                    if cancel is not None and cancel.is_set():
                        return ScanResult(tuple(entries), False, "online", tuple(errors + ["cancelled"]))
                    try:
                        info = child.stat(follow_symlinks=False)
                        attrs = getattr(info, "st_file_attributes", 0)
                        if stat.S_ISLNK(info.st_mode) or attrs & (_REPARSE | _OFFLINE):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if os.path.normcase(child.name) not in excluded:
                                pending.append(Path(child.path))
                        elif stat.S_ISREG(info.st_mode) and Path(child.name).suffix.lower() in extensions:
                            # Exclude lexical aliases (ADS/device/trailing dot names).
                            _component(child.name)
                            path = Path(child.path)
                            # DirEntry.stat has zero file identity fields on Windows.
                            # A fresh stat is metadata-only and does not read document bytes.
                            actual = path.stat(follow_symlinks=False)
                            _plain_file(actual)
                            entries.append(_entry(path, root, actual, identity_supported))
                    except (OSError, NotesError):
                        errors.append("entry_unavailable")
        except (OSError, NotesError):
            errors.append("directory_unavailable")
    try:
        _check_chain(root)
        if not root.is_dir():
            raise FileNotFoundError
    except (OSError, NotesError):
        return ScanResult(tuple(entries), False, "offline", tuple(errors + ["root_unavailable"]))
    return ScanResult(tuple(entries), not errors, "online", tuple(errors))


def create_document(root: Path, relative_dir: str, filename: str,
                    extensions=(".txt", ".md"), *, initial_text: str = "",
                    cancel: Event | None = None, excluded_dirs=()) -> FileEntry:
    if cancel is not None and cancel.is_set():
        raise _error("cancelled", "创建已取消，未新建文件。")
    filename = validate_document_name(filename)
    if Path(filename).suffix.lower() not in {ext.lower() for ext in extensions}:
        raise _error("invalid_extension", "文件扩展名不在文档库允许的类型中。")
    data = _initial_document_bytes(initial_text)
    parts = _parts(relative_dir, allow_empty=True)
    excluded = {os.path.normcase(directory) for directory in excluded_dirs}
    if any(os.path.normcase(part) in excluded for part in parts):
        raise _error("out_of_scope", "目标目录被文档库规则排除，未新建文件。")
    relative = "/".join((*parts, filename))
    path = validate_document_path(root, relative, must_exist=False)
    if cancel is not None and cancel.is_set():
        raise _error("cancelled", "创建已取消，未新建文件。")
    created = False
    try:
        # O_EXCL is authoritative, not a prior exists() check. Track creation
        # before wrapping the descriptor so even fdopen/close failures are honest.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        created = True
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            if cancel is not None and cancel.is_set():
                raise _error("cancelled", "创建后收到取消请求。")
            written = stream.write(data)
            if type(written) is not int or written != len(data):
                raise OSError("short initial document write")
            if cancel is not None and cancel.is_set():
                raise _error("cancelled", "写入后收到取消请求。")
            stream.flush()
            os.fsync(stream.fileno())
        if cancel is not None and cancel.is_set():
            raise _error("cancelled", "写入后收到取消请求。")
        entry = get_file_entry(root, relative)
        if cancel is not None and cancel.is_set():
            raise _error("cancelled", "写入后收到取消请求。")
        return entry
    except Exception as exc:
        if created:
            raise _error(
                "created_partial",
                "文件已建立，但写入或校验未完整确认；可能为空或仅有部分内容。"
                f"现有文件已保留：{path}\n请检查并刷新文档库，不要重复创建。",
            ) from exc
        if isinstance(exc, FileExistsError):
            raise _error("already_exists", "已存在同名文件，未覆盖。") from exc
        raise _error("create_failed", "文件创建失败，请检查目录和权限。") from exc


def list_folders(root: Path, excluded_dirs=(".git", ".venv", "__pycache__"),
                 cancel: Event | None = None) -> tuple[str, ...]:
    """List existing eligible directories; never silently return a partial tree."""
    try:
        root = _absolute(root)
        _local_volume(root)
        _check_chain(root)
        if not root.is_dir():
            raise FileNotFoundError
        pending = [root]
        result = [""]
        excluded = {os.path.normcase(name) for name in excluded_dirs}
        while pending:
            if cancel is not None and cancel.is_set():
                raise _error("cancelled", "文件夹枚举已取消。")
            directory = pending.pop()
            _check_chain(directory)
            with os.scandir(directory) as children:
                for child in children:
                    if cancel is not None and cancel.is_set():
                        raise _error("cancelled", "文件夹枚举已取消。")
                    info = child.stat(follow_symlinks=False)
                    attrs = getattr(info, "st_file_attributes", 0)
                    if stat.S_ISLNK(info.st_mode) or attrs & (_REPARSE | _OFFLINE):
                        continue
                    if stat.S_ISDIR(info.st_mode) and os.path.normcase(child.name) not in excluded:
                        _component(child.name)
                        path = Path(child.path)
                        pending.append(path)
                        result.append(path.relative_to(root).as_posix())
        return tuple(sorted(result, key=os.path.normcase))
    except OSError as exc:
        raise _error("unavailable", "无法完整读取文件夹列表，请检查目录权限后重试。") from exc


@contextmanager
def _shared_reader(path: Path):
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    # fd takes ownership of handle; fdopen takes ownership of fd.
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


def _signature(info: os.stat_result) -> tuple:
    # Windows path stat and descriptor fstat can expose different ctime values
    # (creation time versus change time). Identity, size and mtime are comparable.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def retry_code_for_exception(exc: BaseException) -> str:
    """Return a safe machine code; never classify from localized exception text.

    ``unavailable`` wrappers retain their cause so sharing failures and access
    denial remain distinguishable from policy rejection or missing files.
    """
    if isinstance(exc, NotesError):
        if exc.code == "unavailable":
            return retry_code_for_exception(exc.__cause__) if exc.__cause__ else "io_error"
        return exc.code
    winerror = getattr(exc, "winerror", None)
    if winerror in (32, 33, 1224):  # sharing/lock violation or mapped file
        return "sharing_violation"
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError) or winerror in (5, 65):
        return "access_denied"
    if isinstance(exc, IsADirectoryError):
        return "not_file"
    if isinstance(exc, NotADirectoryError) or winerror in (123, 206):
        return "invalid_path"
    if winerror in (21, 53, 64, 67):
        return "offline"
    return "io_error" if isinstance(exc, OSError) else "unknown"


def read_document(path: Path, encoding_override=None, preview: bool = False,
                  cancel: Event | None = None) -> ReadResult:
    if cancel is not None and cancel.is_set():
        return ReadResult(status="pending", error_code="cancelled", message="读取已取消。")
    try:
        path = _absolute(path)
        _local_volume(path)
        _check_chain(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return ReadResult(status="error", error_code="not_file", message="目标不是普通文件。")
        fields = {"size_bytes": before.st_size, "modified_at": before.st_mtime_ns // 1_000_000}
        limit = PREVIEW_LIMIT if preview else INDEX_LIMIT
        if not preview and before.st_size > limit:
            return ReadResult(status="skipped_large", error_code="too_large",
                              message="文件超过 5 MiB 索引上限。", **fields)
        with _shared_reader(path) as stream:
            opened = os.fstat(stream.fileno())
            chunks = []
            remaining = limit + 1
            while remaining:
                if cancel is not None and cancel.is_set():
                    return ReadResult(status="pending", error_code="cancelled",
                                      message="读取已取消。", **fields)
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after_handle = os.fstat(stream.fileno())
        after = path.stat()
        _check_chain(path)
        if not (_signature(before) == _signature(opened)
                == _signature(after_handle) == _signature(after)):
            return ReadResult(status="pending", error_code="changed",
                              message="文件读取期间发生变化，请稍后重试。", **fields)
        data = b"".join(chunks)
        truncated = len(data) > limit
        if truncated and not preview:
            return ReadResult(status="skipped_large", error_code="too_large", **fields)
        data = data[:limit]
        bom_encoding = ""
        for bom, name in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
            if data.startswith(bom):
                bom_encoding = name
                break
        encoding = encoding_override or bom_encoding or "utf-8"
        try:
            canonical = codecs.lookup(encoding).name
            if canonical not in {codecs.lookup(name).name for name in ENCODINGS}:
                return ReadResult(status="needs_encoding", error_code="unsupported_encoding",
                                  message="不支持所选编码。", **fields)
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            text = decoder.decode(data, final=not truncated)
        except (LookupError, UnicodeError):
            return ReadResult(status="needs_encoding", encoding=encoding, error_code="decode_error",
                              message="无法严格解码，请手动选择文件编码。", **fields)
        if _looks_binary(text):
            return ReadResult(status="skipped_binary", error_code="binary",
                              message="检测到二进制内容，不建立正文索引。", **fields)
        message = ""
        if encoding_override and bom_encoding and encoding_override != bom_encoding:
            message = "手动编码覆盖了文件 BOM 指示，请确认显示结果。"
        return ReadResult(text, encoding, "ready", truncated, message=message, **fields)
    except FileNotFoundError:
        return ReadResult(status="error", error_code="missing", message="文件不存在。")
    except PermissionError as exc:
        return ReadResult(status="pending", error_code=retry_code_for_exception(exc),
                          message="文件锁定或访问被拒绝，请稍后重试。")
    except NotesError as exc:
        return ReadResult(status="error", error_code=retry_code_for_exception(exc),
                          message=exc.message)
    except OSError as exc:
        return ReadResult(status="error", error_code=retry_code_for_exception(exc),
                          message="文件暂时不可读取。")
