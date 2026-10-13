"""Watchdog delivers coalesced dirty-library hints, never document truth."""

from __future__ import annotations

import threading
import ntpath
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from notes.domain.models import ChangeBatch, Library, MoveHint, NotesError
from notes.infrastructure.files import _absolute, _check_chain, _local_volume

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # Explicit degraded state permits manual/periodic compensation.
    Observer = None
    class FileSystemEventHandler:
        pass


class _Handler(FileSystemEventHandler):
    def __init__(self, monitor: FileMonitor, library_id: str, epoch: int):
        super().__init__()
        self.monitor = monitor
        self.library_id = library_id
        self.epoch = epoch

    def on_any_event(self, event):
        if event.event_type not in ("opened", "closed_no_write"):
            self.monitor._event(self.library_id, event, epoch=self.epoch)


class FileMonitor:
    def __init__(self, on_change: Callable[[str], None]):
        self._on_change = on_change
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._stop = threading.Event()
        self._pending: set[str] = set()
        self._changed_paths: dict[str, set[str] | None] = {}
        self._moves: dict[str, dict[tuple[str, str], MoveHint]] = {}
        self._unreliable: set[str] = set()
        self._epoch = 0
        self._libraries: dict[str, Library] = {}
        self._errors: dict[str, str] = {}
        self._observer = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="notes-monitor-hints", daemon=False)
        self._worker.start()

    def _dirty(self, library_id: str):
        with self._lock:
            if not self._closed and library_id in self._libraries:
                self._invalidate_batch(library_id)

    def _invalidate_batch(self, library_id: str, *, notify: bool = True):
        """Caller holds _lock; uncertainty poisons the whole unconsumed batch."""
        if notify:
            self._pending.add(library_id)
        self._changed_paths[library_id] = None
        self._moves.pop(library_id, None)
        self._unreliable.add(library_id)

    @staticmethod
    def _relative_event_path(root: str, value: str) -> str | None:
        if not isinstance(value, str) or not value or "\x00" in value or not ntpath.isabs(value):
            return None
        root = ntpath.normpath(root)
        path = ntpath.normpath(value)
        try:
            if ntpath.commonpath((ntpath.normcase(root), ntpath.normcase(path))) != ntpath.normcase(root):
                return None
            relative = ntpath.relpath(path, root).replace("\\", "/")
            return "" if relative == "." else relative
        except ValueError:
            return None

    def _event(self, library_id: str, event, *, epoch: int | None = None):
        """Collect lexical path hints only; never follow event paths or perform IO."""
        with self._lock:
            library = self._libraries.get(library_id)
            if (self._closed or library is None
                    or (epoch is not None and epoch != self._epoch)):
                return
            self._pending.add(library_id)
            if library_id not in self._changed_paths:
                self._changed_paths[library_id] = set()
            changes = self._changed_paths[library_id]
            if changes is None:
                return
            event_type = getattr(event, "event_type", "")
            if event_type not in {"created", "deleted", "modified", "moved", "closed"}:
                self._invalidate_batch(library_id)
                return
            # Parent-directory metadata noise must not force rereading every file.
            if event.is_directory and event_type == "modified":
                return
            endpoints = []
            for value in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
                if not value:
                    endpoints.append(None)
                    continue
                relative = self._relative_event_path(library.root_path, value)
                endpoints.append(relative)
                if relative is None:
                    self._invalidate_batch(library_id)
                    return
                changes.add(ntpath.normcase(relative).replace("\\", "/"))
                if len(changes) > 1000:
                    self._invalidate_batch(library_id)
                    return
            if event_type == "moved":
                source, target = endpoints
                if not source or not target:
                    self._invalidate_batch(library_id)
                    return
                if event.is_directory or getattr(event, "is_synthetic", False):
                    self._moves.pop(library_id, None)
                    self._unreliable.add(library_id)
                    return
                if library_id in self._unreliable:
                    return
                moves = self._moves.setdefault(library_id, {})
                moves[(source, target)] = MoveHint(source, target, time.monotonic())
                if len(moves) > 1000:
                    self._invalidate_batch(library_id)
            elif not endpoints[0]:
                self._invalidate_batch(library_id)

    def take_changed_paths(self, library_id: str) -> tuple[str, ...] | None:
        """Legacy consumer also clears moves so they cannot leak into a later scan."""
        return self.take_change_batch(library_id).paths

    def take_change_batch(self, library_id: str) -> ChangeBatch:
        """Atomically consume path and move evidence; untrusted batches never carry moves."""
        with self._lock:
            paths = self._changed_paths.pop(library_id, None)
            moves = self._moves.pop(library_id, {})
            reliable = paths is not None and library_id not in self._unreliable
            self._unreliable.discard(library_id)
            self._pending.discard(library_id)
            return ChangeBatch(
                tuple(sorted(paths)) if paths is not None else None,
                tuple(moves.values()) if reliable else (),
                reliable,
            )

    def _run(self):
        while not self._stop.wait(0.5):
            with self._lock:
                pending, self._pending = self._pending, set()
            for library_id in pending:
                if self._stop.is_set():
                    return
                try:
                    self._on_change(library_id)
                except Exception:
                    with self._lock:
                        self._errors[library_id] = "callback_failed"
                        self._invalidate_batch(library_id, notify=False)

    def _stop_observer(self):
        observer, self._observer = self._observer, None
        if observer:
            observer.stop()
            observer.join(timeout=5)
            if observer.is_alive():
                with self._lock:
                    self._errors["observer"] = "stop_timeout"

    def watch(self, libraries: Sequence[Library]):
        """Rebuild subscriptions, including roots that have come back online."""
        with self._lifecycle:
            if self._closed:
                return
            with self._lock:
                old_library_ids = set(self._libraries)
                self._epoch += 1
                epoch = self._epoch
                self._errors.clear()
            self._stop_observer()
            with self._lock:
                self._libraries = {lib.id: lib for lib in libraries if lib.enabled}
                self._pending.intersection_update(self._libraries)
                self._changed_paths = {key: value for key, value in self._changed_paths.items()
                                       if key in self._libraries}
                self._moves = {key: value for key, value in self._moves.items()
                               if key in self._libraries}
                self._unreliable.intersection_update(self._libraries)
                for library_id in old_library_ids & self._libraries.keys():
                    self._invalidate_batch(library_id)
            if Observer is None:
                with self._lock:
                    self._errors["observer"] = "watchdog_unavailable"
                    for library_id in self._libraries:
                        self._invalidate_batch(library_id)
                return
            observer = Observer(timeout=0.5)
            for library in tuple(self._libraries.values()):
                try:
                    root = _absolute(Path(library.root_path))
                    _local_volume(root)
                    _check_chain(root)
                    if not root.is_dir():
                        raise FileNotFoundError
                    observer.schedule(_Handler(self, library.id, epoch), str(root), recursive=True)
                except (OSError, NotesError):
                    with self._lock:
                        self._errors[library.id] = "watch_unavailable"
                        self._invalidate_batch(library.id)
            try:
                observer.start()
                self._observer = observer
            except (OSError, RuntimeError):
                with self._lock:
                    self._errors["observer"] = "start_failed"
                    for library_id in self._libraries:
                        self._invalidate_batch(library_id)
                observer.stop()
                for emitter in tuple(observer.emitters):
                    if emitter.is_alive():
                        emitter.stop()
                        emitter.join(timeout=5)

    def health(self) -> dict:
        with self._lifecycle, self._lock:
            observer = self._observer
            alive = bool(observer and observer.is_alive())
            emitter_alive = bool(observer) and all(emitter.is_alive() for emitter in observer.emitters)
            queue_size = observer.event_queue.qsize() if observer else 0
            for library in self._libraries.values():
                try:
                    root = _absolute(library.root_path)
                    _local_volume(root)
                    _check_chain(root)
                    if not root.is_dir():
                        raise FileNotFoundError
                except (OSError, NotesError):
                    self._errors[library.id] = "root_unavailable"
                    self._invalidate_batch(library.id)
            degraded = bool(self._errors) or bool(self._libraries and (not alive or not emitter_alive))
            if queue_size > 10000:
                degraded = True
            if degraded:
                for library_id in self._libraries:
                    self._invalidate_batch(library_id)
            return {"alive": alive, "degraded": degraded, "emitter_alive": emitter_alive,
                    "queue_size": queue_size, "pending": len(self._pending),
                    "libraries": len(self._libraries), "errors": dict(self._errors),
                    "closed": self._closed}

    def close(self):
        with self._lifecycle:
            if self._closed:
                return
            with self._lock:
                self._closed = True
                self._pending.clear()
                self._changed_paths.clear()
                self._moves.clear()
                self._unreliable.clear()
                self._epoch += 1
            self._stop.set()
            self._stop_observer()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=5)
