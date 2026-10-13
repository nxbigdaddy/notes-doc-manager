from threading import Event
from unittest.mock import patch

from notes.domain.models import Library
from notes.infrastructure.monitor import FileMonitor


def test_dirty_hints_coalesced_and_cleanup(tmp_path):
    hints = []
    ready = Event()
    def changed(identifier):
        hints.append(identifier)
        ready.set()
    monitor = FileMonitor(changed)
    try:
        monitor.watch([Library("one", "One", str(tmp_path))])
        for _ in range(100):
            monitor._dirty("one")
        monitor._dirty("unknown")
        assert monitor.health()["pending"] == 1
        assert ready.wait(3)
        assert hints == ["one"]
    finally:
        monitor.close()
    assert not monitor._worker.is_alive()
    assert monitor.health()["closed"]
    monitor.close()


def test_watchdog_real_event_and_disabled_scope(tmp_path):
    enabled = tmp_path / "enabled"
    disabled = tmp_path / "disabled"
    enabled.mkdir()
    disabled.mkdir()
    hints = []
    ready = Event()
    def changed(identifier):
        hints.append(identifier)
        ready.set()
    monitor = FileMonitor(changed)
    try:
        monitor.watch([Library("one", "One", str(enabled)),
                       Library("two", "Two", str(disabled), enabled=False)])
        assert not monitor.health()["degraded"]
        (enabled / "new.txt").write_text("test")
        (disabled / "ignored.txt").write_text("ignored")
        assert ready.wait(5)
        assert set(hints) == {"one"}
    finally:
        monitor.close()


def test_monitor_degradation_and_recovery(tmp_path):
    root = tmp_path / "offline"
    monitor = FileMonitor(lambda identifier: None)
    lib = Library("one", "One", str(root))
    try:
        monitor.watch([lib])
        assert monitor.health()["degraded"]
        root.mkdir()
        monitor.watch([lib])
        assert not monitor.health()["degraded"]
        monitor.watch([])
        assert monitor.health()["libraries"] == 0
    finally:
        monitor.close()


def test_missing_watchdog_explicit_degraded(tmp_path):
    with patch("notes.infrastructure.monitor.Observer", None):
        monitor = FileMonitor(lambda identifier: None)
        try:
            monitor.watch([Library("one", "One", str(tmp_path))])
            assert monitor.health()["errors"]["observer"] == "watchdog_unavailable"
        finally:
            monitor.close()


def test_root_removed_marks_monitor_degraded(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    monitor = FileMonitor(lambda identifier: None)
    try:
        monitor.watch([Library("one", "One", str(root))])
        root.rmdir()
        assert monitor.health()["degraded"]
    finally:
        monitor.close()


def test_changed_paths_move_directory_noise_and_bounded_overflow(tmp_path):
    from watchdog.events import (
        DirModifiedEvent,
        DirMovedEvent,
        FileModifiedEvent,
        FileMovedEvent,
    )
    monitor = FileMonitor(lambda identifier: None)
    try:
        monitor.watch([Library("one", "One", str(tmp_path))])
        monitor._event("one", DirModifiedEvent(str(tmp_path)))
        assert monitor.take_changed_paths("one") == ()
        monitor._event("one", FileModifiedEvent(str(tmp_path / "中文.TXT")))
        monitor._event("one", FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt")))
        monitor._event("one", DirMovedEvent(str(tmp_path / "folder"), str(tmp_path / "renamed")))
        assert set(monitor.take_changed_paths("one")) == {
            "中文.txt", "old.txt", "new.txt", "folder", "renamed",
        }
        assert monitor.take_changed_paths("one") is None
        for i in range(1001):
            monitor._event("one", FileModifiedEvent(str(tmp_path / f"{i}.txt")))
        assert monitor.take_changed_paths("one") is None
        monitor._event("one", FileModifiedEvent(str(tmp_path / "single.txt")))
        assert monitor.take_changed_paths("one") == ("single.txt",)
    finally:
        monitor.close()
