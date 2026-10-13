from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from notes.application.startup import open_application
from notes.domain.models import NotesError


def run_case(tmp_path, statuses, *, resume="none", accept=False, factory_error=None):
    calls = []
    statuses = iter(statuses)
    manager = SimpleNamespace(
        resume=lambda: calls.append("resume") or {"status": resume, "message": "resume-status"},
        inspect_current=lambda: calls.append("inspect") or next(statuses),
    )

    def present(instance, status):
        assert instance is manager
        calls.append(("dialog", status))
        return accept

    count = 0

    def service(path, dispatcher):
        nonlocal count
        assert path == tmp_path
        calls.append("service")
        count += 1
        if factory_error and count == 1:
            raise factory_error
        return "service-ready"

    result = open_application(
        tmp_path, dispatcher=lambda *_: None, present_recovery=present,
        manager_factory=lambda path: manager, service_factory=service,
    )
    return result, calls


@pytest.mark.parametrize("state", ["missing", "healthy"])
def test_only_healthy_or_absent_store_is_created_after_resume_and_inspection(tmp_path, state):
    result, calls = run_case(tmp_path, [{"status": state}])
    assert result == "service-ready"
    assert calls == ["resume", "inspect", "service"]


@pytest.mark.parametrize("state", ["corrupt", "unsupported", "busy", "inaccessible"])
def test_cancel_never_creates_store_or_silently_resets_database(tmp_path, state):
    marker = tmp_path / "metadata.db"
    marker.write_bytes(b"preserve original")
    result, calls = run_case(tmp_path, [{"status": state, "recoverable": state == "corrupt"}])
    assert result is None and "service" not in calls
    assert marker.read_bytes() == b"preserve original"


def test_successful_confirmation_requires_post_recovery_health(tmp_path):
    result, calls = run_case(
        tmp_path, [{"status": "corrupt", "recoverable": True}, {"status": "healthy"}],
        accept=True,
    )
    assert result == "service-ready" and calls[-2:] == ["inspect", "service"]


def test_dialog_success_does_not_mean_database_is_healthy(tmp_path):
    with pytest.raises(NotesError, match="恢复未验证完成"):
        run_case(tmp_path, [{"status": "corrupt"}, {"status": "corrupt"}], accept=True)


def test_unknown_interrupted_state_is_not_overwritten_even_with_healthy_header(tmp_path):
    result, calls = run_case(tmp_path, [{"status": "healthy"}], resume="blocked")
    assert result is None and "service" not in calls
    shown = next(item[1] for item in calls if isinstance(item, tuple))
    assert shown["status"] == "interrupted" and shown["recoverable"] is False


def test_dialog_cannot_bypass_an_unresolved_journal_with_a_healthy_header(tmp_path):
    with pytest.raises(NotesError, match="恢复未验证完成"):
        run_case(
            tmp_path, [{"status": "healthy"}, {"status": "healthy"}],
            resume="blocked", accept=True,
        )


def test_unrelated_initialization_error_does_not_offer_destructive_recovery(tmp_path):
    with pytest.raises(RuntimeError, match="unrelated"):
        run_case(
            tmp_path, [{"status": "healthy"}, {"status": "healthy"}],
            factory_error=RuntimeError("unrelated"),
        )


def test_new_corruption_between_check_and_service_rechecks_before_recovery(tmp_path):
    result, calls = run_case(
        tmp_path,
        [{"status": "healthy"}, {"status": "corrupt", "recoverable": True}, {"status": "healthy"}],
        factory_error=RuntimeError("metadata became corrupt"), accept=True,
    )
    assert result == "service-ready"
    assert calls.count("service") == 2


def test_import_has_no_directory_creation_side_effects(tmp_path):
    # The controller delegates inspection; its import/decision path does not
    # create a new metadata database before the user has selected a recovery.
    assert list(Path(tmp_path).iterdir()) == []


def test_partial_service_initialization_cleans_its_threads(tmp_path, monkeypatch):
    import notes.application.service as application

    original = application.Store

    class FailedStore(original):
        def list_libraries(self):
            raise RuntimeError("late startup failure")

    before = set(threading.enumerate())
    monkeypatch.setattr(application, "Store", FailedStore)
    with pytest.raises(RuntimeError, match="late startup failure"):
        application.ApplicationService(tmp_path / "data", monitoring=True)
    assert not [thread for thread in threading.enumerate()
                if thread not in before and thread.is_alive()]


def test_recovery_reindex_marker_is_cleared_only_after_success(tmp_path):
    from notes.application.service import ApplicationService
    from notes.infrastructure.store import Store

    store = Store(tmp_path)
    store.set_setting("__maintenance.recovery_requires_reindex", "1")
    store.close()
    service = ApplicationService(tmp_path, monitoring=False)
    try:
        assert service.store.get_setting("__maintenance.recovery_requires_reindex") == ""
    finally:
        service.close()


def test_future_cache_is_not_overwritten_by_recovery_marker(tmp_path):
    from contextlib import closing
    import sqlite3

    from notes.application.service import ApplicationService
    from notes.infrastructure.store import Store

    store = Store(tmp_path)
    store.set_setting("__maintenance.recovery_requires_reindex", "1")
    cache = store.search_path
    store.close()
    with closing(sqlite3.connect(cache)) as connection:
        connection.execute("PRAGMA user_version=999")
    before = cache.read_bytes()
    service = ApplicationService(tmp_path, monitoring=False)
    try:
        assert service.store.get_setting("__maintenance.recovery_requires_reindex") == "1"
        assert "全文索引" in service.call("state").status
        assert cache.read_bytes() == before
    finally:
        service.close()
