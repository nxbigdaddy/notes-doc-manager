"""Local metadata, disposable full-text index, and validated logical backups.

Every connection belongs to its calling thread. All operations share a reentrant
gate so restore cannot race a reader or writer; no connection escapes a method.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

from notes import __version__
from notes.domain.models import (
    Document, FileEntry, Library, NotesError, ReadResult, SearchHit, SearchQuery,
    SearchResult, SmartList, Tag,
)
from notes.domain.smart_lists import (
    MAX_SMART_LISTS, decode_query, encode_query, validate_name, validate_query,
)

DEFAULT_EXCLUDES = (".git", ".venv", "__pycache__")
MAX_BACKUP_BYTES = 100 * 1024 * 1024
INDEX_STATUSES = {"pending", "ready", "skipped_large", "skipped_binary", "needs_encoding", "error"}
ENCODINGS = {"utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
             "utf-32", "utf-32-le", "utf-32-be", "gbk", "gb18030"}
BACKUP_STATE_KEY = "__backup.state.v1"
AUTO_BACKUP_NAME = re.compile(r"auto-\d{8}T\d{6}-[0-9a-f]{32}\.db\Z")
INDEX_SCHEMA_VERSION = 2
INDEX_TRIGGERS = ("indexed_documents_ai", "indexed_documents_ad", "indexed_documents_au")


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _key(path: str) -> str:
    # Path identities deliberately do not use text NFC/casefold normalization.
    return ntpath.normcase(ntpath.normpath(path))


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NotesError("invalid_path", "文档相对路径无效。")
    path = PureWindowsPath(value)
    if path.is_absolute() or path.drive or value.startswith(("/", "\\")):
        raise NotesError("invalid_path", "文档路径必须为相对路径。")
    parts = value.replace("\\", "/").split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
    if any(not part or part in {".", ".."} or part.endswith((" ", "."))
           or any(c in ':*?"<>|' or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in part)
           or part.split(".")[0].upper() in reserved for part in parts):
        raise NotesError("invalid_path", "文档路径不能越界或包含特殊路径。")
    return str(path).replace("\\", "/")


def _rules(extensions, excluded_dirs):
    if isinstance(extensions, str) or isinstance(excluded_dirs, str):
        raise NotesError("invalid_rules", "扩展名和排除目录应为列表。")
    extensions = tuple(dict.fromkeys(str(x).lower() for x in extensions))
    excluded_dirs = tuple(dict.fromkeys(str(x) for x in excluded_dirs))
    if not extensions or any(not x.startswith(".") or len(x) < 2
                             or any(c in x for c in '/\\:*?"<>|') for x in extensions):
        raise NotesError("invalid_rules", "请提供有效的文件扩展名。")
    if any(not x or x in {".", ".."} or any(c in x for c in '/\\:*?"<>|')
           for x in excluded_dirs):
        raise NotesError("invalid_rules", "排除规则必须为目录名。")
    return extensions, excluded_dirs


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS libraries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL,
        root_key TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
        availability TEXT NOT NULL, extensions TEXT NOT NULL, excluded_dirs TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
        relative_path TEXT NOT NULL, path_key TEXT NOT NULL, file_name TEXT NOT NULL,
        size_bytes INTEGER NOT NULL, modified_at INTEGER NOT NULL,
        availability TEXT NOT NULL, in_scope INTEGER NOT NULL,
        index_status TEXT NOT NULL, content_generation INTEGER NOT NULL,
        is_favorite INTEGER NOT NULL DEFAULT 0, last_open_requested_at INTEGER,
        encoding_override TEXT, UNIQUE(library_id,path_key))""",
    """CREATE TABLE IF NOT EXISTS tags (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS document_tags (
        document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY(document_id,tag_id))""",
    """CREATE TABLE IF NOT EXISTS document_file_identities (
        document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
        identity TEXT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS documents_library ON documents(library_id,in_scope,availability)",
    "CREATE INDEX IF NOT EXISTS documents_opened ON documents(last_open_requested_at DESC,id)",
    "CREATE INDEX IF NOT EXISTS tags_document ON document_tags(tag_id,document_id)",
)
SMART_LIST_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS smart_lists (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
    "name_key TEXT NOT NULL UNIQUE, query_json TEXT NOT NULL)"
)


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.data_dir / "metadata.db"
        # Older executables manually maintain ordinary FTS tables. Never expose
        # the new trigger-maintained external-content format to those binaries.
        self.search_path = self.data_dir / "search-v2.db"
        self._lock = threading.RLock()
        self.fts_available = False
        self.search_warning = ""
        self._backup_failure = ""
        self._last_auto_status = {}
        self._index_sessions = 0
        self.last_index_checkpoint = None
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise NotesError("schema_version", "管理数据库版本不兼容。")
            self.validate_smart_list_schema(conn, required=version == 2)
            if version < 2 and self._has_smart_table(conn) and conn.execute("SELECT 1 FROM smart_lists LIMIT 1").fetchone():
                raise NotesError("schema_version", "旧版管理数据库含未声明的智能列表，拒绝忽略数据。")
            conn.execute("PRAGMA journal_mode=WAL")
            with self._transaction(conn):
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute("PRAGMA user_version=" + str(max(1, version)))
        try:
            self._initialize_index()
        except (sqlite3.DatabaseError, NotesError):
            self.search_warning = "正文索引不可用，请重建索引；文件名搜索仍可使用。"

    @contextmanager
    def document_action_gate(self):
        """Keep a short user action's final metadata snapshot stable through launch.

        The caller must not wait for the external editor to exit, run a scan,
        read document bodies, or dispatch GUI callbacks while holding this gate.
        """
        with self._lock:
            yield

    @staticmethod
    def _has_smart_table(connection):
        return connection.execute("SELECT 1 FROM sqlite_schema WHERE name='smart_lists'").fetchone() is not None

    @staticmethod
    def validate_smart_list_schema(connection, *, required=True):
        """Read-only structural contract shared with isolated recovery readers."""
        entry = connection.execute("SELECT type,sql FROM sqlite_schema WHERE name='smart_lists'").fetchone()
        if entry is None:
            if required:
                raise NotesError("smart_list_schema", "管理数据库缺少智能列表表，不能补成空表。")
            return
        sql = entry[1] or ""
        if (entry[0] != "table" or re.search(r"\b(?:VIRTUAL|CHECK|GENERATED)\b", sql, re.IGNORECASE)
                or connection.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name='smart_lists'").fetchone()):
            raise NotesError("smart_list_schema", "智能列表表结构不受支持。")
        columns = connection.execute("PRAGMA table_xinfo(smart_lists)").fetchall()
        # Indexes here deliberately use numeric access so both ordinary SQLite
        # connections and sqlite3.Row recovery connections are supported.
        if (len(columns) != 4 or tuple(column[1] for column in columns) != ("id", "name", "name_key", "query_json")
                or any(str(column[2]).upper() != "TEXT" or column[6] != 0 or column[4] is not None for column in columns)
                or tuple(column[5] for column in columns) != (1, 0, 0, 0)
                or any(columns[index][3] != 1 for index in (1, 2, 3))
                or connection.execute("PRAGMA foreign_key_list(smart_lists)").fetchone() is not None):
            raise NotesError("smart_list_schema", "智能列表字段或约束不符合支持格式。")
        unique_name = False
        for index in connection.execute("PRAGMA index_list(smart_lists)"):
            name = index[1].replace('"', '""')
            details = connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
            if index[4] or any(row[1] == -2 or row[4] not in ("BINARY", None) for row in details):
                raise NotesError("smart_list_schema", "智能列表不支持部分、表达式或自定义排序索引。")
            key_names = tuple(row[2] for row in details if row[5])
            if index[2] and key_names == ("name_key",):
                unique_name = True
        if not unique_name:
            raise NotesError("smart_list_schema", "智能列表缺少名称唯一性约束。")

    def _smart_version(self, connection):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2):
            raise NotesError("schema_version", "管理数据库版本不兼容。")
        self.validate_smart_list_schema(connection, required=version == 2)
        if version == 1 and self._has_smart_table(connection) and connection.execute("SELECT 1 FROM smart_lists LIMIT 1").fetchone():
            raise NotesError("schema_version", "旧版管理数据库含未声明的智能列表，拒绝忽略数据。")
        return version

    @staticmethod
    def _smart_id(value):
        if type(value) is not str:
            raise NotesError("invalid_smart_list", "智能列表标识无效。")
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError
        except (ValueError, AttributeError):
            raise NotesError("invalid_smart_list", "智能列表标识无效。") from None
        return value

    @classmethod
    def _validate_smart_rows(cls, rows):
        if type(rows) is not list or len(rows) > MAX_SMART_LISTS:
            raise NotesError("invalid_smart_list", "智能列表集合格式无效或超过100项。")
        ids, names = set(), set()
        result = []
        for row in rows:
            if type(row) is not dict or set(row) != {"id", "name", "name_key", "query_json"}:
                raise NotesError("invalid_smart_list", "智能列表记录字段不完整或含未知字段。")
            identity = cls._smart_id(row["id"])
            name = validate_name(row["name"])
            if (name != row["name"] or type(row["name_key"]) is not str
                    or row["name_key"] != normalize_text(name) or identity in ids or row["name_key"] in names):
                raise NotesError("invalid_smart_list", "智能列表名称、标识或唯一性校验失败。")
            query = decode_query(row["query_json"])
            ids.add(identity)
            names.add(row["name_key"])
            result.append((row, query))
        return tuple(result)

    def _read_smart_rows(self, connection):
        if self._smart_version(connection) == 1:
            return ()
        rows = [dict(row) for row in connection.execute("SELECT * FROM smart_lists ORDER BY name_key,id LIMIT ?",
                                                       (MAX_SMART_LISTS + 1,))]
        return self._validate_smart_rows(rows)

    @staticmethod
    def _smart_reference_warning(connection, query, *, existence_only=False):
        warnings = []
        if query.library_id is not None:
            row = connection.execute("SELECT enabled,availability FROM libraries WHERE id=?", (query.library_id,)).fetchone()
            if row is None:
                warnings.append("保存的文档库已不存在")
            elif not existence_only:
                if not row[0]:
                    warnings.append("保存的文档库已停用")
                elif row[1] != "online" and not query.include_unavailable:
                    warnings.append("文档库离线或尚未确认在线")
        for tag_id in query.tag_ids:
            if connection.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone() is None:
                warnings.append("保存的标签已不存在")
                break
        return "；".join(warnings)

    def _smart_dto(self, connection, row, query):
        warning = self._smart_reference_warning(connection, query)
        return SmartList(row["id"], row["name"], query, not warning, warning)

    def list_smart_lists(self) -> tuple[SmartList, ...]:
        with self._connect() as connection:
            return tuple(self._smart_dto(connection, row, query) for row, query in self._read_smart_rows(connection))

    def create_smart_list(self, name, query: dict) -> SmartList:
        name = validate_name(name)
        parsed = validate_query(query)
        encoded = encode_query(parsed)
        with self._lock, self._connect() as connection:
            rows = self._read_smart_rows(connection)
            if len(rows) >= MAX_SMART_LISTS:
                raise NotesError("smart_list_limit", "智能列表最多100项，请先删除不需要的列表。")
            if any(row["name_key"] == normalize_text(name) for row, _ in rows):
                raise NotesError("duplicate_smart_list", "智能列表名称已存在。")
            warning = self._smart_reference_warning(connection, parsed, existence_only=True)
            if warning:
                raise NotesError("invalid_reference", warning + "，未保存智能列表。")
            version = self._smart_version(connection)
            if version == 1:
                self.backup_metadata()
            identity = str(uuid.uuid4())
            try:
                with self._transaction(connection):
                    if version == 1:
                        connection.execute(SMART_LIST_SCHEMA)
                    connection.execute("INSERT INTO smart_lists VALUES (?,?,?,?)",
                                       (identity, name, normalize_text(name), encoded))
                    if version == 1:
                        connection.execute("PRAGMA user_version=2")
            except sqlite3.IntegrityError:
                raise NotesError("duplicate_smart_list", "智能列表名称或标识冲突，未保存。") from None
            row = {"id": identity, "name": name, "name_key": normalize_text(name), "query_json": encoded}
            return self._smart_dto(connection, row, parsed)

    def update_smart_list(self, id, name, query: dict) -> SmartList:
        identity = self._smart_id(id)
        name = validate_name(name)
        parsed = validate_query(query)
        encoded = encode_query(parsed)
        with self._connect() as connection, self._transaction(connection):
            if self._smart_version(connection) != 2 or connection.execute(
                    "SELECT 1 FROM smart_lists WHERE id=?", (identity,)).fetchone() is None:
                raise NotesError("not_found", "智能列表不存在。")
            warning = self._smart_reference_warning(connection, parsed, existence_only=True)
            if warning:
                raise NotesError("invalid_reference", warning + "，未保存智能列表。")
            try:
                connection.execute("UPDATE smart_lists SET name=?,name_key=?,query_json=? WHERE id=?",
                                   (name, normalize_text(name), encoded, identity))
            except sqlite3.IntegrityError:
                raise NotesError("duplicate_smart_list", "智能列表名称已存在，未保存。") from None
            row = {"id": identity, "name": name, "name_key": normalize_text(name), "query_json": encoded}
            return self._smart_dto(connection, row, parsed)

    def delete_smart_list(self, id) -> None:
        identity = self._smart_id(id)
        with self._connect() as connection, self._transaction(connection):
            if self._smart_version(connection) != 2 or not connection.execute(
                    "DELETE FROM smart_lists WHERE id=?", (identity,)).rowcount:
                raise NotesError("not_found", "智能列表不存在。")

    def search_smart_list(self, id, page=0, page_size=100, cancel=None) -> SearchResult:
        identity = self._smart_id(id)
        with self._lock, self._connect() as connection:
            if self._smart_version(connection) != 2:
                raise NotesError("not_found", "智能列表不存在。")
            raw = connection.execute("SELECT * FROM smart_lists WHERE id=?", (identity,)).fetchone()
            if raw is None:
                raise NotesError("not_found", "智能列表不存在。")
            row, query = self._validate_smart_rows([dict(raw)])[0]
            warning = self._smart_reference_warning(connection, query)
            if warning:
                raise NotesError("smart_list_unavailable", warning + "；不会放宽已保存的筛选条件。")
            return self.search(replace(query, page=page, page_size=page_size), cancel=cancel)

    @contextmanager
    def _connect(self, path: Path | None = None):
        with self._lock:
            conn = self._new_connection(path)
            try:
                yield conn
            finally:
                conn.close()

    def _new_connection(self, path=None):
        conn = sqlite3.connect(path or self.metadata_path, timeout=5,
                               isolation_level=None, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=" + ("NORMAL" if path == self.search_path else "FULL"))
            # Every writer retains normal passive auto-checkpointing. An idle
            # anchor is not a read transaction and never pins an old WAL snapshot.
            conn.execute("PRAGMA wal_autocheckpoint=1000")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def index_session(self):
        """Keep an idle, calling-thread-owned cache connection across scan batches.

        No transaction or Store lock spans yield. Queries and other short Store
        operations continue normally; metadata durability remains FULL. Each
        writer auto-checkpoints after roughly 1000 WAL pages; session exit adds
        a passive checkpoint and closes the anchor on its creating thread.
        """
        anchor = None
        with self._lock:
            if self.fts_available:
                anchor = self._new_connection(self.search_path)
                self._index_sessions += 1
                self.last_index_checkpoint = None
        try:
            yield
        finally:
            if anchor is not None:
                with self._lock:
                    try:
                        # PASSIVE does not wait for external busy readers. A
                        # pinned external reader can exceed the normal WAL bound;
                        # no reader is killed or data deleted to hide that case.
                        self.last_index_checkpoint = tuple(anchor.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                    finally:
                        anchor.close()
                        self._index_sessions -= 1

    @staticmethod
    @contextmanager
    def _transaction(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _initialize_index(self):
        with self._connect(self.search_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > INDEX_SCHEMA_VERSION:
                raise NotesError("index_version", "搜索缓存来自更新版本；当前版本不会覆盖它。")
            conn.execute("PRAGMA journal_mode=WAL")
            entries = {r["name"]: r["sql"] for r in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
            normalizer = "nfc-casefold-nfc-v1:" + unicodedata.unidata_version
            old = (conn.execute("SELECT value FROM index_info WHERE key='normalization'").fetchone()
                   if "index_info" in entries else None)
            valid_schema = (version == INDEX_SCHEMA_VERSION
                            and "content='indexed_documents'" in (entries.get("document_fts") or "")
                            and all(name in entries for name in INDEX_TRIGGERS)
                            and old is not None and old[0] == normalizer)
            if not valid_schema:
                # Publish invalidation BEFORE creating the new format. A crash
                # at either boundary can only cause an extra rebuild, never
                # revive an older executable's same-generation cache.
                with self._connect() as metadata, self._transaction(metadata):
                    metadata.execute("UPDATE documents SET index_status='pending',content_generation=content_generation+1")
                self._reset_index_schema(conn)
            conn.execute("SELECT rowid FROM document_fts WHERE document_fts MATCH ?", ('"中文测试"',)).fetchall()
        self.fts_available = True
        self.search_warning = ""
        # Repair interrupted two-database writes and missing/deleted cache on startup.
        with self._connect() as meta, self._connect(self.search_path) as index:
            valid = {r["document_id"]: r["generation"] for r in index.execute(
                "SELECT document_id,generation FROM indexed_documents")}
            with self._transaction(meta):
                for row in meta.execute("SELECT id,content_generation FROM documents WHERE index_status='ready'"):
                    if valid.get(row["id"]) != row["content_generation"]:
                        meta.execute("UPDATE documents SET index_status='pending',content_generation=content_generation+1 WHERE id=?", (row["id"],))

    def _reset_index_schema(self, conn):
        """Atomically recreate this version's disposable cache, never metadata."""
        with self._transaction(conn):
            for trigger in INDEX_TRIGGERS:
                conn.execute("DROP TRIGGER IF EXISTS " + trigger)
            conn.execute("DROP TABLE IF EXISTS document_fts")
            conn.execute("DROP TABLE IF EXISTS indexed_documents")
            conn.execute("DROP TABLE IF EXISTS index_info")
            conn.execute("""CREATE TABLE indexed_documents (
                row_id INTEGER PRIMARY KEY, document_id TEXT UNIQUE NOT NULL,
                generation INTEGER NOT NULL, body TEXT NOT NULL, encoding TEXT NOT NULL)""")
            conn.execute("CREATE INDEX indexed_generation ON indexed_documents(document_id,generation)")
            conn.execute("""CREATE VIRTUAL TABLE document_fts USING fts5(
                body,content='indexed_documents',content_rowid='row_id',tokenize='trigram')""")
            conn.execute("""CREATE TRIGGER indexed_documents_ai AFTER INSERT ON indexed_documents BEGIN
                INSERT INTO document_fts(rowid,body) VALUES (new.row_id,new.body); END""")
            conn.execute("""CREATE TRIGGER indexed_documents_ad AFTER DELETE ON indexed_documents BEGIN
                INSERT INTO document_fts(document_fts,rowid,body) VALUES ('delete',old.row_id,old.body); END""")
            conn.execute("""CREATE TRIGGER indexed_documents_au AFTER UPDATE OF body,row_id ON indexed_documents BEGIN
                INSERT INTO document_fts(document_fts,rowid,body) VALUES ('delete',old.row_id,old.body);
                INSERT INTO document_fts(rowid,body) VALUES (new.row_id,new.body); END""")
            conn.execute("CREATE TABLE index_info (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO index_info VALUES ('normalization',?)",
                         ("nfc-casefold-nfc-v1:" + unicodedata.unidata_version,))
            conn.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")

    @staticmethod
    def _library(row):
        return Library(row["id"], row["name"], row["root_path"], bool(row["enabled"]),
                       row["availability"], tuple(json.loads(row["extensions"])),
                       tuple(json.loads(row["excluded_dirs"])))

    @staticmethod
    def _document(conn, row, tags=None):
        if tags is None:
            tags = tuple(Tag(r["id"], r["name"]) for r in conn.execute(
                "SELECT t.id,t.name FROM tags t JOIN document_tags dt ON t.id=dt.tag_id "
                "WHERE dt.document_id=? ORDER BY t.name_key", (row["id"],)))
        return Document(**{k: (bool(row[k]) if k in {"in_scope", "is_favorite"} else row[k])
                           for k in Document.__dataclass_fields__ if k != "tags"}, tags=tags)

    def list_libraries(self):
        with self._connect() as conn:
            return tuple(self._library(r) for r in conn.execute("SELECT * FROM libraries ORDER BY name,id"))

    def get_library(self, library_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
            if row is None:
                raise NotesError("not_found", "文档库不存在。")
            return self._library(row)

    def add_library(self, root_path, name="", extensions=(".txt", ".md"), excluded_dirs=DEFAULT_EXCLUDES):
        self._check_root_boundary(str(root_path))
        path = Path(root_path).absolute()
        if not path.is_dir() or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise NotesError("invalid_root", "请选择可访问且非链接的目录。")
        path = path.resolve()
        self._check_root_boundary(str(path))
        extensions, excluded_dirs = _rules(extensions, excluded_dirs)
        with self._lock:
            self._check_overlap(str(path), self.list_libraries())
            library = Library(str(uuid.uuid4()), name.strip() or path.name or str(path),
                              str(path), True, "unknown", extensions, excluded_dirs)
            with self._connect() as conn:
                conn.execute("INSERT INTO libraries VALUES (?,?,?,?,?,?,?,?)",
                             (library.id, library.name, library.root_path, _key(library.root_path),
                              1, "unknown", json.dumps(extensions), json.dumps(excluded_dirs)))
            return library

    @staticmethod
    def _check_overlap(path, libraries):
        for library in libraries:
            try:
                common = ntpath.commonpath((_key(path), _key(library.root_path)))
            except ValueError:
                continue
            if common in {_key(path), _key(library.root_path)}:
                raise NotesError("overlapping_library", "文档库目录不能重复或相互包含。")

    def _check_root_boundary(self, path):
        """Backups may refer to offline local roots, never UNC/device/app data."""
        if not isinstance(path, str) or "\x00" in path or path.replace("/", "\\").startswith("\\\\"):
            raise NotesError("invalid_root", "不支持网络共享、设备路径或无效文档库目录。")
        _, tail = ntpath.splitdrive(path)
        relative = tail.strip("\\/")
        if relative:
            _relative(relative)
        self._check_overlap(path, (Library("application-data", "应用数据", str(self.data_dir)),))

    def set_library_enabled(self, library_id, enabled):
        with self._lock:
            self.get_library(library_id)
            with self._connect() as conn, self._transaction(conn):
                conn.execute("UPDATE libraries SET enabled=? WHERE id=?", (int(bool(enabled)), library_id))
                if not enabled:
                    conn.execute("UPDATE documents SET index_status='pending',content_generation=content_generation+1 WHERE library_id=?",
                                 (library_id,))
                    conn.execute("""DELETE FROM document_file_identities WHERE document_id IN
                        (SELECT id FROM documents WHERE library_id=?)""", (library_id,))
            if not enabled and self.fts_available:
                try:
                    document_ids = {d.id for d in self.list_documents(library_id)}
                    with self._connect(self.search_path) as conn, self._transaction(conn):
                        for row in conn.execute("SELECT row_id,document_id FROM indexed_documents").fetchall():
                            if row["document_id"] in document_ids:
                                conn.execute("DELETE FROM indexed_documents WHERE row_id=?", (row["row_id"],))
                except sqlite3.DatabaseError:
                    self.fts_available = False
                    self.search_warning = "正文索引不可用；停用库的缓存清理未完成，请重建索引。"

    def update_library_rules(self, library_id, extensions, excluded_dirs):
        extensions, excluded_dirs = _rules(extensions, excluded_dirs)
        self.get_library(library_id)
        with self._connect() as conn, self._transaction(conn):
            conn.execute("UPDATE libraries SET extensions=?,excluded_dirs=? WHERE id=?",
                         (json.dumps(extensions), json.dumps(excluded_dirs), library_id))
            library = self.get_library(library_id)  # separate connection cannot see transaction
            library = Library(library.id, library.name, library.root_path, library.enabled,
                              library.availability, extensions, excluded_dirs)
            for row in conn.execute("SELECT id,relative_path,in_scope FROM documents WHERE library_id=?", (library_id,)):
                scope = self._in_scope(row["relative_path"], library)
                if scope != bool(row["in_scope"]):
                    conn.execute("UPDATE documents SET in_scope=?,content_generation=content_generation+1,index_status='pending' WHERE id=?",
                                 (int(scope), row["id"]))
                if not scope:
                    self._set_file_identity(conn, row["id"], "")

    @staticmethod
    def _in_scope(relative, library):
        path = PureWindowsPath(relative)
        return path.suffix.lower() in library.extensions and not any(
            _key(part) in {_key(x) for x in library.excluded_dirs} for part in path.parts[:-1])

    @staticmethod
    def _valid_file_identity(identity):
        # Opaque adapter-derived identity, never a filename/content-hash guess.
        # Invalid/unknown adapter data removes old evidence rather than retaining
        # a possibly misleading identity.
        return identity if (isinstance(identity, str) and 0 < len(identity) <= 256
                            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in identity)) else ""

    @classmethod
    def _set_file_identity(cls, conn, document_id, identity):
        identity = cls._valid_file_identity(identity)
        if identity:
            conn.execute("""INSERT INTO document_file_identities(document_id,identity) VALUES (?,?)
                ON CONFLICT(document_id) DO UPDATE SET identity=excluded.identity""",
                         (document_id, identity))
        else:
            conn.execute("DELETE FROM document_file_identities WHERE document_id=?", (document_id,))

    @classmethod
    def _get_file_identity(cls, conn, document_id):
        row = conn.execute("SELECT identity FROM document_file_identities WHERE document_id=?", (document_id,)).fetchone()
        return cls._valid_file_identity(row[0]) if row else ""

    def apply_scan(self, library_id, entries: tuple[FileEntry, ...], complete: bool, availability="online"):
        if availability not in {"online", "offline", "inaccessible", "unknown"}:
            raise NotesError("invalid_status", "文档库状态无效。")
        with self._lock:
            library = self.get_library(library_id)
            seen = set()
            with self._connect() as conn, self._transaction(conn):
                conn.execute("UPDATE libraries SET availability=? WHERE id=?", (availability, library_id))
                for entry in entries:
                    relative = _relative(entry.relative_path)
                    key = _key(relative)
                    if key in seen:
                        raise NotesError("path_conflict", "扫描中发现重复文件路径。")
                    seen.add(key)
                    if entry.size_bytes < 0 or entry.modified_at < 0:
                        raise NotesError("invalid_entry", "文件元信息无效。")
                    row = conn.execute("SELECT * FROM documents WHERE library_id=? AND path_key=?",
                                       (library_id, key)).fetchone()
                    scope = int(self._in_scope(relative, library))
                    identity = (self._valid_file_identity(entry.file_identity) if library.enabled and scope else "")
                    if row is None:
                        document_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)",
                                     (document_id, library_id, relative, key, PureWindowsPath(relative).name,
                                      entry.size_bytes, entry.modified_at, "present", scope, "pending", 1))
                    else:
                        document_id = row["id"]
                        previous_identity = self._get_file_identity(conn, document_id)
                        changed = (row["size_bytes"] != entry.size_bytes or row["modified_at"] != entry.modified_at
                                   or row["availability"] != "present" or row["in_scope"] != scope
                                   or bool(previous_identity and previous_identity != identity))
                        conn.execute("""UPDATE documents SET relative_path=?,file_name=?,size_bytes=?,modified_at=?,
                            availability='present',in_scope=?,content_generation=content_generation+?,
                            index_status=CASE WHEN ? THEN 'pending' ELSE index_status END WHERE id=?""",
                                     (relative, PureWindowsPath(relative).name, entry.size_bytes, entry.modified_at,
                                      scope, int(changed), int(changed), row["id"]))
                    self._set_file_identity(conn, document_id, identity)
                if complete and availability == "online":
                    for row in conn.execute("SELECT id,path_key,availability,in_scope FROM documents WHERE library_id=?", (library_id,)):
                        if row["path_key"] not in seen and row["availability"] != "missing" and row["in_scope"]:
                            conn.execute("UPDATE documents SET availability='missing',content_generation=content_generation+1,index_status='pending' WHERE id=?", (row["id"],))
            return self.list_documents(library_id)

    def get_document(self, document_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None:
                raise NotesError("not_found", "文档不存在。")
            return self._document(conn, row)

    def list_documents(self, library_id=None):
        with self._connect() as conn:
            parameters = (library_id,) if library_id else ()
            rows = conn.execute("SELECT * FROM documents" + (" WHERE library_id=?" if library_id else "")
                                + " ORDER BY modified_at DESC,id", parameters).fetchall()
            # Bulk-load associations instead of issuing a query for every
            # document in a 10k-file snapshot.
            tags_by_document = {}
            sql = ("SELECT dt.document_id,t.id,t.name FROM document_tags dt "
                   "JOIN tags t ON dt.tag_id=t.id JOIN documents d ON dt.document_id=d.id"
                   + (" WHERE d.library_id=?" if library_id else "") + " ORDER BY t.name_key")
            for tag in conn.execute(sql, parameters):
                tags_by_document.setdefault(tag["document_id"], []).append(Tag(tag["id"], tag["name"]))
            return tuple(self._document(conn, row, tuple(tags_by_document.get(row["id"], ()))) for row in rows)

    def set_index(self, document_id, generation, read: ReadResult):
        return self.set_index_batch(((document_id, generation, read),))[0]

    def set_index_batch(self, items):
        """Commit bounded read batches without per-document WAL checkpoints.

        The caller reads files before invoking this method. Generation/source
        checks happen under the Store gate; one transaction updates the entire
        search batch, followed by a separate metadata transaction. Interrupted
        metadata publication remains pending and is never considered covered.
        """
        items = tuple(items)
        if not items:
            return ()
        if len({item[0] for item in items}) != len(items):
            raise NotesError("duplicate_document", "索引批次不能包含重复文档。")
        if any(read.status not in INDEX_STATUSES for _, _, read in items):
            raise NotesError("invalid_status", "索引状态无效。")
        with self._lock, self._connect() as metadata:
            accepted = []
            result = []
            for document_id, generation, read in items:
                row = metadata.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
                if row is None:
                    raise NotesError("not_found", "文档不存在。")
                valid = row["content_generation"] == generation and not (
                    read.status == "ready" and (read.truncated or read.size_bytes != row["size_bytes"]
                                               or read.modified_at != row["modified_at"]))
                result.append(valid)
                if valid:
                    accepted.append((document_id, generation, read))
            if not accepted:
                return tuple(result)
            statuses = {document_id: read.status for document_id, _, read in accepted}
            if self.fts_available:
                try:
                    with self._connect(self.search_path) as conn, self._transaction(conn):
                        for document_id, generation, read in accepted:
                            old = conn.execute("SELECT row_id FROM indexed_documents WHERE document_id=?", (document_id,)).fetchone()
                            if old:
                                conn.execute("DELETE FROM indexed_documents WHERE row_id=?", (old[0],))
                            if read.status == "ready":
                                text = normalize_text(read.text.replace("\r\n", "\n").replace("\r", "\n"))
                                conn.execute("INSERT INTO indexed_documents(document_id,generation,body,encoding) VALUES (?,?,?,?)",
                                             (document_id, generation, text, read.encoding))
                except sqlite3.DatabaseError as exc:
                    if self._is_database_busy(exc):
                        # The cache transaction has rolled back (or never
                        # began). A transient lock must not disable a healthy
                        # cache or publish the pending batch as index errors.
                        raise NotesError(
                            "database_busy", "正文索引数据库正忙，本次索引未完成，请稍后刷新重试。"
                        ) from None
                    self.fts_available = False
                    self.search_warning = "正文索引不可用，请重建索引。"
                    statuses = {document_id: "error" for document_id in statuses}
            else:
                statuses = {document_id: "error" if status == "ready" else status
                            for document_id, status in statuses.items()}
            with self._transaction(metadata):
                metadata.executemany("UPDATE documents SET index_status=? WHERE id=?",
                                     [(status, document_id) for document_id, status in statuses.items()])
            return tuple(result)

    def invalidate_document(self, document_id):
        return self.invalidate_documents((document_id,))[0]

    def invalidate_documents(self, document_ids):
        document_ids = tuple(dict.fromkeys(document_ids))
        with self._connect() as conn, self._transaction(conn):
            for document_id in document_ids:
                if not conn.execute("UPDATE documents SET content_generation=content_generation+1,index_status='pending' WHERE id=?",
                                    (document_id,)).rowcount:
                    raise NotesError("not_found", "文档不存在。")
            return tuple(self._document(conn, conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())
                         for document_id in document_ids)

    def _relocation_plan(self, conn, document_id, target_relative):
        from notes.infrastructure.files import validate_document_path, validate_library_root

        row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise NotesError("not_found", "文档不存在。")
        document = self._document(conn, row)
        library = self._library(conn.execute("SELECT * FROM libraries WHERE id=?", (document.library_id,)).fetchone())
        if not library.enabled:
            raise NotesError("library_disabled", "请先启用文档库。")
        target_relative = _relative(target_relative)
        if _key(target_relative) == _key(document.relative_path):
            raise NotesError("same_path", "目标路径与原路径相同。")
        if not self._in_scope(target_relative, library):
            raise NotesError("out_of_scope", "目标文件不符合当前扩展名或排除目录规则。")
        root = validate_library_root(library.root_path, self.data_dir)
        try:
            validate_document_path(root, document.relative_path)
        except NotesError as exc:
            if exc.code != "missing":
                raise NotesError("old_path_unavailable", "无法确认原文件确实缺失，请检查原路径和访问权限。") from exc
        else:
            raise NotesError("old_path_exists", "原文件仍然存在，不能重新定位。")
        target = validate_document_path(root, target_relative)
        try:
            info = target.lstat()
        except OSError as exc:
            raise NotesError("target_unavailable", "目标文件不可用，请重新检查。") from exc
        attrs = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISREG(info.st_mode) or attrs & (0x400 | 0x1000 | 0x40000 | 0x400000):
            raise NotesError("target_unavailable", "目标必须是本地普通文件，不能是链接或云占位文件。")
        target_relative = target.relative_to(root).as_posix()
        target_row = conn.execute("SELECT * FROM documents WHERE library_id=? AND path_key=?",
                                  (library.id, _key(target_relative))).fetchone()
        target_document = self._document(conn, target_row) if target_row else None
        if target_document and (target_document.tags or target_document.is_favorite
                                or target_document.last_open_requested_at is not None
                                or target_document.encoding_override):
            raise NotesError("annotated_target", "目标已有关联标签、收藏、打开记录或编码设置，不能合并。")
        return document, library, target_relative, target_document, info

    def inspect_relocation(self, document_id, target_relative):
        """Inspect a metadata-only relink; no file is moved, renamed, or written."""
        with self._connect() as conn:
            document, library, relative, target, info = self._relocation_plan(conn, document_id, target_relative)
            return {
                "document_id": document.id, "library_id": library.id,
                "old_relative": document.relative_path, "target_relative": relative,
                "target_document_id": target.id if target else None,
                "requires_merge_confirmation": target is not None,
                "size_bytes": info.st_size, "modified_at": info.st_mtime_ns // 1_000_000,
                "message": ("目标已被扫描登记；确认后仅吸收无人工信息的占位记录，保留原文档身份和标签。"
                            if target else "仅更新文档路径关联，不移动或修改任何文件。"),
            }

    def relocate_document(self, document_id, target_relative, merge_unannotated=False):
        from notes.infrastructure.files import get_file_entry

        with self._lock:
            removed_ids = [document_id]
            with self._connect() as conn, self._transaction(conn):
                plan = self._relocation_plan(conn, document_id, target_relative)
                if plan[3] is not None and merge_unannotated is not True:
                    raise NotesError("merge_confirmation_required", "目标已有无人工信息的占位记录，需要明确确认后才能合并。")
                # Repeat filesystem checks under the same write transaction
                # immediately before modifying identities or deleting a placeholder.
                document, library, relative, target, info = self._relocation_plan(conn, document_id, target_relative)
                current_entry = get_file_entry(Path(library.root_path), relative)
                if target is not None:
                    if merge_unannotated is not True:
                        raise NotesError("merge_confirmation_required", "需要确认占位记录合并。")
                    conn.execute("DELETE FROM documents WHERE id=?", (target.id,))
                    removed_ids.append(target.id)
                conn.execute("""UPDATE documents SET relative_path=?,path_key=?,file_name=?,size_bytes=?,
                    modified_at=?,availability='present',in_scope=1,index_status='pending',
                    content_generation=content_generation+1 WHERE id=?""",
                             (relative, _key(relative), PureWindowsPath(relative).name, current_entry.size_bytes,
                              current_entry.modified_at, document.id))
                conn.execute("UPDATE libraries SET availability='online' WHERE id=?", (library.id,))
                self._set_file_identity(conn, document.id, current_entry.file_identity)
            # Metadata/generation publication is authoritative. Cache cleanup
            # failure must not revive hits or undo an already successful relink.
            self._purge_relocated_cache(removed_ids)
            return self.get_document(document_id)

    def _purge_relocated_cache(self, document_ids):
        if self.fts_available:
            try:
                with self._connect(self.search_path) as index, self._transaction(index):
                    index.executemany("DELETE FROM indexed_documents WHERE document_id=?",
                                      ((document_id,) for document_id in document_ids))
            except sqlite3.DatabaseError:
                self.fts_available = False
                self.search_warning = "路径关联已更新；旧索引清理失败，请重建索引。"

    def _observed_move_plan(self, conn, library_id, source_relative, target_relative, target_entry):
        from notes.infrastructure.files import get_file_entry

        if not isinstance(target_entry, FileEntry):
            return None
        source_relative = _relative(source_relative)
        target_relative = _relative(target_relative)
        if _key(source_relative) == _key(target_relative):
            return None
        if _key(_relative(target_entry.relative_path)) != _key(target_relative):
            return None
        hint_identity = self._valid_file_identity(target_entry.file_identity)
        if not hint_identity:
            return None
        row = conn.execute("SELECT * FROM documents WHERE library_id=? AND path_key=?",
                           (library_id, _key(source_relative))).fetchone()
        if row is None or not row["in_scope"]:
            return None
        source_identity = self._get_file_identity(conn, row["id"])
        if not source_identity or source_identity != hint_identity:
            return None
        document, library, relative, target, _ = self._relocation_plan(conn, row["id"], target_relative)
        if not self._in_scope(document.relative_path, library):
            return None
        actual = get_file_entry(Path(library.root_path), relative)
        if (not self._valid_file_identity(actual.file_identity)
                or actual.file_identity != source_identity
                or _key(_relative(actual.relative_path)) != _key(relative)
                or actual.size_bytes != target_entry.size_bytes
                or actual.modified_at != target_entry.modified_at):
            return None
        if target is not None and self._get_file_identity(conn, target.id) != source_identity:
            return None
        return document, library, relative, target, actual

    def apply_observed_move(self, library_id, source_relative, target_relative, target_entry: FileEntry):
        """Apply a caller-gated live move only with matching real file identity.

        This never moves, deletes or writes source documents. Unproven events are
        benign no-ops so the service can fall back to an ordinary compensating
        scan; database failures still propagate rather than pretending success.
        """
        with self._lock:
            try:
                with self._connect() as conn, self._transaction(conn):
                    first = self._observed_move_plan(conn, library_id, source_relative, target_relative, target_entry)
                    if first is None:
                        return None
                    # Re-run current filesystem and annotation checks inside the
                    # same write transaction immediately before changing records.
                    plan = self._observed_move_plan(conn, library_id, source_relative, target_relative, target_entry)
                    if plan is None:
                        return None
                    document, library, relative, target, actual = plan
                    if (document.id != first[0].id
                            or (target.id if target else None) != (first[3].id if first[3] else None)):
                        return None
                    removed_ids = [document.id]
                    if target is not None:
                        conn.execute("DELETE FROM documents WHERE id=?", (target.id,))
                        removed_ids.append(target.id)
                    conn.execute("""UPDATE documents SET relative_path=?,path_key=?,file_name=?,size_bytes=?,
                        modified_at=?,availability='present',in_scope=1,index_status='pending',
                        content_generation=content_generation+1 WHERE id=?""",
                                 (relative, _key(relative), PureWindowsPath(relative).name, actual.size_bytes,
                                  actual.modified_at, document.id))
                    self._set_file_identity(conn, document.id, actual.file_identity)
                    conn.execute("UPDATE libraries SET availability='online' WHERE id=?", (library.id,))
            except (NotesError, OSError):
                return None
            self._purge_relocated_cache(removed_ids)
            return self.get_document(document.id)

    def list_tags(self):
        with self._connect() as conn:
            return tuple(Tag(r["id"], r["name"]) for r in conn.execute("SELECT * FROM tags ORDER BY name_key"))

    @staticmethod
    def _tag_name(name):
        if not isinstance(name, str):
            raise NotesError("invalid_tag", "标签名称无效。")
        name = unicodedata.normalize("NFC", name.strip())
        if not 1 <= len(name) <= 50 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name):
            raise NotesError("invalid_tag", "标签名称应为 1 至 50 个有效字符。")
        return name

    def create_tag(self, name):
        name = self._tag_name(name)
        tag = Tag(str(uuid.uuid4()), name)
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO tags VALUES (?,?,?)", (tag.id, name, normalize_text(name)))
        except sqlite3.IntegrityError:
            raise NotesError("duplicate_tag", "标签名称已存在。") from None
        return tag

    def rename_tag(self, tag_id, name):
        name = self._tag_name(name)
        try:
            with self._connect() as conn:
                if not conn.execute("UPDATE tags SET name=?,name_key=? WHERE id=?", (name, normalize_text(name), tag_id)).rowcount:
                    raise NotesError("not_found", "标签不存在。")
        except sqlite3.IntegrityError:
            raise NotesError("duplicate_tag", "标签名称已存在。") from None

    def delete_tag(self, tag_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))

    def set_document_tags(self, document_id, tag_ids):
        self.get_document(document_id)
        try:
            with self._connect() as conn, self._transaction(conn):
                conn.execute("DELETE FROM document_tags WHERE document_id=?", (document_id,))
                conn.executemany("INSERT INTO document_tags VALUES (?,?)",
                                 [(document_id, tag_id) for tag_id in dict.fromkeys(tag_ids)])
        except sqlite3.IntegrityError:
            raise NotesError("not_found", "指定的文档或标签不存在。") from None

    def set_favorite(self, document_id, value):
        self.get_document(document_id)
        with self._connect() as conn:
            conn.execute("UPDATE documents SET is_favorite=? WHERE id=?", (int(bool(value)), document_id))

    def mark_opened(self, document_id):
        self.get_document(document_id)
        with self._connect() as conn:
            conn.execute("UPDATE documents SET last_open_requested_at=? WHERE id=?", (time.time_ns() // 1_000_000, document_id))

    def set_encoding(self, document_id, encoding):
        if encoding and encoding not in ENCODINGS:
            raise NotesError("invalid_encoding", "不支持所选编码。")
        with self._lock:
            self.get_document(document_id)
            with self._connect() as conn:
                conn.execute("UPDATE documents SET encoding_override=?,content_generation=content_generation+1,index_status='pending' WHERE id=?",
                             (encoding or None, document_id))

    def get_setting(self, key, default=""):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        if not isinstance(key, str) or not isinstance(value, str) or len(key) > 100 or len(value) > 100_000:
            raise NotesError("invalid_setting", "设置格式无效。")
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def search(self, query: SearchQuery, cancel=None):
        if not isinstance(query.text, str) or not isinstance(query.folder, str):
            raise NotesError("invalid_query", "搜索词和文件夹必须是文本。")
        raw = query.text.strip()
        if len(raw) > 256 or any(0xD800 <= ord(c) <= 0xDFFF for c in raw):
            raise NotesError("invalid_query", "搜索词最多 256 个有效字符。")
        if not isinstance(query.mode, str) or query.mode not in {"all", "name", "content", "filename", "body"}:
            raise NotesError("invalid_query", "搜索模式无效。")
        if (type(query.page) is not int or type(query.page_size) is not int
                or query.page < 0 or not 1 <= query.page_size <= 1000
                or query.page > (2**63 - 1) // query.page_size):
            raise NotesError("invalid_query", "分页参数无效。")
        if (not isinstance(query.tag_ids, (tuple, list)) or len(query.tag_ids) > 1000
                or any(not isinstance(tag, str) or not 1 <= len(tag) <= 128
                       or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in tag)
                       for tag in query.tag_ids)):
            raise NotesError("invalid_query", "标签筛选应为最多 1000 个有效文本标识。")
        if query.library_id is not None and (not isinstance(query.library_id, str) or len(query.library_id) > 128):
            raise NotesError("invalid_query", "文档库标识无效。")
        if any(type(flag) is not bool for flag in (query.favorites_only, query.recent_only, query.include_unavailable)):
            raise NotesError("invalid_query", "筛选开关必须是布尔值。")
        needle = normalize_text(raw)
        deadline = time.monotonic() + 10
        def stopped():
            return time.monotonic() >= deadline or bool(
                cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))
        with self._lock:
            if stopped():
                return SearchResult(complete=False, warning="查询已取消或超时，当前为部分结果。")
            try:
                return self._search_sql(query, needle, stopped, self.fts_available, self.search_warning)
            except sqlite3.DatabaseError as exc:
                if self._is_database_busy(exc):
                    raise NotesError("database_busy", "数据库正忙，请稍后重试。") from None
                # A broken cache never prevents metadata-only navigation. Do
                # not label missing body results as a completed full-text query.
                if not self.fts_available:
                    raise NotesError("database_error", "管理数据库查询失败，请检查数据库。") from None
                self.fts_available = False
                self.search_warning = "正文索引查询失败，请重建索引；目前仅有文件名结果。"
                try:
                    return self._search_sql(query, needle, stopped, False, self.search_warning)
                except sqlite3.DatabaseError as exc:
                    if self._is_database_busy(exc):
                        raise NotesError("database_busy", "数据库正忙，请稍后重试。") from None
                    raise NotesError("database_error", "管理数据库查询失败，请检查数据库。") from None

    @staticmethod
    def _is_database_busy(exc):
        code = getattr(exc, "sqlite_errorcode", 0)
        return (code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                or str(exc).lower() in {"database is locked", "database table is locked"})

    def _search_sql(self, query, needle, stopped, use_cache, warning):
        """One read snapshot; SQL filters/materializes IDs, only a page enters Python."""
        conditions = ["l.enabled=1", "d.in_scope=1"]
        values = [needle, needle]
        if not query.include_unavailable:
            conditions += ["l.availability='online'", "d.availability='present'"]
        if query.library_id:
            conditions.append("d.library_id=?")
            values.append(query.library_id)
        if query.folder:
            prefix = _key(_relative(query.folder.rstrip("/\\"))) + "\\"
            conditions.append("substr(d.path_key,1,?)=?")
            values.extend((len(prefix), prefix))
        if query.favorites_only:
            conditions.append("d.is_favorite=1")
        if query.recent_only:
            conditions.append("d.last_open_requested_at IS NOT NULL")
        selected_tags = tuple(dict.fromkeys(query.tag_ids))
        if selected_tags:
            conditions.append("""d.id IN (SELECT document_id FROM document_tags
                WHERE tag_id IN (SELECT id FROM filter_tags) GROUP BY document_id HAVING count(*)=?)""")
            values.append(len(selected_tags))
        scope_order = " ORDER BY d.last_open_requested_at DESC,d.id LIMIT 100" if query.recent_only else ""
        index_column = "i.row_id" if use_cache else "NULL"
        index_join = (""" LEFT JOIN cache.indexed_documents i INDEXED BY indexed_generation
            ON i.document_id=d.id AND i.generation=d.content_generation AND d.index_status='ready'"""
                      if use_cache else "")
        total = covered = scoped = 0
        hits = []
        needs_body = bool(needle) and query.mode not in {"name", "filename"}
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.create_function("notes_normalize", 1, normalize_text, deterministic=True)
                conn.set_progress_handler(lambda: int(stopped()), 1000)
                if use_cache:
                    conn.execute("ATTACH DATABASE ? AS cache", (str(self.search_path),))
                conn.execute("BEGIN")
                if selected_tags:
                    conn.execute("CREATE TEMP TABLE filter_tags(id TEXT PRIMARY KEY) WITHOUT ROWID")
                    conn.executemany("INSERT INTO filter_tags VALUES (?)", ((tag,) for tag in selected_tags))
                conn.execute("""CREATE TEMP TABLE scope(
                    id TEXT PRIMARY KEY, modified_at INTEGER, opened_at INTEGER,
                    index_row INTEGER, matched_name INTEGER) WITHOUT ROWID""")
                conn.execute(f"""INSERT INTO scope SELECT d.id,d.modified_at,d.last_open_requested_at,
                    {index_column},CASE WHEN ?!='' THEN instr(notes_normalize(d.file_name),?)>0 ELSE 0 END
                    FROM documents d JOIN libraries l ON l.id=d.library_id {index_join}
                    WHERE {" AND ".join(conditions)} {scope_order}""", values)
                conn.execute("CREATE INDEX scope_index_row ON scope(index_row)")
                scoped, covered = conn.execute("SELECT count(*),count(index_row) FROM scope").fetchone()
                conn.execute("CREATE TEMP TABLE matches(id TEXT PRIMARY KEY,body_match INTEGER NOT NULL) WITHOUT ROWID")
                if not needle:
                    conn.execute("INSERT INTO matches SELECT id,0 FROM scope")
                elif query.mode not in {"content", "body"}:
                    conn.execute("INSERT INTO matches SELECT id,0 FROM scope WHERE matched_name=1")
                if needs_body and use_cache:
                    if len(needle) >= 3 and "\x00" not in needle:
                        phrase = '"' + needle.replace('"', '""') + '"'
                        conn.execute("""INSERT OR REPLACE INTO matches
                            SELECT s.id,1 FROM cache.document_fts
                            JOIN scope s ON s.index_row=document_fts.rowid
                            JOIN cache.indexed_documents i ON i.row_id=s.index_row
                            WHERE document_fts MATCH ? AND instr(i.body,?)>0""", (phrase, needle))
                    else:
                        conn.execute("""INSERT OR REPLACE INTO matches SELECT s.id,1 FROM scope s
                            JOIN cache.indexed_documents i ON i.row_id=s.index_row WHERE instr(i.body,?)>0""",
                                     (needle,))
                total = conn.execute("SELECT count(*) FROM matches").fetchone()[0]
                ordering = ("s.opened_at DESC,s.id" if query.recent_only else
                            "s.matched_name DESC,s.modified_at DESC,s.id")
                rows = conn.execute(f"""SELECT d.*,s.matched_name,s.index_row,m.body_match FROM matches m
                    JOIN scope s ON s.id=m.id JOIN documents d ON d.id=m.id
                    ORDER BY {ordering} LIMIT ? OFFSET ?""",
                                    (query.page_size, query.page * query.page_size)).fetchall()
                tags = {}
                for start in range(0, len(rows), 500):
                    ids = [row["id"] for row in rows[start:start + 500]]
                    marks = ",".join("?" for _ in ids)
                    for tag in conn.execute(
                        f"""SELECT dt.document_id,t.id,t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id
                        WHERE dt.document_id IN ({marks}) ORDER BY t.name_key""", ids):
                        tags.setdefault(tag["document_id"], []).append(Tag(tag["id"], tag["name"]))
                for row in rows:
                    if stopped():
                        return SearchResult(tuple(hits), total, covered, scoped - covered, False,
                                            "查询已取消或超时，当前为部分结果。")
                    document = self._document(conn, row, tuple(tags.get(row["id"], ())))
                    snippet = ""
                    line = None
                    if row["body_match"] and use_cache:
                        body = conn.execute("SELECT body FROM cache.indexed_documents WHERE row_id=?",
                                            (row["index_row"],)).fetchone()[0]
                        offset = body.find(needle)
                        snippet = body[max(0, offset - 40):offset + len(needle) + 100].replace("\n", " ")
                        line = body.count("\n", 0, offset) + 1
                    hits.append(SearchHit(document, snippet, line, bool(row["matched_name"])))
                conn.execute("COMMIT")
                return SearchResult(tuple(hits), total, covered, scoped - covered,
                                    not (needs_body and not use_cache), warning)
            except sqlite3.OperationalError:
                if stopped():
                    return SearchResult(tuple(hits), total, covered, scoped - covered, False,
                                        "查询已取消或超时，当前为部分结果。")
                raise
            finally:
                conn.set_progress_handler(None, 0)

    @staticmethod
    def _plain_chain(path, allow_missing_leaf=False):
        for component in (*reversed(path.parents), path):
            try:
                info = component.lstat()
            except FileNotFoundError:
                if allow_missing_leaf and component == path:
                    return
                raise
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise NotesError("backup_unsafe_path", "备份目录和文件不能经过符号链接或重解析点。")

    def _backup_directory(self):
        directory = self.data_dir / "backups"
        self._plain_chain(directory, allow_missing_leaf=True)
        directory.mkdir(exist_ok=True)
        self._plain_chain(directory)
        if not directory.is_dir() or directory.parent.resolve() != self.data_dir.resolve():
            raise NotesError("backup_unsafe_path", "备份目录边界校验失败。")
        return directory

    @staticmethod
    def _cancelled(cancel):
        return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))

    def _create_snapshot(self, prefix, now, cancel=None):
        directory = self._backup_directory()
        name = prefix + "-" + datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".db"
        target = directory / name
        temporary = directory / ("." + name + ".partial")
        deadline = time.monotonic() + 30
        def progress(_status, _remaining, _total):
            if self._cancelled(cancel):
                raise NotesError("backup_cancelled", "元数据备份已取消，可稍后重试。")
            if time.monotonic() >= deadline:
                raise NotesError("backup_timeout", "元数据备份超时，可稍后重试。")
        try:
            progress(0, 0, 0)
            with temporary.open("xb"):
                pass
            with self._connect() as source, self._connect(temporary) as destination:
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise NotesError("backup_failed", "管理信息备份校验失败。")
            self._plain_chain(temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            self._backup_directory()
            if os.name == "nt":
                temporary.rename(target)
            else:
                os.link(temporary, target)
                temporary.unlink()
            return target
        finally:
            # Only this invocation's exact temporary file may be removed. Never
            # traverse/recurse or follow a newly substituted backup directory.
            try:
                self._plain_chain(directory)
                if temporary.exists():
                    self._plain_chain(temporary)
                    temporary.unlink()
            except (OSError, NotesError):
                pass

    def backup_metadata(self):
        """Unrotated protective snapshot, including pre-restore backups."""
        with self._lock:
            return self._create_snapshot("metadata", time.time())

    def _auto_state(self):
        try:
            value = json.loads(self.get_setting(BACKUP_STATE_KEY, "{}"))
            if not isinstance(value, dict):
                return {}
            success = value.get("last_success")
            if success is not None and (type(success) not in (int, float) or not math.isfinite(success)):
                return {}
            if success is not None:
                datetime.fromtimestamp(success, timezone.utc)
            snapshots = value.get("snapshots", [])
            if not isinstance(snapshots, list):
                snapshots = []
            valid_snapshots = [
                item for item in snapshots if isinstance(item, dict)
                and isinstance(item.get("name"), str) and AUTO_BACKUP_NAME.fullmatch(item["name"])
                and type(item.get("created_at")) in (int, float) and math.isfinite(item["created_at"])
            ]
            value["snapshots"] = list({item["name"]: item for item in valid_snapshots}.values())
            if value.get("registry_order") != "append_v1":
                # Legacy snapshots were stored in wall-clock sort order. Migrate
                # once; every subsequent success is ordered by durable append.
                value["snapshots"].sort(key=lambda item: (item["created_at"], item["name"]))
            value["registry_order"] = "append_v1"
            return value
        except (TypeError, ValueError, OverflowError, OSError):
            return {}

    def _metadata_fingerprint(self):
        # Derived metadata does not justify a new management backup. Source
        # contents remain the user's ordinary files, not part of this backup.
        queries = (
            "SELECT id,name,root_path,enabled,extensions,excluded_dirs FROM libraries ORDER BY id",
            "SELECT id,library_id,relative_path,is_favorite,last_open_requested_at,encoding_override FROM documents ORDER BY id",
            "SELECT id,name FROM tags ORDER BY id",
            "SELECT document_id,tag_id FROM document_tags ORDER BY document_id,tag_id",
            "SELECT key,value FROM settings WHERE substr(key,1,2)!='__' ORDER BY key",
        )
        digest = hashlib.sha256()
        populated = False
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                if self._smart_version(conn) == 2:
                    # Invalid saved filters are not silently backed up/rotated
                    # over healthy snapshots as if their semantics were known.
                    self._read_smart_rows(conn)
                    queries += ("SELECT id,name,name_key,query_json FROM smart_lists ORDER BY id",)
                for number, query in enumerate(queries):
                    digest.update(str(number).encode("ascii") + b"\n")
                    for row in conn.execute(query):
                        populated = True
                        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                        digest.update(b"\n")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return digest.hexdigest(), populated

    def _last_snapshot_healthy(self, state):
        """Cheap ownership/stat/header verification, not a repeated full hash."""
        value = state.get("last_path")
        if not isinstance(value, str) or not value:
            return False
        path = Path(value)
        directory = self.data_dir / "backups"
        owned = {item["name"]: item for item in state.get("snapshots", [])}
        if (not path.is_absolute() or ".." in path.parts or path.name not in owned
                or not AUTO_BACKUP_NAME.fullmatch(path.name)
                or _key(str(path.parent)) != _key(str(directory))):
            return False
        # Read the expected local path, never a path merely claimed by state.
        path = directory / path.name
        try:
            self._plain_chain(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
            expected = owned[path.name]
            if "size_bytes" in expected and expected["size_bytes"] != info.st_size:
                return False
            if "file_identity" in expected and expected["file_identity"] != [info.st_dev, info.st_ino]:
                return False
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    return False
                header = stream.read(100)
            if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
                return False
            page_size = int.from_bytes(header[16:18], "big")
            page_size = 65536 if page_size == 1 else page_size
            if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
                return False
            page_count = int.from_bytes(header[28:32], "big")
            return (info.st_size >= page_size and info.st_size % page_size == 0
                    and (page_count == 0 or page_count * page_size == info.st_size))
        except (OSError, NotesError):
            return False

    @staticmethod
    def _backup_now(now):
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise NotesError("invalid_time", "备份时间无效。")
        try:
            datetime.fromtimestamp(now, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise NotesError("invalid_time", "备份时间无效。") from None
        return float(now)

    def backup_status(self, now=None):
        now = self._backup_now(now)
        with self._lock:
            state = self._auto_state()
            fingerprint, populated = self._metadata_fingerprint()
            last = state.get("last_success")
            dirty = (fingerprint != state.get("fingerprint")) if last is not None else populated
            protection_lost = last is not None and not self._last_snapshot_healthy(state)
            dirty = dirty or protection_lost
            previous_error = self._backup_failure or state.get("last_error", "")
            error = previous_error if dirty else ""
            clock_skew = last is not None and last > now + 60
            due = dirty and (protection_lost or clock_skew or bool(error) or last is None or datetime.fromtimestamp(now, timezone.utc).date()
                            > datetime.fromtimestamp(last, timezone.utc).date())
            status = "failed" if error else ("due" if due else ("scheduled" if dirty else "clean"))
            message = error or ("管理信息有变化，等待自动备份。" if due else
                                "管理信息有变化，将于下一个 UTC 日备份。" if dirty else "管理信息没有未备份的变化。")
            result = {"last_success": last, "due": bool(due), "dirty": bool(dirty),
                      "status": status, "message": message, "path": state.get("last_path", ""),
                      "schedule": "UTC calendar day", "clock_skew": clock_skew}
            if protection_lost and not error:
                result["message"] = "上次自动快照缺失或未通过安全校验，需要重新备份。"
            elif previous_error and not dirty:
                result["message"] = ("上次维护未完成，但现有自动快照健康且已覆盖管理信息。" if last is not None
                                     else "当前没有需要备份的管理信息。")
            elif clock_skew and not error:
                result["message"] = ("检测到系统时间回拨，管理信息有变化，需要重新备份。" if dirty else
                                     "检测到系统时间回拨；管理信息仍与已有快照一致。")
            self._last_auto_status = result.copy()
            return result

    def _rotate_automatic_snapshots(self, state):
        directory = self._backup_directory()
        snapshots = state.get("snapshots", [])
        retained = snapshots[-7:]
        failed_old = []
        warning = ""
        for item in snapshots[:-7]:
            name = item["name"]
            path = directory / name
            try:
                if not AUTO_BACKUP_NAME.fullmatch(name) or path.parent != directory:
                    raise NotesError("backup_unsafe_path", "自动快照文件名不符合安全规则。")
                self._plain_chain(directory)
                self._plain_chain(path)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise NotesError("backup_unsafe_path", "自动快照不是独立普通文件。")
                if "file_identity" in item and item["file_identity"] != [info.st_dev, info.st_ino]:
                    raise NotesError("backup_unsafe_path", "自动快照已被其他文件替换，不能自动删除。")
                path.unlink()
            except FileNotFoundError:
                continue
            except (OSError, NotesError):
                failed_old.append(item)
                warning = "新快照已成功，但部分旧自动快照未能安全清理。"
        state["snapshots"] = failed_old + retained
        return warning

    def run_automatic_backup(self, now=None, force=False, cancel=None):
        """Back up dirty metadata once per UTC day; force skips only the schedule.

        Successful state is durable and excluded from the dirty fingerprint.
        Only registry-owned auto snapshots are rotated; metadata-* protection
        snapshots and unrelated files are never deleted by automatic retention.
        """
        now = self._backup_now(now)
        with self._lock:
            try:
                status = self.backup_status(now)
                state = self._auto_state()
            except (OSError, sqlite3.Error, NotesError):
                self._backup_failure = "无法读取元数据备份状态，请稍后重试。"
                result = {"last_success": None, "due": True, "dirty": True, "path": "",
                          **self._last_auto_status}
                result.update(status="failed", message=self._backup_failure)
                return result
            if not status["dirty"]:
                # A valid published snapshot is not a failed backup merely
                # because a later retention/bookkeeping attempt failed.
                if state.get("last_error") or self._backup_failure:
                    state["last_error"] = ""
                    try:
                        self.set_setting(BACKUP_STATE_KEY, json.dumps(state, ensure_ascii=False))
                        self._backup_failure = ""
                    except (OSError, sqlite3.Error, NotesError):
                        status["message"] = "现有快照健康；维护状态暂未更新，可稍后重试。"
                return status
            if not status["due"] and not force:
                return status
            try:
                fingerprint, _ = self._metadata_fingerprint()
                path = self._create_snapshot("auto", now, cancel)
                state.update(last_success=now, fingerprint=fingerprint, last_path=str(path), last_error="")
                self._plain_chain(path)
                info = path.lstat()
                state.setdefault("snapshots", []).append({
                    "name": path.name, "created_at": now, "size_bytes": info.st_size,
                    "file_identity": [info.st_dev, info.st_ino],
                })
                # Publish success before retention; publication failure cannot
                # cause old valid snapshots to be removed.
                self.set_setting(BACKUP_STATE_KEY, json.dumps(state, ensure_ascii=False))
                self._backup_failure = ""
            except (OSError, sqlite3.Error, NotesError) as exc:
                self._backup_failure = (exc.message if isinstance(exc, NotesError) else "自动元数据备份失败，旧备份仍保留，可稍后重试。")
                # Reload the last durable successful state. In particular, do
                # not persist speculative success after publication failure.
                try:
                    state = self._auto_state()
                    state["last_error"] = self._backup_failure
                    self.set_setting(BACKUP_STATE_KEY, json.dumps(state, ensure_ascii=False))
                except (OSError, sqlite3.Error, NotesError):
                    pass
                try:
                    result = self.backup_status(now)
                except (OSError, sqlite3.Error, NotesError):
                    result = status.copy()
                result.update(status="failed", message=self._backup_failure)
                return result
            # A successfully validated and durably published snapshot remains
            # successful even if optional retention cannot safely finish.
            try:
                warning = self._rotate_automatic_snapshots(state)
                self.set_setting(BACKUP_STATE_KEY, json.dumps(state, ensure_ascii=False))
            except (OSError, sqlite3.Error, NotesError):
                warning = "新快照已成功，但旧自动快照清理或状态更新未完成。"
            try:
                result = self.backup_status(now)
            except (OSError, sqlite3.Error, NotesError):
                result = {"last_success": now, "dirty": False, "due": False, "path": str(path)}
            result.update(status="created", message=warning or "自动元数据备份成功。", path=str(path))
            return result

    def export_backup(self, path):
        path = Path(path).absolute()
        if path.exists():
            raise NotesError("target_exists", "导出目标已存在，请选择新文件名。")
        if not path.parent.is_dir():
            raise NotesError("invalid_path", "导出目录不存在。")
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                version = self._smart_version(conn)
                payload = {"format_version": version, "app_version": __version__,
                           "exported_at_utc": datetime.now(timezone.utc).isoformat()}
                for table in ("libraries", "documents", "tags", "document_tags", "settings"):
                    payload[table] = [dict(r) for r in conn.execute("SELECT * FROM " + table)]
                if version == 2:
                    payload["smart_lists"] = [row for row, _ in self._read_smart_rows(conn)]
                # Executable configuration is deliberately nonportable.
                payload["settings"] = []
                for doc in payload["documents"]:
                    doc["index_status"] = "pending"
                    doc["availability"] = "unknown"
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        self._validate_backup(payload)
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.stat().st_size > MAX_BACKUP_BYTES:
                raise NotesError("backup_too_large", "备份超过 100 MiB 限制。")
            # On Windows rename refuses an existing target. Use a hard link on
            # POSIX to retain no-overwrite semantics rather than os.replace.
            if os.name == "nt":
                temporary.rename(path)
            else:
                os.link(temporary, path)
                temporary.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()

    def _backup_source(self, path, *, retain_bytes=True):
        """Bind exactly-read bytes to a stable, ordinary input file and path."""
        path = Path(path).absolute()
        self._plain_chain(path)
        def identity(info):
            # Windows ctime is deprecated and this runtime returns different
            # ctime semantics for stat/fstat after rename. Birth time is stable
            # across the path and descriptor; content is separately SHA256-bound.
            born = getattr(info, "st_birthtime_ns", info.st_ctime_ns)
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, born)
        try:
            digest = hashlib.sha256()
            data = None
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise NotesError("invalid_backup", "备份输入必须是普通文件。")
                if before.st_size > MAX_BACKUP_BYTES:
                    raise NotesError("backup_too_large", "备份超过 100 MiB 限制。")
                if retain_bytes:
                    data = stream.read(before.st_size + 1)
                    read_size = len(data)
                    digest.update(data)
                else:
                    read_size = 0
                    while chunk := stream.read(128 * 1024):
                        read_size += len(chunk)
                        if read_size > MAX_BACKUP_BYTES:
                            raise NotesError("backup_too_large", "备份超过 100 MiB 限制。")
                        digest.update(chunk)
                after = os.fstat(stream.fileno())
            self._plain_chain(path)
            if (read_size != before.st_size or identity(before) != identity(after)
                    or identity(after) != identity(path.stat())):
                raise NotesError("backup_changed", "备份读取期间发生变化，请重新检查并确认。")
            envelope = [_key(str(path)), identity(after), digest.hexdigest()]
            signature = hashlib.sha256(
                json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return data, signature
        except OSError:
            raise NotesError("invalid_backup", "备份无法稳定读取，请检查路径和权限。") from None

    def _read_backup(self, path, *, with_signature=False):
        try:
            data, signature = self._backup_source(path)
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result
            def invalid_constant(_value):
                raise ValueError("non-finite JSON number")
            payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
                                 parse_constant=invalid_constant)
            self._validate_backup(payload)
            return (payload, signature) if with_signature else payload
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError):
            raise NotesError("invalid_backup", "备份格式或内容校验失败。") from None

    @classmethod
    def validate_backup_payload(cls, payload, data_dir, *, check_paths=True):
        """Validate without creating directories/connections or initializing Store.

        Recovery uses lexical-only validation first, then probes link metadata
        only for roots already confirmed to be on supported local volumes.
        """
        validator = cls.__new__(cls)
        validator.data_dir = Path(data_dir).absolute()
        validator._validate_backup(payload, check_paths=check_paths)

    def _validate_backup(self, payload, *, check_paths=True):
        def require(condition):
            if not condition:
                raise NotesError("invalid_backup", "备份格式或内容校验失败。")
        require(isinstance(payload, dict) and type(payload.get("format_version")) is int
                and payload["format_version"] in (1, 2))
        if payload["format_version"] == 1:
            if "smart_lists" in payload:
                require(type(payload["smart_lists"]) is list and not payload["smart_lists"])
        else:
            require("smart_lists" in payload)
            try:
                self._validate_smart_rows(payload["smart_lists"])
            except NotesError:
                raise NotesError("invalid_backup", "智能列表备份格式、名称或筛选条件校验失败。") from None
        for table in ("libraries", "documents", "tags", "document_tags", "settings"):
            require(isinstance(payload.get(table), list) and len(payload[table]) <= 500_000)
            require(all(isinstance(row, dict) for row in payload[table]))
        ids = {}
        for table in ("libraries", "documents", "tags"):
            ids[table] = set()
            for row in payload[table]:
                value = row.get("id")
                require(isinstance(value, str))
                try:
                    require(str(uuid.UUID(value)) == value)
                except (ValueError, AttributeError):
                    require(False)
                require(value not in ids[table])
                ids[table].add(value)
        libraries = []
        for row in payload["libraries"]:
            require(isinstance(row.get("root_path"), str) and Path(row["root_path"]).is_absolute())
            self._check_root_boundary(row["root_path"])
            root = Path(row["root_path"])
            require("\x00" not in str(root) and ".." not in root.parts)
            if check_paths:
                for ancestor in (root, *root.parents):
                    require(not ancestor.is_symlink() and not (hasattr(ancestor, "is_junction") and ancestor.is_junction()))
            require(isinstance(row.get("name"), str) and 0 < len(row["name"]) <= 1000)
            require(type(row.get("enabled")) in {int, bool} and row["enabled"] in (0, 1))
            extensions, excluded = _rules(json.loads(row["extensions"]), json.loads(row["excluded_dirs"]))
            self._check_overlap(str(root), libraries)
            library = Library(row["id"], row["name"], str(root), bool(row["enabled"]),
                              "unknown", extensions, excluded)
            libraries.append(library)
        paths = set()
        library_roots = {library.id: Path(library.root_path) for library in libraries}
        for row in payload["documents"]:
            require(row.get("library_id") in ids["libraries"])
            relative = _relative(row.get("relative_path"))
            if check_paths:
                current = library_roots[row["library_id"]]
                for part in PureWindowsPath(relative).parts:
                    current /= part
                    require(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()))
            identity = row["library_id"], _key(relative)
            require(identity not in paths)
            paths.add(identity)
            for field in ("size_bytes", "modified_at", "content_generation"):
                require(type(row.get(field)) is int and 0 <= row[field] <= 2**63 - 2)
            for field in ("in_scope", "is_favorite"):
                require(type(row.get(field)) in {int, bool} and row[field] in (0, 1))
            require(row.get("last_open_requested_at") is None or
                    (type(row["last_open_requested_at"]) is int and 0 <= row["last_open_requested_at"] < 2**63))
            require(row.get("encoding_override") is None or row["encoding_override"] in ENCODINGS)
        names = set()
        for row in payload["tags"]:
            name = self._tag_name(row.get("name"))
            key = normalize_text(name)
            require(key not in names)
            names.add(key)
        associations = set()
        for row in payload["document_tags"]:
            require(row.get("document_id") in ids["documents"] and row.get("tag_id") in ids["tags"])
            pair = row["document_id"], row["tag_id"]
            require(pair not in associations)
            associations.add(pair)
        # Settings intentionally ignored on import, particularly editor executables.

    def inspect_backup(self, path):
        payload, signature = self._read_backup(path, with_signature=True)
        current_signature, _ = self._metadata_fingerprint()
        return {"format_version": payload["format_version"], "libraries": len(payload["libraries"]),
                "documents": len(payload["documents"]), "tags": len(payload["tags"]),
                "smart_lists": len(payload.get("smart_lists", [])),
                "replaces_existing": True, "contains_document_bodies": False,
                "editor_requires_confirmation": True, "signature": signature,
                "current_signature": current_signature}

    def restore_backup(self, path, *, expected_backup=None, expected_current=None):
        # Trusted synchronous Store users may omit the two-phase check. The
        # interactive application service always requires both confirmation IDs.
        guarded = expected_backup is not None or expected_current is not None
        if guarded and any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in (expected_backup, expected_current)):
            raise NotesError("confirmation_required", "恢复前必须重新检查并确认备份及当前管理状态。")
        payload, signature = self._read_backup(path, with_signature=True)
        if guarded and signature != expected_backup:
            raise NotesError("backup_changed", "备份已在检查后改变；未恢复，请重新检查并确认。")

        def check_current():
            if guarded and self._metadata_fingerprint()[0] != expected_current:
                raise NotesError("metadata_changed", "管理信息已在检查后改变；未恢复，请重新检查并确认。")

        with self._lock:
            if self._index_sessions:
                raise NotesError("index_busy", "正在扫描索引，请完成后再恢复管理信息。")
            check_current()
            # This machine's backup ownership registry is not imported from
            # untrusted JSON and must survive logical restore for safe retention.
            local_backup_state = self.get_setting(BACKUP_STATE_KEY)
            with self._connect() as connection:
                current_version = self._smart_version(connection)
            target_version = max(current_version, payload["format_version"])
            self.backup_metadata()
            # A single SQLite transaction is the recovery boundary. This avoids
            # unsafe live DB/WAL replacement; a crash rolls back the whole import.
            with self._connect() as conn, self._transaction(conn):
                # BEGIN IMMEDIATE excludes other SQLite writers while checking
                # current semantics. The local gate spans inspection and commit.
                check_current()
                if guarded and self._backup_source(path, retain_bytes=False)[1] != expected_backup:
                    raise NotesError("backup_changed", "备份已在确认后改变；未恢复，请重新检查并确认。")
                if target_version == 2:
                    if current_version == 1:
                        conn.execute(SMART_LIST_SCHEMA)
                    conn.execute("DELETE FROM smart_lists")
                for table in ("document_file_identities", "document_tags", "documents", "tags", "libraries", "settings"):
                    conn.execute("DELETE FROM " + table)
                restored_libraries = {}
                for row in payload["libraries"]:
                    extensions, excluded_dirs = _rules(json.loads(row["extensions"]), json.loads(row["excluded_dirs"]))
                    conn.execute("INSERT INTO libraries VALUES (?,?,?,?,?,?,?,?)",
                                 (row["id"], row["name"], row["root_path"], _key(row["root_path"]),
                                  row["enabled"], "unknown", json.dumps(extensions), json.dumps(excluded_dirs)))
                    restored_libraries[row["id"]] = Library(row["id"], row["name"], row["root_path"],
                                                             bool(row["enabled"]), "unknown", extensions, excluded_dirs)
                for row in payload["documents"]:
                    relative = _relative(row["relative_path"])
                    conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (row["id"], row["library_id"], relative, _key(relative),
                                  PureWindowsPath(relative).name, row["size_bytes"], row["modified_at"],
                                  "unknown", int(self._in_scope(relative, restored_libraries[row["library_id"]])),
                                  "pending", row["content_generation"] + 1,
                                  row["is_favorite"], row["last_open_requested_at"], row["encoding_override"]))
                for row in payload["tags"]:
                    name = self._tag_name(row["name"])
                    conn.execute("INSERT INTO tags VALUES (?,?,?)", (row["id"], name, normalize_text(name)))
                for row in payload["document_tags"]:
                    conn.execute("INSERT INTO document_tags VALUES (?,?)", (row["document_id"], row["tag_id"]))
                for row in payload.get("smart_lists", []):
                    conn.execute("INSERT INTO smart_lists VALUES (?,?,?,?)",
                                 (row["id"], row["name"], row["name_key"], row["query_json"]))
                if local_backup_state:
                    conn.execute("INSERT INTO settings VALUES (?,?)", (BACKUP_STATE_KEY, local_backup_state))
                conn.execute("PRAGMA user_version=" + str(target_version))
            try:
                self.rebuild_index()
            except Exception as exc:
                raise NotesError(
                    "restored_index_pending",
                    "管理信息已恢复，但搜索索引重建未完成；正文未改变。"
                    "请检查并重建索引，不要重复恢复管理信息。",
                ) from exc

    def rebuild_index(self):
        with self._lock:
            if self._index_sessions:
                raise NotesError("index_busy", "正在扫描索引，请完成后再重建缓存。")
            try:
                with self._connect(self.search_path) as index:
                    if index.execute("PRAGMA user_version").fetchone()[0] > INDEX_SCHEMA_VERSION:
                        raise NotesError("index_version", "搜索缓存来自更新版本，当前版本不会覆盖它。")
            except sqlite3.DatabaseError:
                pass
            # Invalidate metadata first: interruption can never revive old hits.
            with self._connect() as conn:
                conn.execute("UPDATE documents SET index_status='pending',content_generation=content_generation+1")
            try:
                with self._connect(self.search_path) as conn:
                    self._reset_index_schema(conn)
                self._initialize_index()
            except sqlite3.DatabaseError:
                # Connections have closed; preserve corrupt cache for diagnosis.
                suffix = ".corrupt-" + uuid.uuid4().hex
                for path in (self.search_path, Path(str(self.search_path) + "-wal"), Path(str(self.search_path) + "-shm")):
                    if path.exists():
                        path.rename(path.with_name(path.name + suffix))
                self._initialize_index()

    def close(self):
        """There are no retained connections or threads to close."""
