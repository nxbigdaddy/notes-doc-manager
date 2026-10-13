from concurrent.futures import ThreadPoolExecutor

import pytest

from notes.application.retry import RETRY_DELAYS, RetryQueue, is_retryable
from notes.domain.models import ReadResult


def transient(code="sharing_violation"):
    return ReadResult(status="pending", error_code=code)


def test_explicit_time_exact_backoff_and_exhaustion_without_sleeps():
    queue = RetryQueue()
    now = 100.0
    assert queue.record("doc", "lib", 1, transient(), now=now)
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        assert queue.pop_due(now + delay - 0.01) == ()
        now += delay
        items = queue.pop_due(now)
        assert len(items) == 1
        assert items[0].attempt == attempt
        assert queue.pop_due(now + 100) == ()  # In flight is never leased twice.
        assert queue.complete(items[0], transient(), now=now) == (attempt < 5)
    assert queue.health()["exhausted"] == 1
    assert queue.pop_due(1000) == ()
    assert not queue.record("doc", "lib", 1, transient(), now=1000)
    assert queue.pop_due(1001) == ()
    assert queue.record("doc", "lib", 2, transient(), now=1002)
    assert queue.pop_due(1002.5)[0].attempt == 1


def test_injected_clock_and_duplicate_failure_keeps_original_due():
    clock = [10.0]
    queue = RetryQueue(clock=lambda: clock[0])
    assert queue.record("doc", "lib", 1, transient())
    clock[0] = 10.4
    assert not queue.record("doc", "lib", 1, transient())
    assert queue.pop_due() == ()
    clock[0] = 10.5
    assert queue.pop_due()[0].attempt == 1


def test_new_generation_and_same_generation_event_reset_reject_stale_completion():
    queue = RetryQueue()
    queue.record("doc", "lib", 1, transient(), now=0)
    old = queue.pop_due(0.5)[0]
    assert queue.is_current(old)
    assert queue.record("doc", "lib", 2, transient(), now=1)
    assert not queue.is_current(old)
    assert not queue.complete(old, ReadResult(), now=1)
    assert not queue.record("doc", "lib", 1, ReadResult(), now=1)
    assert not queue.discard("doc", generation=1)
    current = queue.pop_due(1.5)[0]
    assert current.generation == 2
    assert queue.record("doc", "lib", 2, transient(), now=2, reset=True)
    assert not queue.complete(current, transient(), now=2)
    renewed = queue.pop_due(2.5)[0]
    assert queue.is_current(renewed)
    assert renewed.generation == current.generation
    assert renewed.attempt == 1
    assert renewed.token != current.token


def test_success_permanent_failure_and_cancellation_clear_work():
    for read in (ReadResult(), ReadResult(status="pending", error_code="cancelled"),
                 ReadResult(status="needs_encoding", error_code="decode_error"),
                 ReadResult(status="error", error_code="missing")):
        queue = RetryQueue()
        queue.record("doc", "lib", 1, transient(), now=0)
        item = queue.pop_due(0.5)[0]
        assert not queue.complete(item, read, now=1)
        assert queue.health()["tracked"] == 0
        assert queue.pop_due(100) == ()
        assert queue.record("doc", "lib", 1, transient(), now=101)
        assert not queue.record("doc", "lib", 1, read, now=102)
        assert queue.health()["tracked"] == 0


@pytest.mark.parametrize("code", [
    "changed", "metadata_changed", "sharing_violation", "access_denied", "io_error",
])
@pytest.mark.parametrize("status", ["pending", "error"])
def test_only_explicit_transient_codes_retry(code, status):
    read = ReadResult(status=status, error_code=code)
    assert is_retryable(read)
    assert RetryQueue().record("doc", "lib", 1, read, now=0)


@pytest.mark.parametrize("read", [
    ReadResult(status="pending"),
    ReadResult(status="error"),
    ReadResult(status="pending", error_code="cancelled"),
    ReadResult(status="error", error_code="missing"),
    ReadResult(status="error", error_code="offline"),
    ReadResult(status="error", error_code="reparse_point"),
    ReadResult(status="error", error_code="invalid_path"),
    ReadResult(status="needs_encoding", error_code="changed"),
    ReadResult(status="skipped_large", error_code="sharing_violation"),
    ReadResult(status="skipped_binary", error_code="io_error"),
    ReadResult(status="ready", error_code="changed"),
])
def test_nontransient_statuses_and_unknown_codes_never_retry(read):
    queue = RetryQueue()
    assert not is_retryable(read)
    assert not queue.record("doc", "lib", 1, read, now=0)
    assert queue.health()["tracked"] == 0


def test_defer_submission_preserves_attempt_and_waits_instead_of_spinning():
    queue = RetryQueue()
    queue.record("doc", "lib", 1, transient(), now=0)
    item = queue.pop_due(0.5)[0]
    for index in range(20):
        now = 0.5 + index * 0.5
        assert queue.defer(item, now=now)
        assert queue.pop_due(now) == ()
        assert queue.pop_due(now + 0.49) == ()
        renewed = queue.pop_due(now + 0.5)[0]
        assert renewed == item
    assert queue.complete(item, transient(), now=11)
    assert queue.pop_due(11.99) == ()
    assert queue.pop_due(12)[0].attempt == 2


def test_capacity_and_heap_bound_include_exhausted_markers():
    queue = RetryQueue(max_items=2)
    queue.record("first", "lib", 1, transient(), now=0)
    queue.record("second", "lib", 1, transient(), now=0)
    assert not queue.record("third", "lib", 1, transient(), now=0)
    assert queue.health()["rejected_capacity"] == 1
    for generation in range(2, 500):
        assert queue.record("first", "lib", generation, transient(), now=generation)
        assert queue.health()["tracked"] == 2
        assert queue.health()["heap_entries"] <= 68
    assert queue.discard("second")
    assert queue.record("third", "lib", 1, transient(), now=500)
    assert len(queue.pop_due(1000)) == 2


def test_pop_due_bounded_batches():
    queue = RetryQueue()
    for identifier in range(100):
        queue.record(str(identifier), "lib", 1, transient(), now=0)
    assert len(queue.pop_due(0.5)) == 32
    assert len(queue.pop_due(0.5, limit=10)) == 10
    assert queue.health()["pending"] == 58
    assert queue.health()["in_flight"] == 42


def test_discard_library_cancel_close_and_stale_leases():
    queue = RetryQueue()
    queue.record("one", "a", 1, transient(), now=0)
    queue.record("two", "b", 1, transient(), now=0)
    leased = queue.pop_due(0.5)
    assert queue.discard_library("a") == 1
    assert not queue.complete(leased[0], transient(), now=1)
    assert queue.health()["tracked"] == 1
    queue.clear()
    assert not any(queue.is_current(item) for item in leased)
    assert queue.health()["tracked"] == 0
    assert not queue.complete(leased[1], transient(), now=1)
    assert queue.record("three", "c", 1, transient(), now=2)
    queue.close()
    queue.close()
    assert not queue.record("four", "d", 1, transient(), now=3)
    assert queue.pop_due(1000) == ()
    assert queue.health()["closed"]


def test_concurrent_duplicate_submissions_and_pop_only_lease_once():
    queue = RetryQueue()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: queue.record("doc", "lib", 1, transient(), now=0), range(100)
        ))
        assert sum(results) == 1
        leased = list(pool.map(lambda _: queue.pop_due(0.5), range(100)))
    assert sum(map(len, leased)) == 1
    assert queue.health()["in_flight"] == 1


@pytest.mark.parametrize("capacity", [0, -1, 10001, True, 1.5])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        RetryQueue(max_items=capacity)


def test_invalid_time_and_identity_do_not_queue():
    queue = RetryQueue()
    with pytest.raises(ValueError):
        queue.record("doc", "lib", 1, transient(), now=float("nan"))
    with pytest.raises(ValueError):
        queue.record("doc", "lib", True, transient(), now=0)
    with pytest.raises(ValueError):
        queue.record("", "lib", 1, transient(), now=0)
    assert queue.health()["tracked"] == 0


def test_files_error_codes_distinguish_cancel_missing_decode_and_changed(tmp_path):
    from contextlib import contextmanager
    from threading import Event
    from unittest.mock import patch
    from notes.infrastructure import files
    path = tmp_path / "document.txt"
    path.write_text("original", encoding="utf-8")
    cancel = Event()
    cancel.set()
    assert files.read_document(path, cancel=cancel).error_code == "cancelled"
    assert files.read_document(tmp_path / "missing.txt").error_code == "missing"
    path.write_bytes(b"\xff")
    assert files.read_document(path).error_code == "decode_error"
    path.write_bytes(b"\x00")
    assert files.read_document(path).error_code == "binary"
    path.write_text("original", encoding="utf-8")
    original_reader = files._shared_reader

    @contextmanager
    def changed_reader(source):
        with original_reader(source) as stream:
            yield stream
        source.write_text("changed after read", encoding="utf-8")

    with patch.object(files, "_shared_reader", changed_reader):
        result = files.read_document(path)
    assert result.error_code == "changed"
    assert is_retryable(result)


@pytest.mark.parametrize("winerror,expected", [
    (32, "sharing_violation"), (33, "sharing_violation"),
    (1224, "sharing_violation"), (5, "access_denied"),
    (21, "offline"), (53, "offline"), (123, "invalid_path"), (206, "invalid_path"),
])
def test_exception_codes_preserve_wrapped_windows_cause(winerror, expected):
    from notes.domain.models import NotesError
    from notes.infrastructure.files import retry_code_for_exception
    original = OSError("localized text is irrelevant")
    original.winerror = winerror
    wrapped = NotesError("unavailable", "opaque message")
    wrapped.__cause__ = original
    assert retry_code_for_exception(wrapped) == expected
    assert retry_code_for_exception(original) == expected


def test_exception_codes_without_windows_error():
    from notes.domain.models import NotesError
    from notes.infrastructure.files import retry_code_for_exception
    assert retry_code_for_exception(PermissionError()) == "access_denied"
    assert retry_code_for_exception(FileNotFoundError()) == "missing"
    assert retry_code_for_exception(OSError()) == "io_error"
    assert retry_code_for_exception(NotesError("reparse_point", "")) == "reparse_point"
    assert retry_code_for_exception(NotesError("cancelled", "")) == "cancelled"
