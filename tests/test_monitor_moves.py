import time
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from watchdog.events import DirMovedEvent, FileModifiedEvent, FileMovedEvent

from notes.domain.models import Library
from notes.infrastructure.monitor import FileMonitor, _Handler


@pytest.fixture
def monitor_setup(tmp_path):
    library = Library("one", "One", str(tmp_path))
    monitor = FileMonitor(lambda identifier: None)
    monitor.watch([library])
    try:
        yield monitor, library, tmp_path
    finally:
        monitor.close()


def test_complete_move_preserves_case_and_monotonic_time_without_io(monitor_setup):
    monitor, library, root = monitor_setup
    event = FileMovedEvent(str(root / "Folder" / "Original.TXT"),
                           str(root / "Folder" / "Renamed.txt"))
    with patch.object(Path, "stat", side_effect=AssertionError("No callback IO")):
        with patch("notes.infrastructure.monitor.time.monotonic", return_value=12345.0):
            monitor._event(library.id, event)
    batch = monitor.take_change_batch(library.id)
    assert batch.reliable
    assert batch.paths == ("folder/original.txt", "folder/renamed.txt")
    assert len(batch.moves) == 1
    assert batch.moves[0].source_relative == "Folder/Original.TXT"
    assert batch.moves[0].target_relative == "Folder/Renamed.txt"
    assert batch.moves[0].received_at == 12345.0
    assert not batch.moves[0].is_directory
    assert not batch.moves[0].is_synthetic
    empty = monitor.take_change_batch(library.id)
    assert empty.moves == ()


def test_legacy_consumer_does_not_leave_moves_for_later_batch(monitor_setup):
    monitor, library, root = monitor_setup
    monitor._event(library.id, FileMovedEvent(str(root / "A.txt"), str(root / "B.txt")))
    assert monitor.take_changed_paths(library.id) == ("a.txt", "b.txt")
    monitor._event(library.id, FileModifiedEvent(str(root / "C.txt")))
    batch = monitor.take_change_batch(library.id)
    assert batch.reliable and batch.moves == () and batch.paths == ("c.txt",)


@pytest.mark.parametrize("kind", ["directory", "synthetic", "outside", "incomplete", "unknown"])
def test_unsafe_event_poisoning_never_returns_partial_move_set(monitor_setup, kind):
    monitor, library, root = monitor_setup
    monitor._event(library.id, FileMovedEvent(str(root / "A.txt"), str(root / "B.txt")))
    if kind == "directory":
        event = DirMovedEvent(str(root / "folder"), str(root / "new-folder"))
    elif kind == "synthetic":
        event = FileMovedEvent(str(root / "B.txt"), str(root / "C.txt"), is_synthetic=True)
    elif kind == "outside":
        event = FileMovedEvent(str(root.parent / "outside.txt"), str(root / "C.txt"))
    elif kind == "incomplete":
        event = FileMovedEvent(str(root / "B.txt"), "")
    else:
        monitor._dirty(library.id)
        event = FileModifiedEvent(str(root / "C.txt"))
    monitor._event(library.id, event)
    # A valid later move must not resurrect the poisoned earlier batch.
    monitor._event(library.id, FileMovedEvent(str(root / "D.txt"), str(root / "E.txt")))
    batch = monitor.take_change_batch(library.id)
    assert not batch.reliable and batch.moves == ()


def test_paths_and_moves_are_bounded_and_overflow_discards_all_moves(monitor_setup):
    monitor, library, root = monitor_setup
    for index in range(1001):
        monitor._event(library.id, FileModifiedEvent(str(root / f"{index}.txt")))
    batch = monitor.take_change_batch(library.id)
    assert batch.paths is None and not batch.reliable and batch.moves == ()
    # More than 1000 unique move edges between fewer than 1000 path vertices.
    for index in range(1001):
        source = root / f"{index % 40}.txt"
        target = root / f"{40 + index // 40}.txt"
        monitor._event(library.id, FileMovedEvent(str(source), str(target)))
    batch = monitor.take_change_batch(library.id)
    assert batch.paths is None and not batch.reliable and batch.moves == ()
    monitor._event(library.id, FileMovedEvent(str(root / "new.txt"), str(root / "next.txt")))
    assert monitor.take_change_batch(library.id).reliable


def test_subscription_rebuild_discards_moves_and_old_epoch_handlers(monitor_setup):
    monitor, library, root = monitor_setup
    old_handler = _Handler(monitor, library.id, monitor._epoch)
    event = FileMovedEvent(str(root / "A.txt"), str(root / "B.txt"))
    old_handler.on_any_event(event)
    monitor.watch([library])
    gap = monitor.take_change_batch(library.id)
    assert not gap.reliable and gap.moves == () and gap.paths is None
    old_handler.on_any_event(event)
    assert monitor.take_change_batch(library.id).moves == ()
    _Handler(monitor, library.id, monitor._epoch).on_any_event(event)
    fresh = monitor.take_change_batch(library.id)
    assert fresh.reliable and len(fresh.moves) == 1


def test_unhealthy_observer_discards_queued_move_opportunities(monitor_setup):
    monitor, library, root = monitor_setup
    monitor._event(library.id, FileMovedEvent(str(root / "A.txt"), str(root / "B.txt")))
    observer = monitor._observer
    observer.stop()
    observer.join(timeout=3)
    assert monitor.health()["degraded"]
    batch = monitor.take_change_batch(library.id)
    assert not batch.reliable and batch.moves == () and batch.paths is None


def test_actual_windows_file_rename_arrives_as_complete_move_hint(tmp_path):
    source = tmp_path / "Original.TXT"
    source.write_text("unchanged body", encoding="utf-8")
    library = Library("one", "One", str(tmp_path))
    received = Event()
    monitor = FileMonitor(lambda identifier: received.set())
    monitor.watch([library])
    try:
        target = tmp_path / "Renamed.TXT"
        source.rename(target)
        assert received.wait(5)
        deadline = time.monotonic() + 3
        moves = []
        while time.monotonic() < deadline:
            batch = monitor.take_change_batch(library.id)
            moves.extend(batch.moves)
            if moves:
                break
            time.sleep(0.05)
        assert len(moves) == 1
        assert moves[0].source_relative == "Original.TXT"
        assert moves[0].target_relative == "Renamed.TXT"
        assert target.read_text(encoding="utf-8") == "unchanged body"
    finally:
        monitor.close()
