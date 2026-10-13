"""Asynchronous application use cases; no wxPython imports or GUI-owned IO."""

from __future__ import annotations

import logging
import math
import ntpath
import os
import sqlite3
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PureWindowsPath
from time import time as wall_time
from typing import Any, Callable

from notes.application.retry import RetryQueue
from notes.domain.models import (
    AppState,
    ChangeBatch,
    MoveHint,
    NotesError,
    Preview,
    ReadResult,
    SearchQuery,
    TemplatePreview,
)
from notes.domain.templates import list_templates, render_template
from notes.infrastructure import editor, files
from notes.infrastructure.monitor import FileMonitor
from notes.infrastructure.store import Store

LOG = logging.getLogger(__name__)
Callback = Callable[[Any], None]


class _Cancellation:
    def __init__(self, *events: threading.Event):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class ApplicationService:
    """Bulk writes are serialized; short user opens have an independent action lane.

    Store's gate still serializes each SQL operation with thread-owned connections.
    Editor launches must not wait behind a complete library scan.
    """

    def __init__(
        self,
        data_dir: Path,
        dispatcher: Callable[..., Any] | None = None,
        *,
        monitoring: bool = True,
        wall_clock: Callable[[], float] | None = None,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir)
        recovery_warning = ""
        if self.store.get_setting("__maintenance.recovery_requires_reindex", "") == "1":
            try:
                self.store.rebuild_index()
                self.store.set_setting("__maintenance.recovery_requires_reindex", "")
            except (NotesError, sqlite3.Error, OSError):
                # Unknown future caches and IO failures must not undo metadata
                # recovery or be overwritten to pretend the index is healthy.
                recovery_warning = "管理信息已恢复，全文索引仍需重建；当前可浏览文件名"
                LOG.warning("recovered_index_rebuild_deferred")
        self._dispatch_callback = dispatcher or (lambda fn, *args: fn(*args))
        self._commands = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes-write")
        self._actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes-open")
        self._search = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes-search")
        self._preview = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes-preview")
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._wall_clock = wall_clock or wall_time
        self._retries = RetryQueue()
        self._search_cancel = threading.Event()
        self._query_cancellations = {
            "main": self._search_cancel,
            "quick": threading.Event(),
        }
        self._preview_cancel = threading.Event()
        self._scan_cancel = threading.Event()
        self._listeners: list[Callback] = []
        self._dirty: set[str] = set()
        self._dirty_force: set[str] = set()
        self._scan_scheduled: set[str] = set()
        self._status = recovery_warning or "就绪"
        self._busy = False
        self._data_revision = 0
        self._closed = False
        self._active_scan_libraries: set[str] = set()
        self._hash_not_before: dict[str, float] = {}
        self._hash_paused_until = 0.0
        self._backup_summary: dict = {
            "status": "pending", "message": "自动备份状态待检查", "last_success": None,
        }
        self._backup_not_before = 0.0
        self._last_backup_check: float | None = None
        self._maintenance_queued = False
        self._pending_slots = threading.BoundedSemaphore(128)
        self._action_slots = threading.BoundedSemaphore(8)
        self._quick_query_slots = threading.BoundedSemaphore(32)
        self._monitor = None
        self._scheduler: threading.Thread | None = None
        try:
            if monitoring:
                self._monitor = FileMonitor(self._file_changed)
                self._refresh_watches()
                self._scheduler = threading.Thread(
                    target=self._maintenance, name="notes-maintenance", daemon=False
                )
                self._scheduler.start()
            # This scan never creates libraries or discovers arbitrary directories.
            for library in self.store.list_libraries():
                if library.enabled:
                    self._schedule_scan(library.id, force=self._hash_due(library.id))
            if self._monitor:
                self._request_maintenance()
        except Exception:
            self.close()
            raise

    def subscribe(self, callback: Callback) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def submit(
        self,
        command: str,
        payload: dict | None = None,
        on_success: Callback | None = None,
        on_error: Callback | None = None,
    ) -> Future:
        data = dict(payload or {})
        cancel: threading.Event | None = None
        channel = "main"
        if command in ("query", "cancel_query"):
            channel = data.pop("channel", "main")
            if type(channel) is not str or channel not in ("main", "quick"):
                return self._failed(NotesError("invalid_channel", "查询通道无效"), on_error)
        with self._lock:
            if self._stop.is_set():
                return self._failed(NotesError("closed", "程序正在关闭"), on_error)
            if command in ("cancel_scan", "cancel_query"):
                # Cancellation must never wait in the queue behind the operation it stops.
                token = (
                    self._scan_cancel if command == "cancel_scan"
                    else self._query_cancellations[channel]
                )
                token.set()
                if command == "cancel_scan":
                    self._retries.clear()
                    self._hash_paused_until = time.monotonic() + 15 * 60
                    for library_id in self._active_scan_libraries:
                        self._retries.discard_library(library_id)
                        self._hash_not_before[library_id] = time.monotonic() + 15 * 60
                future = Future()
                future.set_result(None)
                if on_success:
                    self._deliver(on_success, None)
                return future
            is_action = command in {"open_document", "set_editor", "set_quick_open_hotkey"}
            slots = (self._action_slots if is_action else
                     self._quick_query_slots if command == "query" and channel == "quick"
                     else self._pending_slots)
            if not slots.acquire(blocking=False):
                return self._failed(NotesError("busy", "后台任务较多，请稍后重试"), on_error)
            executor = self._commands
            if is_action:
                executor = self._actions
            elif command == "query":
                self._query_cancellations[channel].set()
                self._query_cancellations[channel] = cancel = threading.Event()
                if channel == "main":
                    self._search_cancel = cancel  # Preserve the main-channel legacy alias.
                executor = self._search
            elif command == "preview":
                self._preview_cancel.set()
                self._preview_cancel = cancel = threading.Event()
                executor = self._preview
            elif command in {"list_templates", "preview_template"}:
                # Pure code-defined template work does not wait behind a full
                # library scan and does not cancel the document preview lane.
                executor = self._preview
            try:
                future = executor.submit(self._execute, command, data, cancel)
            except RuntimeError:
                slots.release()
                return self._failed(NotesError("closed", "程序正在关闭"), on_error)

        def completed(done: Future):
            slots.release()
            if self._stop.is_set():
                return
            try:
                result = done.result()
            except Exception as exc:
                error = self._safe_error(exc)
                if on_error:
                    self._deliver(on_error, error)
            else:
                if on_success:
                    self._deliver(on_success, result)

        future.add_done_callback(completed)
        return future

    def call(self, command: str, payload: dict | None = None, timeout: float = 60) -> Any:
        """Blocking test/CLI helper. Never call this from a GUI event handler."""
        return self.submit(command, payload).result(timeout=timeout)

    def _failed(self, error: NotesError, callback: Callback | None) -> Future:
        future = Future()
        future.set_exception(error)
        if callback:
            self._deliver(callback, error)
        return future

    @staticmethod
    def _safe_error(exc: Exception) -> NotesError:
        if isinstance(exc, NotesError):
            return exc
        if isinstance(exc, CancelledError):
            return NotesError("cancelled", "任务已取消")
        # Never log exception text/tracebacks which may contain content and paths.
        LOG.error("operation_failed error_type=%s", type(exc).__name__)
        if isinstance(exc, PermissionError):
            return NotesError("permission", "没有访问权限，请检查文件或目录权限")
        if isinstance(exc, FileNotFoundError):
            return NotesError("missing", "路径不存在或文件已被移动")
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return NotesError("invalid", "参数或配置无效，请检查后重试")
        return NotesError("operation_failed", "操作未完成，请重试或查看诊断状态")

    def _deliver(self, callback: Callback, result: Any):
        if self._stop.is_set():
            return
        try:
            self._dispatch_callback(callback, result)
        except Exception:
            LOG.warning("callback_dispatch_failed")

    def _state(self) -> AppState:
        with self._lock:
            status, busy = self._status, self._busy
        smart_lists = ()
        smart_lists_warning = ""
        try:
            smart_lists = self.store.list_smart_lists()
        except (NotesError, sqlite3.Error):
            # A runtime read failure must not silently reset saved conditions or
            # break ordinary navigation. Startup preflight remains fully strict.
            smart_lists_warning = "智能列表无法读取，原数据未修改；请检查或恢复管理信息。"
        return AppState(
            libraries=tuple(self.store.list_libraries()),
            tags=tuple(self.store.list_tags()),
            editor_path=self.store.get_setting("editor_path", ""),
            status=status,
            busy=busy,
            data_revision=self._data_revision,
            backup_status=str(self._backup_summary.get("message", "")),
            last_backup_at=self._backup_summary.get("last_success"),
            quick_open_hotkey_enabled=(
                self.store.get_setting("quick_open_hotkey_enabled", "0") == "1"
            ),
            smart_lists=smart_lists,
            smart_lists_warning=smart_lists_warning,
        )

    def _notify(self, status: str | None = None, busy: bool | None = None):
        with self._lock:
            if status is not None:
                self._status = status
            if busy is not None:
                self._busy = busy
            if status is not None and not self._busy:
                self._data_revision += 1
            listeners = tuple(self._listeners)
        if not listeners or self._stop.is_set():
            return
        state = self._state()
        for callback in listeners:
            self._deliver(callback, state)

    def _execute(self, command: str, payload: dict, cancel: threading.Event | None = None):
        if self._stop.is_set() or (cancel and cancel.is_set()):
            raise NotesError("cancelled", "任务已取消")
        p = payload
        if command == "state":
            return self._state()
        if command == "list_templates":
            return list_templates()
        if command == "list_smart_lists":
            return self.store.list_smart_lists()
        if command == "preview_template":
            return self._render_new_document(p.get("template_id", "blank"), p["filename"])
        if command == "backup_status":
            self._backup_summary = self.store.backup_status(now=self._wall_clock())
            return self._backup_summary
        if command == "automatic_backup":
            return self._automatic_backup(force=bool(p.get("force", False)))
        if command == "maintenance":
            return self._maintenance_tasks()
        if command == "retry_index":
            return self._retry_index(p["item"])
        if command == "query":
            if "smart_list_id" in p:
                if set(p) - {"smart_list_id", "page", "page_size"}:
                    raise NotesError(
                        "invalid_smart_list",
                        "智能列表查询不能混入其他筛选条件；请编辑列表或切换普通导航。",
                    )
                return self.store.search_smart_list(
                    p["smart_list_id"], page=p.get("page", 0),
                    page_size=p.get("page_size", 100), cancel=cancel,
                )
            if "tag_ids" in p:
                p["tag_ids"] = tuple(p["tag_ids"])
            return self.store.search(SearchQuery(**p), cancel=cancel)
        if command == "preview":
            document = self.store.get_document(p["document_id"])
            library = self.store.get_library(document.library_id)
            self._require_document_scope(library, document)
            path = files.validate_document_path(Path(library.root_path), document.relative_path)
            read = files.read_document(
                path, encoding_override=document.encoding_override, preview=True, cancel=cancel
            )
            return Preview(document, str(path), read)
        if command == "folders":
            library = self.store.get_library(p["library_id"])
            return files.list_folders(
                Path(library.root_path), excluded_dirs=library.excluded_dirs, cancel=self._stop
            )
        if command == "add_library":
            root = files.validate_library_root(
                p["root_path"], self.data_dir,
                existing_roots=[lib.root_path for lib in self.store.list_libraries()],
            )
            library = self.store.add_library(
                str(root), p.get("name", ""),
                tuple(p.get("extensions", (".txt", ".md"))),
                tuple(p.get("excluded_dirs", (".git", ".venv", "__pycache__"))),
            )
            self._refresh_watches()
            self._notify("已添加文档库，等待扫描")
            self._schedule_scan(library.id)
            return library
        if command in ("scan", "rebuild_index"):
            if command == "rebuild_index":
                self.store.rebuild_index()
            return self._perform_scan(
                p.get("library_id"), force=bool(p.get("force", False)),
                affected_paths=tuple(p.get("affected_paths", ())),
                moves=tuple(p.get("moves", ())),
                moves_reliable=p.get("moves_reliable") is True,
            )
        if command in ("inspect_relocation", "relocate_document"):
            document = self.store.get_document(p["document_id"])
            library = self.store.get_library(document.library_id)
            relative = self._relocation_relative(library, p["target_path"])
            if command == "inspect_relocation":
                return self.store.inspect_relocation(document.id, relative)
            if p.get("confirmed") is not True:
                raise NotesError("confirmation_required", "重新定位文件必须明确确认")
            relocated = self.store.relocate_document(
                document.id, relative, merge_unannotated=p.get("merge_unannotated") is True
            )
            self._retries.discard(document.id)
            self._index_one(library, relocated)
            self._notify("文件关联已更新，标签和收藏已保留；未移动或改写文件")
            self._request_maintenance_if_enabled()
            return self.store.get_document(document.id)
        if command == "create_document":
            library = self.store.get_library(p["library_id"])
            if not library.enabled:
                raise NotesError("disabled", "请先启用文档库")
            rendered = self._render_new_document(p.get("template_id", "blank"), p["filename"])
            try:
                entry = files.create_document(
                    Path(library.root_path), p.get("relative_dir", ""), p["filename"],
                    extensions=library.extensions, initial_text=rendered.text, cancel=self._stop,
                    excluded_dirs=library.excluded_dirs,
                )
            except NotesError as exc:
                if exc.code == "created_partial":
                    # Keep the file, never repeat exclusive creation or launch
                    # an editor automatically after a partially failed write.
                    self._repair_created_document(library.id)
                raise
            try:
                documents = self.store.apply_scan(
                    library.id, (entry,), complete=False, availability="online"
                )
                document = next(d for d in documents if d.relative_path == entry.relative_path)
            except Exception as exc:
                self._repair_created_document(library.id)
                raise NotesError(
                    "created_not_registered",
                    "文件已创建，但登记失败；请刷新文档库，不要重复创建。\n"
                    f"保留文件：{Path(library.root_path) / entry.relative_path}",
                ) from exc
            try:
                self._index_one(library, document)
                self._notify("文档已创建")
                self._request_maintenance_if_enabled()
                return self.store.get_document(document.id)
            except Exception as exc:
                # The source and registration already exist. A cache/status
                # failure must never tell the user to repeat file creation.
                self._repair_created_document(library.id)
                raise NotesError(
                    "created_followup_failed",
                    "文件已创建并登记，但索引或状态刷新未完成；"
                    "请刷新文档库，不要重复创建。\n"
                    f"保留文件：{Path(library.root_path) / entry.relative_path}",
                ) from exc
        if command == "open_document":
            # Early lookup is not authority: a queued selection can move or be
            # excluded while waiting. Re-read a final snapshot under Store's
            # short action gate so our own relocation/restore cannot interleave.
            self.store.get_document(p["document_id"])
            recent_saved = True
            with self.store.document_action_gate():
                document = self.store.get_document(p["document_id"])
                library = self.store.get_library(document.library_id)
                self._require_document_scope(library, document)
                path = files.validate_document_path(Path(library.root_path), document.relative_path)
                editor_path = self.store.get_setting("editor_path", "")
                if not editor_path:
                    raise NotesError("editor_not_configured", "请先在设置中选择 Notepad3")
                if self._stop.is_set():
                    raise NotesError("cancelled", "打开请求已取消")
                editor.open_document(editor_path, path)
                try:
                    self.store.mark_opened(document.id)
                except (NotesError, sqlite3.Error, OSError):
                    recent_saved = False
            if not recent_saved:
                LOG.warning("editor_requested_recent_record_failed")
                try:
                    self._notify("已请求 Notepad3 打开文件，但最近打开记录未能保存")
                except (NotesError, sqlite3.Error, OSError):
                    LOG.warning("editor_requested_warning_delivery_failed")
                return None
            self._notify("已请求 Notepad3 打开文件")
            self._request_maintenance_if_enabled()
            return None
        if command == "set_favorite":
            self.store.set_favorite(p["document_id"], bool(p["value"]))
        elif command in {"create_smart_list", "update_smart_list"}:
            allowed = {"name", "query"} | ({"id"} if command == "update_smart_list" else set())
            if set(p) != allowed:
                raise NotesError("invalid_smart_list", "智能列表保存参数不完整或含未知字段。")
            if command == "create_smart_list":
                result = self.store.create_smart_list(p["name"], p["query"])
            else:
                result = self.store.update_smart_list(p["id"], p["name"], p["query"])
            self._notify("智能列表已保存；未移动或改写文档")
            self._request_maintenance_if_enabled()
            return result
        elif command == "delete_smart_list":
            if p.get("confirmed") is not True:
                raise NotesError("confirmation_required", "删除智能列表需要明确确认，文档不会删除")
            if set(p) != {"id", "confirmed"}:
                raise NotesError("invalid_smart_list", "智能列表删除参数无效。")
            self.store.delete_smart_list(p["id"])
        elif command == "set_document_tags":
            self.store.set_document_tags(p["document_id"], tuple(p["tag_ids"]))
        elif command == "create_tag":
            result = self.store.create_tag(p["name"])
            self._notify("标签已创建")
            self._request_maintenance_if_enabled()
            return result
        elif command == "rename_tag":
            self.store.rename_tag(p["tag_id"], p["name"])
        elif command == "delete_tag":
            self.store.delete_tag(p["tag_id"])
        elif command == "set_library_enabled":
            self.store.set_library_enabled(p["library_id"], bool(p["enabled"]))
            if not p["enabled"]:
                self._retries.discard_library(p["library_id"])
            self._refresh_watches()
            if p["enabled"]:
                self._schedule_scan(p["library_id"])
        elif command == "update_library_rules":
            self.store.update_library_rules(
                p["library_id"], tuple(p["extensions"]), tuple(p["excluded_dirs"])
            )
            self._retries.discard_library(p["library_id"])
            self._schedule_scan(p["library_id"])
        elif command == "set_editor":
            path = editor.validate_editor(p["path"]) if p["path"] else ""
            self.store.set_setting("editor_path", str(path))
        elif command == "set_quick_open_hotkey":
            if type(p.get("enabled")) is not bool:
                raise NotesError("invalid_setting", "快捷键开关必须是明确的布尔值")
            self.store.set_setting("quick_open_hotkey_enabled", "1" if p["enabled"] else "0")
        elif command == "set_encoding":
            document = self.store.get_document(p["document_id"])
            library = self.store.get_library(document.library_id)
            self.store.set_encoding(document.id, p.get("encoding") or None)
            self._index_one(library, self.store.get_document(document.id))
        elif command == "export_backup":
            path = self._validate_backup_path(p["path"], exporting=True)
            self.store.export_backup(path)
            self._notify("管理信息已导出（不含正文）")
            return None
        elif command == "inspect_backup":
            path = self._validate_backup_path(p["path"], exporting=False)
            return self.store.inspect_backup(path)
        elif command == "restore_backup":
            if p.get("confirmed") is not True:
                raise NotesError("confirmation_required", "替换管理信息必须明确确认")
            if any(type(p.get(key)) is not str for key in ("expected_backup", "expected_current")):
                raise NotesError("confirmation_required", "请先检查备份和当前管理状态，再明确确认恢复")
            path = self._validate_backup_path(p["path"], exporting=False)
            pending_index = None
            try:
                self.store.restore_backup(
                    path, expected_backup=p["expected_backup"], expected_current=p["expected_current"],
                )
            except NotesError as exc:
                if exc.code != "restored_index_pending":
                    raise
                pending_index = exc
            try:
                self._retries.clear()
                self._hash_not_before.clear()
                self._backup_not_before = 0.0
                self._refresh_watches()
                for library in self.store.list_libraries():
                    if library.enabled:
                        self._schedule_scan(library.id)
                self._notify("管理信息已恢复，正文未修改；编辑器路径需重新确认")
                self._request_maintenance_if_enabled()
            except Exception as exc:
                raise NotesError(
                    "restored_followup_failed",
                    "管理信息已恢复，但后续状态刷新未完成；请检查管理信息和索引，不要重复恢复。",
                ) from exc
            if pending_index is not None:
                raise pending_index
            return None
        else:
            raise NotesError("unknown_command", "不支持的操作")
        self._notify("管理信息已更新")
        self._request_maintenance_if_enabled()
        return None

    @staticmethod
    def _require_document_scope(library, document):
        if not library.enabled:
            raise NotesError("disabled", "文档库已停用，请重新启用后操作")
        if not document.in_scope:
            raise NotesError("out_of_scope", "文档已不在当前管理规则中，请刷新列表")

    def _render_new_document(self, template_id, filename) -> TemplatePreview:
        if type(template_id) is not str:
            raise NotesError("invalid_template", "请选择有效的内置模板")
        files.validate_document_name(filename)
        now = datetime.fromtimestamp(self._wall_clock()).astimezone()
        title = PureWindowsPath(filename).stem
        if template_id == "blank":
            # Empty creation has no title interpolation; keep the established
            # filename contract rather than narrowing it for an unused title.
            return TemplatePreview("blank", title, "", now.isoformat())
        return render_template(template_id, title, now)

    def _repair_created_document(self, library_id):
        """Best-effort repair must not hide the already-created file outcome."""
        try:
            self._schedule_scan(library_id)
        except Exception:
            LOG.warning("created_document_repair_schedule_failed")
            self._file_changed(library_id)

    @staticmethod
    def _relocation_relative(library, value: str) -> str:
        """Keep a lexical path until the filesystem adapter has rejected links."""
        target = Path(value).expanduser()
        if not target.is_absolute():
            raise NotesError("invalid_path", "请选择文档库内的绝对文件路径")
        root = Path(library.root_path)
        a, b = os.path.normcase(os.path.abspath(target)), os.path.normcase(os.path.abspath(root))
        try:
            if os.path.commonpath((a, b)) != b:
                raise ValueError
        except ValueError as exc:
            raise NotesError("path_escape", "只能重新定位到同一文档库内的文件") from exc
        relative = os.path.relpath(target, root).replace("\\", "/")
        files.validate_document_path(root, relative)
        return relative

    def _hash_due(self, library_id: str, *, ignore_delay: bool = False) -> bool:
        now = self._wall_clock()
        if not ignore_delay and time.monotonic() < max(
            self._hash_not_before.get(library_id, 0), self._hash_paused_until
        ):
            return False
        raw = self.store.get_setting(f"__maintenance.hash_verified.{library_id}", "")
        try:
            value = float(raw)
        except (ValueError, TypeError):
            return True
        # Treat corrupt/future timestamps conservatively, not as "verified forever".
        return not math.isfinite(value) or value <= 0 or value > now + 60 or now - value >= 86400

    def _request_maintenance_if_enabled(self):
        if self._monitor:
            self._request_maintenance()

    def _request_maintenance(self):
        with self._lock:
            if self._stop.is_set() or self._maintenance_queued:
                return
            self._maintenance_queued = True
        future = self.submit("maintenance")

        def finished(done):
            with self._lock:
                self._maintenance_queued = False
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    LOG.warning("maintenance_task_failed")

        future.add_done_callback(finished)

    def _automatic_backup(self, *, force: bool = False) -> dict:
        now = self._wall_clock()
        try:
            summary = self.store.run_automatic_backup(now=now, force=force, cancel=self._stop)
        except Exception:
            summary = dict(self._backup_summary, status="failed", message="自动备份失败，稍后重试")
            LOG.warning("automatic_backup_failed")
        self._backup_summary = summary
        self._last_backup_check = now
        failed = summary.get("status") == "failed"
        self._backup_not_before = now + (300 if failed else 30)
        if failed:
            self._notify("管理信息自动备份失败；旧备份仍保留")
        elif force or summary.get("status") == "created":
            self._notify(str(summary.get("message", "管理信息备份状态已更新")))
        else:
            # Update protection status without treating a no-op as a data mutation.
            self._notify()
        return summary

    def _maintenance_tasks(self) -> dict:
        now = self._wall_clock()
        if self._last_backup_check is not None and now < self._last_backup_check - 60:
            self._backup_not_before = 0.0
        if now >= self._backup_not_before:
            self._automatic_backup()
        scheduled = 0
        for library in self.store.list_libraries():
            if library.enabled and self._hash_due(library.id):
                if self._schedule_scan(library.id, force=True):
                    scheduled += 1
        return {"backup": dict(self._backup_summary), "hash_scans_scheduled": scheduled}

    def _validate_backup_path(self, value: str, *, exporting: bool) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != ".json":
            raise NotesError("invalid_backup_path", "请选择 .json 管理信息备份文件")
        # Export cannot overwrite source documents, even if .json was added as a text type.
        for library in self.store.list_libraries():
            root = Path(library.root_path).resolve()
            if path == root or root in path.parents:
                raise NotesError("invalid_backup_path", "请在文档库以外保存或选择管理信息备份")
        if exporting and path.exists():
            raise NotesError("exists", "备份文件已存在，请使用新文件名，原备份不会被覆盖")
        if exporting and not path.parent.is_dir():
            raise NotesError("missing", "备份目标目录不存在")
        return path

    def _refresh_watches(self):
        if not self._monitor:
            return
        try:
            self._monitor.watch(self.store.list_libraries())
        except Exception:
            self._status = "文件监听不可用，将使用补偿扫描；可手动刷新"
            LOG.warning("monitor_setup_failed")

    def _file_changed(self, library_id: str, *, force: bool = False):
        with self._lock:
            if not self._stop.is_set():
                self._dirty.add(library_id)
                if force:
                    self._dirty_force.add(library_id)

    def _schedule_scan(
        self, library_id: str, *, from_events: bool = False, force: bool = False
    ) -> bool:
        with self._lock:
            if library_id in self._scan_scheduled or self._stop.is_set():
                return False
            self._scan_scheduled.add(library_id)
        batch = ChangeBatch()
        if from_events and self._monitor:
            batch = self._monitor.take_change_batch(library_id)
        future = self.submit(
            "scan",
            {"library_id": library_id, "force": force or batch.paths is None,
             "affected_paths": batch.paths or (),
             "moves": batch.moves,
             "moves_reliable": bool(batch.reliable and batch.paths is not None)},
        )

        def finished(done):
            with self._lock:
                self._scan_scheduled.discard(library_id)
            if not self._stop.is_set() and not done.cancelled():
                try:
                    done.result()
                except Exception as exc:
                    if isinstance(exc, NotesError) and exc.code == "busy":
                        self._file_changed(library_id, force=True)
                    self._notify("扫描未完成，可手动刷新重试", False)

        future.add_done_callback(finished)
        return True

    def _perform_scan(
        self, library_id: str | None, *, force: bool = False,
        affected_paths: tuple[str, ...] = (), moves: tuple[MoveHint, ...] = (),
        moves_reliable: bool = False,
    ) -> dict:
        with self._lock:
            self._scan_cancel = threading.Event()
            scan_cancel = _Cancellation(self._stop, self._scan_cancel)
        libraries = (
            (self.store.get_library(library_id),) if library_id else self.store.list_libraries()
        )
        with self._lock:
            self._active_scan_libraries = {lib.id for lib in libraries if lib.enabled}
        discovered = indexed = failures = tracked_moves = 0
        self._notify("正在扫描文档库…", True)
        try:
            for library in libraries:
                if scan_cancel.is_set():
                    break
                if not library.enabled:
                    continue
                if library_id and moves_reliable:
                    tracked_moves += self._apply_move_hints(library, moves, scan_cancel)
                full_verification = force or self._hash_due(library.id)
                verification_ok = True
                result = files.scan_directory(
                    Path(library.root_path), library.extensions, library.excluded_dirs,
                    cancel=scan_cancel,
                )
                documents = self.store.apply_scan(
                    library.id, result.entries, result.complete, result.availability
                )
                discovered += len(result.entries)
                failures += len(result.errors)
                if result.availability != "online":
                    self._retries.discard_library(library.id)
                    if full_verification:
                        self._hash_not_before[library.id] = time.monotonic() + 15 * 60
                    failures += 1
                    continue
                # Snapshot limits: unknown/partial enumeration must never erase identities.
                seen = {entry.relative_path for entry in result.entries}
                candidates = []
                invalidate = []
                for document in documents:
                    if document.relative_path not in seen or not document.in_scope:
                        self._retries.discard(document.id)
                        continue
                    key = ntpath.normcase(document.relative_path).replace("\\", "/")
                    affected = any(
                        not part or key == part or key.startswith(part.rstrip("/") + "/")
                        for part in affected_paths
                    )
                    if full_verification or affected:
                        invalidate.append(document.id)
                    elif document.index_status == "ready":
                        continue
                    candidates.append(document)
                replacements = {
                    doc.id: doc for doc in self.store.invalidate_documents(invalidate)
                } if invalidate else {}
                pending = []

                def flush():
                    nonlocal indexed, failures, verification_ok
                    if not pending:
                        return
                    accepted = self.store.set_index_batch(pending)
                    for ok, (identifier, generation, read) in zip(accepted, pending, strict=True):
                        if ok and read.status == "ready" and self.store.fts_available:
                            indexed += 1
                        else:
                            failures += 1
                        if (not ok or read.status in {"pending", "error"}
                                or (read.status == "ready" and not self.store.fts_available)):
                            verification_ok = False
                        if ok and not scan_cancel.is_set():
                            self._retries.record(identifier, library.id, generation, read)
                    pending.clear()
                    self._notify(
                        f"已发现 {discovered} 个文件，已处理正文 {indexed} 个", True
                    )

                for original in candidates:
                    if scan_cancel.is_set():
                        break
                    document = replacements.get(original.id, original)
                    read = self._read_for_index(library, document, cancel=scan_cancel)
                    pending.append((document.id, document.content_generation, read))
                    if len(pending) >= 50:
                        flush()
                # Completed reads may be committed even if the user cancelled the remainder.
                flush()
                if scan_cancel.is_set():
                    self._retries.discard_library(library.id)
                if full_verification:
                    if result.complete and not scan_cancel.is_set() and verification_ok:
                        self.store.set_setting(
                            f"__maintenance.hash_verified.{library.id}", str(self._wall_clock())
                        )
                        self._hash_not_before.pop(library.id, None)
                    else:
                        # Do not claim success after cancel/partial reads, or spin on bad roots.
                        self._hash_not_before[library.id] = time.monotonic() + 15 * 60
            self._notify(
                ("扫描已取消，已完成的管理信息保留：" if scan_cancel.is_set() else "扫描完成：")
                + f"发现 {discovered} 个文件，更新正文 {indexed} 个"
                + (f"；已保留 {tracked_moves} 个外部文件移动的管理关联" if tracked_moves else "")
                + (f"；{failures} 项未完成或需检查" if failures else ""),
                False,
            )
            return {"discovered": discovered, "indexed": indexed, "failures": failures,
                    "cancelled": scan_cancel.is_set(), "tracked_moves": tracked_moves}
        except Exception as exc:
            message = (
                "扫描未完成：搜索索引暂时被占用，现有索引已保留；请稍后刷新重试。"
                if isinstance(exc, NotesError) and exc.code == "database_busy"
                else "扫描未完成，已完成的管理信息保留；请检查错误后刷新重试。"
            )
            try:
                self._notify(message, False)
            except Exception:
                # Do not replace the original failure if metadata reads needed
                # for the UI snapshot also fail.
                LOG.warning("scan_failure_state_notification_failed")
            raise
        finally:
            with self._lock:
                self._busy = False
                self._active_scan_libraries.clear()

    def _apply_move_hints(self, library, hints: tuple[MoveHint, ...], cancel) -> int:
        """Hints locate candidates; live NTFS identity and the Store decide ownership.

        Never guess chains, cycles, cross-library movements, directory moves or
        partial event collections. A declined hint falls back to normal scanning
        and the existing user-confirmed relocation workflow.
        """
        if not hints or len(hints) > 1000:
            return 0
        now = time.monotonic()
        candidates = {}
        path_uses: dict[str, int] = {}
        for hint in hints:
            if not isinstance(hint, MoveHint):
                return 0
            if (hint.is_directory or hint.is_synthetic
                    or type(hint.received_at) not in (int, float)
                    or not math.isfinite(hint.received_at)
                    or not 0 <= now - hint.received_at <= 300):
                continue
            if not isinstance(hint.source_relative, str) or not isinstance(hint.target_relative, str):
                return 0
            source = ntpath.normcase(ntpath.normpath(hint.source_relative)).replace("\\", "/")
            target = ntpath.normcase(ntpath.normpath(hint.target_relative)).replace("\\", "/")
            if source == target:
                continue  # Case-only renames retain identity via the normal path-key scan.
            key = (source, target)
            if key in candidates:
                continue
            candidates[key] = hint
            path_uses[source] = path_uses.get(source, 0) + 1
            path_uses[target] = path_uses.get(target, 0) + 1
        tracked = 0
        for (source, target), hint in candidates.items():
            if cancel.is_set():
                break
            if path_uses[source] != 1 or path_uses[target] != 1:
                continue
            try:
                entry = files.get_file_entry(Path(library.root_path), hint.target_relative)
                if not entry.file_identity:
                    continue
                document = self.store.apply_observed_move(
                    library.id, hint.source_relative, hint.target_relative, entry
                )
            except (NotesError, OSError):
                continue
            if document is not None:
                self._retries.discard(document.id)
                tracked += 1
        return tracked

    def _index_one(self, library, document, cancel=None) -> bool:
        read = self._read_for_index(library, document, cancel=cancel)
        accepted = self.store.set_index(document.id, document.content_generation, read)
        if accepted and not self._stop.is_set():
            self._retries.record(document.id, library.id, document.content_generation, read)
        return accepted and read.status == "ready"

    def _read_for_index(self, library, document, cancel=None) -> ReadResult:
        try:
            path = files.validate_document_path(
                Path(library.root_path), document.relative_path, must_exist=True
            )
            read = files.read_document(
                path, encoding_override=document.encoding_override, cancel=cancel or self._stop
            )
        except NotesError as exc:
            read = ReadResult(
                status="error", message=exc.message, error_code=files.retry_code_for_exception(exc)
            )
        except OSError as exc:
            read = ReadResult(
                status="error", message="无法读取文件", error_code=files.retry_code_for_exception(exc)
            )
        if read.status == "ready" and (
            read.size_bytes != document.size_bytes or read.modified_at != document.modified_at
        ):
            return ReadResult(
                status="pending", message="文件读取期间发生变化", error_code="metadata_changed"
            )
        return read

    def _retry_index(self, item) -> dict:
        if not self._retries.is_current(item) or self._stop.is_set():
            return {"status": "stale"}
        try:
            document = self.store.get_document(item.document_id)
            library = self.store.get_library(document.library_id)
        except NotesError:
            self._retries.discard(item.document_id, item.generation)
            return {"status": "missing"}
        if (document.content_generation != item.generation or not library.enabled
                or not document.in_scope or document.availability != "present"):
            self._retries.discard(item.document_id, item.generation)
            return {"status": "stale"}
        read = self._read_for_index(library, document, cancel=self._stop)
        if not self._retries.is_current(item) or self._stop.is_set():
            return {"status": "cancelled"}
        if read.error_code == "metadata_changed":
            # The next scoped scan refreshes source stat and generation before reindexing.
            # Keep the attempt counter: unchanged scan metadata must not reset the series.
            pending = self._retries.complete(item, read)
            if pending:
                self._file_changed(library.id)
            else:
                self._notify("文件持续变化，已暂停重试；稳定后可刷新")
            return {"status": "refresh_pending" if pending else "retry_exhausted"}
        accepted = self.store.set_index(document.id, document.content_generation, read)
        if accepted:
            pending = self._retries.complete(item, read)
        else:
            self._retries.discard(item.document_id, item.generation)
            pending = False
        if accepted and read.status == "ready":
            self._notify("文件读取已恢复，正文索引已更新")
        elif not pending:
            self._notify("部分文件暂时无法索引，可在文件可读后刷新重试")
        return {"status": read.status, "retry_pending": pending}

    def _dispatch_retries(self):
        for item in self._retries.pop_due(limit=16):
            future = self.submit("retry_index", {"item": item})

            def completed(done, leased=item):
                if done.cancelled() or self._stop.is_set():
                    self._retries.discard(leased.document_id, leased.generation)
                    return
                try:
                    done.result()
                except NotesError as exc:
                    if exc.code == "busy":
                        self._retries.defer(leased)
                    else:
                        self._retries.discard(leased.document_id, leased.generation)
                except Exception:
                    self._retries.complete(
                        leased, ReadResult(status="error", error_code="io_error")
                    )
                    LOG.warning("index_retry_failed")

            future.add_done_callback(completed)

    def _maintenance(self):
        next_scan = time.monotonic() + 15 * 60
        next_health = time.monotonic() + 30
        next_maintenance = time.monotonic() + 30
        while not self._stop.wait(0.25):
            self._dispatch_retries()
            with self._lock:
                dirty = tuple((key, key in self._dirty_force) for key in self._dirty)
                self._dirty.clear()
                self._dirty_force.clear()
            for library_id, force in dirty:
                if not self._schedule_scan(library_id, from_events=True, force=force):
                    self._file_changed(library_id, force=force)
            now = time.monotonic()
            if now >= next_scan:
                for library in self.store.list_libraries():
                    if library.enabled:
                        self._schedule_scan(library.id)
                next_scan = now + 15 * 60
            if now >= next_health and self._monitor:
                try:
                    health = self._monitor.health()
                    if health.get("degraded") or not health.get("alive", True):
                        self._notify("文件监听异常，补偿扫描仍启用；可手动刷新")
                        self._refresh_watches()
                        for library in self.store.list_libraries():
                            if library.enabled:
                                self._file_changed(library.id, force=True)
                except Exception:
                    LOG.warning("monitor_health_failed")
                next_health = now + 30
            if now >= next_maintenance:
                self._request_maintenance()
                next_maintenance = now + 30

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            with self._lock:
                self._stop.set()
                self._search_cancel.set()
                for token in self._query_cancellations.values():
                    token.set()
                self._preview_cancel.set()
                self._scan_cancel.set()
                self._retries.close()
                self._listeners.clear()
            if self._monitor:
                self._monitor.close()
            if self._scheduler:
                if self._scheduler.ident is not None:
                    self._scheduler.join(timeout=5)
            self._preview.shutdown(wait=True, cancel_futures=True)
            self._search.shutdown(wait=True, cancel_futures=True)
            self._actions.shutdown(wait=True, cancel_futures=True)
            self._commands.shutdown(wait=True, cancel_futures=True)
            self.store.close()
            self._closed = True
