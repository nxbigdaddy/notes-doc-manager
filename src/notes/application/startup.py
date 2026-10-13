"""Startup ordering: instance ownership belongs to bootstrap; no implicit repair."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from notes.domain.models import NotesError


def open_application(
    data_dir: Path,
    *,
    dispatcher,
    present_recovery: Callable[[object, dict], bool],
    manager_factory=None,
    service_factory=None,
):
    """Inspect/recover before any Store or background service can mutate a bad DB.

    A false presentation result means the user chose to exit. Never initialize
    an empty database to hide corruption, incompatible versions, or denied IO.
    """
    if manager_factory is None:
        from notes.infrastructure.recovery import RecoveryManager

        manager_factory = RecoveryManager
    if service_factory is None:
        from notes.application.service import ApplicationService

        service_factory = ApplicationService
    manager = manager_factory(data_dir)

    def require_recovered_health():
        # A healthy header alone cannot authorize opening a database when an
        # interrupted-install journal still has an unresolved old/new boundary.
        final_resume = manager.resume()
        final_state = manager.inspect_current()
        if final_resume.get("status") == "blocked" or final_state.get("status") != "healthy":
            raise NotesError("recovery_incomplete", "恢复未验证完成，未启动管理服务")

    resumed = manager.resume()
    status = manager.inspect_current()
    if resumed.get("status") == "blocked":
        status = dict(
            status, status="interrupted", recoverable=False,
            message=resumed.get("message", "上次恢复状态无法安全判定，请保留原数据并退出"),
        )
    if status.get("status") not in {"healthy", "missing"}:
        if not present_recovery(manager, status):
            return None
        # Do not trust a dialog ID alone as evidence that installation succeeded.
        require_recovered_health()
    try:
        return service_factory(data_dir, dispatcher=dispatcher)
    except Exception:
        # A change between preflight and service creation may expose corruption.
        # Do not offer replacement merely because an unrelated GUI/library failed.
        changed = manager.inspect_current()
        if changed.get("status") != "corrupt" or not changed.get("recoverable"):
            raise
        if not present_recovery(manager, changed):
            return None
        require_recovered_health()
        return service_factory(data_dir, dispatcher=dispatcher)
