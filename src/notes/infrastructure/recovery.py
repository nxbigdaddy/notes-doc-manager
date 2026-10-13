"""Pre-service, explicit-confirmation metadata recovery.

No Store is constructed here. Untrusted SQLite is read from an isolated copy,
never installed verbatim; only validated logical rows enter a clean database.
Recovery sessions and their protective copies are retained, never rotated away.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from notes import __version__
from notes.domain.models import Library, NotesError
from notes.domain.smart_lists import MAX_SMART_LISTS
from notes.infrastructure.store import SCHEMA, Store, _relative, normalize_text

LIVE_NAMES = ("metadata.db", "metadata.db-wal", "metadata.db-shm", "metadata.db-journal")
SIDECARS = LIVE_NAMES[1:]
MAX_DB_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 100 * 1024 * 1024
MAX_ROWS = 250_000
TIMEOUT_SECONDS = 30
SESSION_NAME = re.compile(r"[0-9a-f]{32}\Z")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
BACKUP_NAME = re.compile(r"(?:auto|metadata)-\d{8}T\d{6}-[0-9a-f]{8,32}\.db\Z")
STAGES = {"prepared", "sidecars_removed", "metadata_installed", "committed", "completed", "rolled_back"}
TERMINAL_STAGES = {"completed", "rolled_back"}
TABLE_COLUMNS = {
    "libraries": ("id", "name", "root_path", "root_key", "enabled", "availability", "extensions", "excluded_dirs"),
    "documents": ("id", "library_id", "relative_path", "path_key", "file_name", "size_bytes", "modified_at",
                  "availability", "in_scope", "index_status", "content_generation", "is_favorite",
                  "last_open_requested_at", "encoding_override"),
    "tags": ("id", "name", "name_key"),
    "document_tags": ("document_id", "tag_id"),
    "settings": ("key", "value"),
}
SMART_LIST_COLUMNS = ("id", "name", "name_key", "query_json")
OPTIONAL_TABLE_COLUMNS = {
    "document_file_identities": ("document_id", "identity"),
    "smart_lists": SMART_LIST_COLUMNS,
}


def _failure(code, message):
    return NotesError(code, message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid_number(_value):
        raise ValueError("nonfinite JSON number")
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=invalid_number)


class _Budget:
    def __init__(self, cancel=None):
        self.cancel = cancel
        self.deadline = time.monotonic() + TIMEOUT_SECONDS

    def check(self):
        if self.cancel and (self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel()):
            raise _failure("cancelled", "恢复已取消，尚未提交的原数据没有改动。")
        if time.monotonic() > self.deadline:
            raise _failure("recovery_timeout", "检查超时，请关闭其他占用程序后重试。")

    def progress(self):
        try:
            self.check()
            return 0
        except NotesError:
            return 1


class RecoveryManager:
    """Must be used under the application's single-instance guard, before workers."""

    def __init__(self, data_dir: Path, fault_hook=None):
        self.data_dir = Path(os.path.abspath(data_dir))
        self.fault_hook = fault_hook

    def _hook(self, stage):
        if self.fault_hook:
            self.fault_hook(stage)

    @staticmethod
    def _local_volume(path):
        from notes.infrastructure.files import _local_volume
        if str(path).replace("/", "\\").startswith("\\\\") or not path.is_absolute():
            raise _failure("unsupported", "仅支持本地固定 NTFS 卷，不支持网络或设备路径。")
        try:
            _local_volume(path)
        except NotesError as exc:
            raise _failure("unsupported", exc.message) from None

    @staticmethod
    def _plain_chain(path, allow_missing=False):
        for item in (*reversed(path.parents), path):
            try:
                info = item.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return
                raise
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise _failure("unsafe_path", "恢复路径不能经过符号链接、目录联接或其他重解析点。")
        return

    def _safe_path(self, path, allow_missing=False):
        path = Path(os.path.abspath(path))
        _, tail = ntpath.splitdrive(str(path))
        try:
            if tail.strip("/\\"):
                _relative(tail.strip("/\\"))
        except NotesError:
            raise _failure("unsafe_path", "恢复路径包含设备名、数据流或其他不安全的路径组件。") from None
        self._local_volume(path)  # Before any filesystem probing of a candidate.
        self._plain_chain(path, allow_missing)
        return path

    @contextmanager
    def _guard_live(self):
        """Windows read/delete-sharing guard: deny concurrent writable handles.

        READONLY inspection happens on copies; this handle does not ask SQLite
        to repair or regenerate a live WAL/SHM. Windows installation explicitly
        releases these handles, then rechecks fingerprints before replacement.
        """
        handles = []
        kernel = None
        def release():
            if kernel:
                for handle in reversed(handles):
                    kernel.CloseHandle(handle)
                handles.clear()
        try:
            if os.name == "nt":
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
                kernel.CreateFileW.restype = wintypes.HANDLE
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel.CloseHandle.restype = wintypes.BOOL
                for name in LIVE_NAMES:
                    path = self.data_dir / name
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    self._plain_chain(path)
                    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x00200000, None)
                    if handle == ctypes.c_void_p(-1).value:
                        code = ctypes.get_last_error()
                        if code in (32, 33):
                            raise _failure("database_busy", "管理数据库被其他程序占用，请关闭后重试。")
                        raise _failure("inaccessible", "无法独占保护管理数据库，请检查访问权限。")
                    handles.append(handle)
            yield release
        finally:
            release()

    def _digest_file(self, path, budget, limit=MAX_DB_BYTES):
        budget.check()
        self._plain_chain(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _failure("unsafe_path", "恢复文件必须是独立普通文件，不能使用硬链接。")
        if before.st_size > limit:
            raise _failure("recovery_limit", "文件超过恢复大小上限，未改动原数据。")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                budget.check()
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field)
               for field in fields):
            raise _failure("state_changed", "检查期间文件发生变化，请重新检查并确认。")
        return {"size": before.st_size, "sha256": digest.hexdigest()}

    def _manifest(self, base, budget):
        result = {}
        total = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(base) + suffix)
            try:
                descriptor = self._digest_file(path, budget)
            except FileNotFoundError:
                continue
            result["metadata.db" + suffix] = descriptor
            total += descriptor["size"]
        if total > MAX_BUNDLE_BYTES:
            raise _failure("recovery_limit", "数据库及附属文件总量超过恢复上限。")
        return result

    @staticmethod
    def _signature(path, manifest):
        return hashlib.sha256(_canonical({"path": ntpath.normcase(str(path)), "files": manifest})).hexdigest()

    def _copy_exact(self, source, target, expected, budget):
        self._plain_chain(source)
        self._plain_chain(target.parent)
        with source.open("rb") as input_file, target.open("xb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                budget.check()
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if self._digest_file(target, budget) != expected:
            raise _failure("state_changed", "保护副本校验失败，未继续覆盖原数据。")

    def _clone_bundle(self, base, directory, manifest, budget):
        target = directory / "metadata.db"
        for name, descriptor in manifest.items():
            suffix = name[len("metadata.db"):]
            self._copy_exact(Path(str(base) + suffix), directory / name, descriptor, budget)
        if self._manifest(base, budget) != manifest:
            raise _failure("state_changed", "数据库在复制期间发生变化，请重试。")
        return target

    @staticmethod
    def _configure_read(connection, budget):
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=100")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 256 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.set_progress_handler(budget.progress, 1000)

    def _read_sqlite(self, path, manifest, budget):
        if "metadata.db" not in manifest:
            raise _failure("invalid_backup", "数据库主文件缺失。")
        # A recognisable future-version header is not permission to downgrade
        # merely because another part of that database is also damaged.
        with path.open("rb") as source:
            header = source.read(100)
        if len(header) >= 64 and header[:16] == b"SQLite format 3\x00":
            header_version = int.from_bytes(header[60:64], "big", signed=True)
            if header_version not in (0, 1, 2):
                error = _failure("unsupported", "数据库头声明了不兼容版本，禁止降级覆盖。")
                error.version = header_version
                raise error
        with tempfile.TemporaryDirectory(prefix="notes-recovery-inspect-") as temporary:
            isolated = self._clone_bundle(path, Path(temporary), manifest, budget)
            connection = sqlite3.connect(isolated.as_uri() + "?mode=ro", uri=True, timeout=0.1,
                                         isolation_level=None, check_same_thread=True)
            try:
                self._configure_read(connection, budget)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    error = _failure("unsupported", "数据库版本高于或不兼容当前版本，禁止降级覆盖。")
                    error.version = version
                    raise error
                schema = connection.execute("SELECT type,name,sql FROM sqlite_schema").fetchall()
                tables = {}
                for item in schema:
                    kind, name, sql = item["type"], item["name"], item["sql"] or ""
                    if kind in {"trigger", "view"} or "VIRTUAL TABLE" in sql.upper():
                        raise _failure("invalid_backup", "SQLite 候选包含触发器、视图或虚拟表，拒绝执行或导入。")
                    if kind == "table" and not name.startswith("sqlite_"):
                        if name not in TABLE_COLUMNS and name not in OPTIONAL_TABLE_COLUMNS:
                            raise _failure("invalid_backup", "SQLite 候选包含未知数据表。")
                        if re.search(r"\b(?:CHECK|GENERATED)\b", sql, re.IGNORECASE):
                            raise _failure("invalid_backup", "SQLite 候选包含不允许的表达式。")
                        columns = connection.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
                        expected = TABLE_COLUMNS.get(name, OPTIONAL_TABLE_COLUMNS.get(name))
                        if tuple(column["name"] for column in columns) != expected:
                            raise _failure("invalid_backup", "SQLite 表字段与支持的元数据格式不一致。")
                        if any(column["hidden"] != 0 or column["dflt_value"] not in (None, "0", "NULL")
                               for column in columns):
                            raise _failure("invalid_backup", "不允许隐藏、生成或表达式默认值字段。")
                        for index in connection.execute(f'PRAGMA index_list("{name}")'):
                            if index["partial"]:
                                raise _failure("invalid_backup", "不支持候选中的表达式或部分索引。")
                            index_name = index["name"].replace('"', '""')
                            index_columns = connection.execute(f'PRAGMA index_xinfo("{index_name}")').fetchall()
                            if any(column["cid"] == -2 or column["coll"] not in ("BINARY", "NOCASE", "RTRIM", None)
                                   for column in index_columns):
                                raise _failure("invalid_backup", "不支持候选中的表达式或自定义排序索引。")
                        tables[name] = True
                if not set(TABLE_COLUMNS).issubset(tables):
                    raise _failure("invalid_backup", "数据库缺少必要元数据表，不能自动补空表。")
                if version == 2 or "smart_lists" in tables:
                    Store.validate_smart_list_schema(connection, required=version == 2)
                if version < 2 and "smart_lists" in tables:
                    if connection.execute("SELECT 1 FROM smart_lists LIMIT 1").fetchone() is not None:
                        raise _failure("invalid_backup", "旧版数据库包含非空智能列表，拒绝静默丢弃。")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise _failure("invalid_backup", "SQLite 快速完整性检查失败。")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise _failure("invalid_backup", "SQLite 完整性检查失败。")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise _failure("invalid_backup", "SQLite 关联完整性检查失败。")
                payload = {"format_version": 2 if version == 2 else 1,
                           "app_version": __version__, "exported_at_utc": ""}
                read_tables = dict(TABLE_COLUMNS)
                if version == 2:
                    read_tables["smart_lists"] = SMART_LIST_COLUMNS
                for table, columns in read_tables.items():
                    projection = ",".join('"' + name + '"' for name in columns)
                    rows = []
                    for row in connection.execute(f'SELECT {projection} FROM "{table}" NOT INDEXED'):
                        budget.check()
                        limit = MAX_SMART_LISTS if table == "smart_lists" else MAX_ROWS
                        if len(rows) >= limit:
                            if table == "smart_lists":
                                raise _failure("invalid_backup", "智能列表数量超过支持上限。")
                            raise _failure("recovery_limit", "候选记录数量超过恢复上限。")
                        rows.append(dict(row))
                    payload[table] = rows
                payload["__derived_identity_count"] = (
                    connection.execute("SELECT count(*) FROM document_file_identities").fetchone()[0]
                    if "document_file_identities" in tables else 0)
                return payload, version
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_INTERRUPT:
                    budget.check()  # Preserve timeout/cancellation classification.
                raise
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()

    def _validate_payload(self, payload, budget):
        def require(condition):
            if not condition:
                raise _failure("invalid_backup", "备份字段类型或约束校验失败。")
        require(isinstance(payload, dict))
        require(type(payload.get("format_version")) is int and payload["format_version"] in (1, 2))
        for table in TABLE_COLUMNS:
            require(isinstance(payload.get(table), list) and len(payload[table]) <= MAX_ROWS)
            require(all(isinstance(row, dict) for row in payload[table]))
        if payload["format_version"] == 2:
            smart_lists = payload.get("smart_lists")
            require(isinstance(smart_lists, list) and len(smart_lists) <= MAX_SMART_LISTS)
            for row in smart_lists:
                budget.check()
                require(isinstance(row, dict) and set(row) == set(SMART_LIST_COLUMNS))
                for field in SMART_LIST_COLUMNS:
                    require(isinstance(row[field], str))
                    row[field].encode("utf-8", errors="strict")
        elif "smart_lists" in payload:
            require(isinstance(payload["smart_lists"], list) and not payload["smart_lists"])
        for row in payload["libraries"]:
            budget.check()
            for field in ("id", "name", "root_path", "extensions", "excluded_dirs"):
                require(isinstance(row.get(field), str))
                row[field].encode("utf-8", errors="strict")
            require(len(row["root_path"]) <= 32767 and not any(ord(c) < 32 for c in row["name"]))
            for field, limit in (("extensions", 128), ("excluded_dirs", 256)):
                values = _json(row[field].encode("utf-8"))
                require(isinstance(values, list) and len(values) <= limit)
                require(all(isinstance(value, str) and 0 < len(value) <= 255 for value in values))
        for row in payload["documents"]:
            budget.check()
            require(isinstance(row.get("relative_path"), str) and len(row["relative_path"]) <= 32767)
            require(type(row.get("content_generation")) is int and 0 <= row["content_generation"] < 2**63 - 1024)
        for row in payload["settings"]:
            require(isinstance(row.get("key"), str) and isinstance(row.get("value"), str))
            require(len(row["key"]) <= 100 and len(row["value"]) <= 100_000)
            row["key"].encode("utf-8", errors="strict")
            row["value"].encode("utf-8", errors="strict")
        # This pass is purely lexical; in particular it never probes mapped
        # network/offline roots from an untrusted backup.
        Store.validate_backup_payload(payload, self.data_dir, check_paths=False)
        self._safe_path(self.data_dir, allow_missing=True)
        canonical_data = ntpath.normcase(str(self.data_dir.resolve(strict=False)))
        local_roots = {}
        canonical_roots = []
        for row in payload["libraries"]:
            root = Path(row["root_path"])
            try:
                self._local_volume(root)
            except (NotesError, OSError):
                continue  # Keep unsupported/offline references as unknown.
            self._plain_chain(root, allow_missing=True)
            # Only after volume and link checks may local aliases (e.g. Windows
            # short names) be resolved. Lexical appdata checks alone are not
            # sufficient when two local spellings name the same directory.
            canonical_root = ntpath.normcase(str(root.resolve(strict=False)))
            for other in (canonical_data, *canonical_roots):
                try:
                    common = ntpath.commonpath((canonical_root, other))
                except ValueError:
                    continue
                if common in (canonical_root, other):
                    raise _failure("invalid_backup", "文档库与应用数据或其他本地文档库实际路径重叠。")
            canonical_roots.append(canonical_root)
            local_roots[row["id"]] = root
        for row in payload["documents"]:
            budget.check()
            root = local_roots.get(row["library_id"])
            if root is not None:
                self._plain_chain(root.joinpath(*PureWindowsPath(row["relative_path"]).parts), allow_missing=True)

    def _has_history(self):
        """Inspect only fixed appdata children, never document libraries.

        Even an empty owned backup/recovery directory is conservatively history:
        it can be the residue of interrupted previous work, not a new profile.
        """
        for base in ("search.db", "search-v2.db"):
            for suffix in ("", "-wal", "-shm", "-journal"):
                path = self.data_dir / (base + suffix)
                try:
                    self._plain_chain(path)
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise _failure("unsafe_path", "历史搜索缓存路径不是普通文件，不能静默新建管理库。")
                return True
        for name in ("backups", "recovery"):
            path = self.data_dir / name
            try:
                self._plain_chain(path)
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise _failure("unsafe_path", "历史备份或恢复目录结构异常，不能静默新建管理库。")
            return True
        return False

    def inspect_current(self):
        signature = ""
        version = None
        try:
            self._safe_path(self.data_dir, allow_missing=True)
            budget = _Budget()
            with self._guard_live():
                manifest = self._manifest(self.data_dir / "metadata.db", budget)
                signature = self._signature(self.data_dir / "metadata.db", manifest)
                if not manifest:
                    if self._has_history():
                        return dict(status="corrupt", message="管理主库缺失，但发现历史缓存、备份或恢复目录；请选择备份，不会新建空库。",
                                    signature=signature, fingerprint=signature, recoverable=True, version=None)
                    return dict(status="missing", message="尚未创建管理数据库。", signature=signature,
                                fingerprint=signature, recoverable=False, version=None)
                payload, version = self._read_sqlite(self.data_dir / "metadata.db", manifest, budget)
                self._validate_payload(payload, budget)
                return dict(status="healthy", message="管理数据库通过只读完整性检查。", signature=signature,
                            fingerprint=signature, recoverable=False, version=version)
        except NotesError as exc:
            version = getattr(exc, "version", version)
            status = {"unsupported": "unsupported", "recovery_limit": "unsupported",
                      "unsupported_volume": "unsupported",
                      "database_busy": "busy", "state_changed": "busy",
                      "inaccessible": "inaccessible", "unsafe_path": "inaccessible",
                      "recovery_timeout": "busy"}.get(exc.code, "corrupt")
            return dict(status=status, message=exc.message, signature=signature, fingerprint=signature,
                        recoverable=status == "corrupt", version=version)
        except (PermissionError, OSError):
            return dict(status="inaccessible", message="无法读取管理数据库，请检查权限或磁盘。",
                        signature=signature, fingerprint=signature, recoverable=False, version=version)
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return dict(status="corrupt", message="管理数据库损坏或不符合支持格式；未创建空数据库。",
                        signature=signature, fingerprint=signature, recoverable=True, version=version)

    def list_backups(self):
        directory = self.data_dir / "backups"
        try:
            self._safe_path(directory)
        except FileNotFoundError:
            return ()
        result = []
        for path in directory.iterdir():
            if not (BACKUP_NAME.fullmatch(path.name) or path.suffix.lower() == ".json"):
                continue
            try:
                self._plain_chain(path)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                result.append(dict(path=str(path), name=path.name, size=info.st_size, mtime=info.st_mtime))
            except (OSError, NotesError):
                continue
        return tuple(sorted(result, key=lambda row: (row["mtime"], row["name"]), reverse=True))

    def _inspect_backup(self, path, cancel=None):
        budget = _Budget(cancel)
        path = self._safe_path(path)
        if path.suffix.lower() == ".json":
            descriptor = self._digest_file(path, budget, MAX_JSON_BYTES)
            with path.open("rb") as stream:
                data = stream.read(descriptor["size"] + 1)
            if len(data) != descriptor["size"] or self._digest_file(path, budget, MAX_JSON_BYTES) != descriptor:
                raise _failure("state_changed", "备份在检查期间发生变化。")
            payload = _json(data)
            kind, version = "json", payload.get("format_version") if isinstance(payload, dict) else None
            manifest = {"metadata.db": descriptor}
        elif path.suffix.lower() == ".db":
            manifest = self._manifest(path, budget)
            payload, version = self._read_sqlite(path, manifest, budget)
            kind = "db"
        else:
            raise _failure("invalid_backup", "仅支持 JSON 逻辑备份或 SQLite .db 快照。")
        self._validate_payload(payload, budget)
        signature = self._signature(path, manifest)
        return payload, dict(kind=kind, path=str(path), signature=signature, fingerprint=signature,
                             libraries=len(payload["libraries"]), documents=len(payload["documents"]),
                             tags=len(payload["tags"]), version=version, mtime=path.stat().st_mtime,
                             smart_lists=len(payload["smart_lists"]) if payload["format_version"] == 2 else 0,
                             size=sum(item["size"] for item in manifest.values()),
                             editor_requires_confirmation=True, contains_document_bodies=False)

    def inspect_backup(self, path):
        try:
            return self._inspect_backup(path)[1]
        except NotesError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _failure("invalid_backup", "备份完整性、类型或路径校验失败。") from None

    def _build_database(self, path, payload, budget):
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA:
                connection.execute(statement)
            if payload["format_version"] == 2:
                from notes.infrastructure.store import SMART_LIST_SCHEMA
                connection.execute(SMART_LIST_SCHEMA)
            libraries = {}
            for row in payload["libraries"]:
                budget.check()
                extensions = [value.lower() for value in _json(row["extensions"].encode("utf-8"))]
                connection.execute("INSERT INTO libraries VALUES (?,?,?,?,?,?,?,?)",
                                   (row["id"], row["name"], row["root_path"], ntpath.normcase(ntpath.normpath(row["root_path"])),
                                    row["enabled"], "unknown", json.dumps(extensions), row["excluded_dirs"]))
                libraries[row["id"]] = Library(row["id"], row["name"], row["root_path"],
                                                bool(row["enabled"]), "unknown", tuple(extensions),
                                                tuple(_json(row["excluded_dirs"].encode("utf-8"))))
            for row in payload["documents"]:
                budget.check()
                relative = str(PureWindowsPath(row["relative_path"])).replace("\\", "/")
                connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (row["id"], row["library_id"], relative, ntpath.normcase(ntpath.normpath(relative)),
                                    PureWindowsPath(relative).name, row["size_bytes"], row["modified_at"], "unknown",
                                    int(Store._in_scope(relative, libraries[row["library_id"]])),
                                    "pending", row["content_generation"] + 1,
                                    row["is_favorite"], row["last_open_requested_at"], row["encoding_override"]))
            for row in payload["tags"]:
                name = Store._tag_name(row["name"])
                connection.execute("INSERT INTO tags VALUES (?,?,?)", (row["id"], name, normalize_text(name)))
            connection.executemany("INSERT INTO document_tags VALUES (?,?)",
                                   [(row["document_id"], row["tag_id"]) for row in payload["document_tags"]])
            if payload["format_version"] == 2:
                for row in payload["smart_lists"]:
                    budget.check()
                    connection.execute("INSERT INTO smart_lists(id,name,name_key,query_json) VALUES (?,?,?,?)",
                                       tuple(row[field] for field in SMART_LIST_COLUMNS))
            # No executable/settings/physical identities are imported.
            connection.execute("INSERT INTO settings VALUES ('__maintenance.recovery_requires_reindex','1')")
            connection.execute(f"PRAGMA user_version={payload['format_version']}")
            connection.execute("COMMIT")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _failure("recovery_failed", "新管理数据库校验失败。")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise _failure("recovery_failed", "新管理数据库关联校验失败。")
        finally:
            connection.close()
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())

    def _verify_clean_database(self, path, descriptor, budget):
        manifest = self._manifest(path, budget)
        if manifest != {"metadata.db": descriptor}:
            raise _failure("recovery_blocked", "新管理数据库文件或附属文件不符合已验证状态。")
        payload, version = self._read_sqlite(path, manifest, budget)
        self._validate_payload(payload, budget)
        if (version not in (1, 2) or payload["format_version"] != version
                or payload.get("__derived_identity_count") != 0
                or payload["settings"] != [{"key": "__maintenance.recovery_requires_reindex", "value": "1"}]
                or any(row["availability"] != "unknown" for row in payload["libraries"])
                or any(row["availability"] != "unknown" or row["index_status"] != "pending"
                       for row in payload["documents"])):
            raise _failure("recovery_blocked", "新库并非清洁逻辑导入状态，拒绝激活可疑设置或索引。")

    def _write_journal(self, session, payload):
        self._plain_chain(session)
        envelope = {"payload": payload, "sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
        temporary = session / ("journal-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(_canonical(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, session / "journal.json")

    def _read_journal(self, session):
        self._safe_path(session)
        if not SESSION_NAME.fullmatch(session.name) or session.parent != self.data_dir / "recovery":
            raise _failure("recovery_blocked", "恢复会话路径不符合安全规则。")
        path = session / "journal.json"
        self._plain_chain(path)
        if path.stat().st_size > 64 * 1024:
            raise _failure("recovery_blocked", "恢复日志过大。")
        envelope = _json(path.read_bytes())
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
            raise _failure("recovery_blocked", "恢复日志格式无效。")
        value = envelope["payload"]
        if (not isinstance(value, dict) or set(value) != {"format_version", "id", "stage", "old", "new"}
                or type(value.get("format_version")) is not int or value["format_version"] != 1
                or value.get("id") != session.name or value.get("stage") not in STAGES
                or envelope["sha256"] != hashlib.sha256(_canonical(value)).hexdigest()):
            raise _failure("recovery_blocked", "恢复日志校验失败。")
        if not isinstance(value["old"], dict) or not set(value["old"]).issubset(LIVE_NAMES):
            raise _failure("recovery_blocked", "恢复日志包含不允许的文件名。")
        for descriptor in (*value["old"].values(), value["new"]):
            if (not isinstance(descriptor, dict) or set(descriptor) != {"size", "sha256"}
                    or type(descriptor["size"]) is not int or not 0 <= descriptor["size"] <= MAX_DB_BYTES
                    or not isinstance(descriptor["sha256"], str) or not HEX_DIGEST.fullmatch(descriptor["sha256"])):
                raise _failure("recovery_blocked", "恢复日志文件指纹无效。")
        return value

    def _sessions(self):
        directory = self.data_dir / "recovery"
        try:
            self._safe_path(directory)
        except FileNotFoundError:
            return []
        result = []
        for session in directory.iterdir():
            if not SESSION_NAME.fullmatch(session.name):
                continue
            self._plain_chain(session)
            journal = session / "journal.json"
            try:
                journal.lstat()
            except FileNotFoundError:
                continue  # Preparation had not published permission to alter live files.
            payload = self._read_journal(session)
            if payload["stage"] not in TERMINAL_STAGES:
                result.append((session, payload))
        return result

    @staticmethod
    def _known_state(current, journal):
        if current == {"metadata.db": journal["new"]}:
            return "new"
        if journal["stage"] in {"prepared", "sidecars_removed"}:
            expected = dict(journal["old"])
            if current == expected:
                return "old"
            for name in SIDECARS:
                expected.pop(name, None)
                if current == expected:
                    return "old"
        return None

    def _verify_protected(self, session, journal, budget):
        for name, descriptor in journal["old"].items():
            if self._digest_file(session / ("old-" + name), budget) != descriptor:
                raise _failure("recovery_blocked", "故障原件保护副本已变化，拒绝自动覆盖。")

    def _install(self, session, journal, budget, hooks, release_live=None):
        self._verify_protected(session, journal, budget)
        source = session / "new-metadata.db"
        if self._digest_file(source, budget) != journal["new"]:
            raise _failure("recovery_blocked", "待安装的新管理数据库指纹不一致。")
        # A checksum is not authentication. Even a re-signed journal cannot
        # bypass the same untrusted-SQLite and clean-state validation.
        self._verify_clean_database(source, journal["new"], budget)
        current = self._manifest(self.data_dir / "metadata.db", budget)
        if self._known_state(current, journal) != "old":
            raise _failure("state_changed", "当前数据库不再匹配已确认的旧状态。")
        for name in SIDECARS:
            path = self.data_dir / name
            if name in current:
                if self._digest_file(path, budget) != journal["old"].get(name):
                    raise _failure("state_changed", "数据库附属文件已改变，拒绝移除。")
                path.unlink()  # Exact fixed filename, already protected and verified.
        journal["stage"] = "sidecars_removed"
        self._write_journal(session, journal)
        if hooks:
            self._hook("sidecars_removed")
        current = self._manifest(self.data_dir / "metadata.db", budget)
        expected_main = ({"metadata.db": journal["old"]["metadata.db"]} if "metadata.db" in journal["old"] else {})
        if current != expected_main:
            raise _failure("state_changed", "安装前原数据库已变化，拒绝覆盖。")
        staging = session / ("install-" + uuid.uuid4().hex + ".db")
        self._copy_exact(source, staging, journal["new"], budget)
        # On Windows sharing DELETE does not guarantee MoveFileEx may replace
        # an already-open destination. Release our raw handles and immediately
        # reverify under the caller's single-instance guard before replacement.
        if release_live:
            release_live()
        if self._manifest(self.data_dir / "metadata.db", budget) != expected_main:
            raise _failure("state_changed", "安装时原数据库已变化，拒绝覆盖。")
        os.replace(staging, self.data_dir / "metadata.db")
        journal["stage"] = "metadata_installed"
        self._write_journal(session, journal)
        if hooks:
            self._hook("metadata_installed")
        if self._manifest(self.data_dir / "metadata.db", budget) != {"metadata.db": journal["new"]}:
            raise _failure("state_changed", "安装后的数据库发生未知变化。")
        journal["stage"] = "committed"
        self._write_journal(session, journal)
        if hooks:
            self._hook("committed")
        journal["stage"] = "completed"
        self._write_journal(session, journal)

    def recover(self, path, confirmed: bool, expected_current: str, expected_backup: str, cancel=None):
        if confirmed is not True:
            raise _failure("confirmation_required", "必须明确确认替换管理信息后才能恢复。")
        self._safe_path(self.data_dir, allow_missing=True)
        if self._sessions():
            raise _failure("recovery_pending", "存在尚未完成的恢复，请先重试启动恢复流程。")
        current = self.inspect_current()
        if current["status"] in {"unsupported", "busy", "inaccessible"}:
            raise _failure("unsupported" if current["status"] == "unsupported" else "database_busy",
                           current["message"])
        if not expected_current or current["signature"] != expected_current:
            raise _failure("state_changed", "当前数据库已变化，请重新检查并确认。")
        try:
            payload, candidate = self._inspect_backup(path, cancel)
        except NotesError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _failure("invalid_backup", "备份校验失败，未改动原数据。") from None
        if not expected_backup or candidate["signature"] != expected_backup:
            raise _failure("state_changed", "备份已变化，请重新检查并确认。")
        budget = _Budget(cancel)
        budget.check()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._safe_path(self.data_dir)
        parent = self.data_dir / "recovery"
        self._plain_chain(parent, allow_missing=True)
        parent.mkdir(exist_ok=True)
        session = parent / uuid.uuid4().hex
        session.mkdir()
        try:
            with self._guard_live() as release_live:
                old = self._manifest(self.data_dir / "metadata.db", budget)
                if self._signature(self.data_dir / "metadata.db", old) != expected_current:
                    raise _failure("state_changed", "保护原件前数据库已变化。")
                for name, descriptor in old.items():
                    self._copy_exact(self.data_dir / name, session / ("old-" + name), descriptor, budget)
                new_path = session / "new-metadata.db"
                self._build_database(new_path, payload, budget)
                journal = {"format_version": 1, "id": session.name, "stage": "prepared",
                           "old": old, "new": self._digest_file(new_path, budget)}
                # Re-read the selected backup fingerprint before publishing the
                # durable recovery permission, not just before making the copy.
                if self._inspect_backup(path, cancel)[1]["signature"] != expected_backup:
                    raise _failure("state_changed", "准备期间备份已变化。")
                budget.check()
                self._write_journal(session, journal)
                self._hook("prepared")
                if cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                    journal["stage"] = "rolled_back"
                    self._write_journal(session, journal)
                    raise _failure("cancelled", "恢复已取消，原数据没有改动。")
                # The commit phase finishes or leaves a resumable journal. It
                # does not respond to cancellation halfway through file install.
                self._install(session, journal, _Budget(), hooks=True, release_live=release_live)
            return dict(recovered=True, quarantine_path=str(session),
                        message="管理信息已恢复；故障原件已完整保留，编辑器需重新确认，正文索引将重建。")
        except NotesError:
            raise
        except (OSError, sqlite3.Error):
            raise _failure("recovery_failed", "恢复未完成；原件保护副本仍保留，请重试启动恢复流程。") from None

    def resume(self):
        try:
            self._safe_path(self.data_dir, allow_missing=True)
            sessions = self._sessions()
            if not sessions:
                return dict(status="none", message="没有待续恢复。")
            if len(sessions) != 1:
                return dict(status="blocked", message="发现多个未完成恢复会话，拒绝猜测覆盖。")
            session, journal = sessions[0]
            budget = _Budget()
            self._verify_protected(session, journal, budget)
            with self._guard_live() as release_live:
                current = self._manifest(self.data_dir / "metadata.db", budget)
                known = self._known_state(current, journal)
                if known is None:
                    return dict(status="blocked", message="当前数据库不是已记录的旧状态或新状态，未覆盖任何文件。")
                # A future-version live database must never be overwritten,
                # even if an edited journal otherwise claims its fingerprint.
                if "metadata.db" in current:
                    try:
                        self._read_sqlite(self.data_dir / "metadata.db", current, budget)
                    except NotesError as exc:
                        if exc.code == "unsupported":
                            return dict(status="blocked", message=exc.message)
                    except sqlite3.Error:
                        pass  # A recorded corrupt old main is an expected recovery input.
                if known == "new":
                    self._verify_clean_database(self.data_dir / "metadata.db", journal["new"], budget)
                    journal["stage"] = "completed"
                    self._write_journal(session, journal)
                    return dict(status="completed", message="已确认新管理数据库，恢复提交完成。")
                try:
                    valid_new = self._digest_file(session / "new-metadata.db", budget) == journal["new"]
                    if valid_new:
                        self._verify_clean_database(session / "new-metadata.db", journal["new"], budget)
                except (OSError, sqlite3.Error, NotesError, ValueError, TypeError, KeyError, UnicodeError):
                    valid_new = False
                if valid_new:
                    self._install(session, journal, budget, hooks=False, release_live=release_live)
                    return dict(status="completed", message="已从已验证状态完成中断的恢复。")
                # With known old bytes only, missing sidecars may safely be
                # restored from the independently verified protective copies.
                for name, descriptor in journal["old"].items():
                    if name not in current:
                        staging = session / ("rollback-" + uuid.uuid4().hex + ".tmp")
                        self._copy_exact(session / ("old-" + name), staging, descriptor, budget)
                        os.replace(staging, self.data_dir / name)
                if self._manifest(self.data_dir / "metadata.db", budget) != journal["old"]:
                    return dict(status="blocked", message="回退校验失败，保留当前文件和所有原件副本。")
                journal["stage"] = "rolled_back"
                self._write_journal(session, journal)
                return dict(status="rolled_back", message="新库不可用，已恢复已验证的旧数据库及附属文件。")
        except (NotesError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            message = exc.message if isinstance(exc, NotesError) else "恢复日志或保护副本无法安全验证，未自动覆盖。"
            return dict(status="blocked", message=message)
