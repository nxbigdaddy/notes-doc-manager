"""Deterministic query-lane QA: Event barriers, no sleeps or GUI/global hotkeys."""
from concurrent.futures import CancelledError
from contextlib import contextmanager
import threading

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import NotesError, SearchResult


@pytest.fixture
def service(tmp_path):
    application = ApplicationService(tmp_path / "metadata", monitoring=False)
    try:
        yield application
    finally:
        application.close()


@contextmanager
def blocked_search_lane(service):
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5), "test did not release its search-lane barrier"
    barrier = service._search.submit(blocker)
    assert entered.wait(3)
    try:
        yield
    finally:
        release.set()
        barrier.result(timeout=3)


def assert_cancelled(future):
    try:
        result = future.result(timeout=3)
    except CancelledError:
        return
    except NotesError as error:
        assert error.code == "cancelled"
        return
    assert not result.complete, "cancelled work must not be reported complete"


def assert_capacity(service, count, attribute="_pending_slots"):
    # Executor barrier ensures all previous completion callbacks released leases.
    service._search.submit(lambda: None).result(timeout=3)
    acquired = 0
    slots = getattr(service, attribute)
    try:
        for _ in range(count):
            assert slots.acquire(blocking=False)
            acquired += 1
        assert not slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            slots.release()


def install_gated_search(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, tokens = [], {}
    def search(query, cancel=None):
        calls.append(query.text)
        tokens[query.text] = cancel
        if query.text == "held":
            entered.set()
            assert release.wait(5), "test did not release an in-flight query"
        return SearchResult(complete=not cancel.is_set())
    monkeypatch.setattr(service.store, "search", search)
    return entered, release, calls, tokens


def test_quick_submission_does_not_cancel_inflight_default_main(service, monkeypatch):
    entered, release, calls, tokens = install_gated_search(service, monkeypatch)
    main = service.submit("query", {"text": "held"})
    try:
        assert entered.wait(3)
        legacy = service._search_cancel
        quick = service.submit("query", {"text": "quick", "channel": "quick"})
        assert not tokens["held"].is_set()
        assert service._search_cancel is legacy
        assert legacy is service._query_cancellations["main"]
    finally:
        release.set()
    assert main.result(timeout=3).complete
    assert quick.result(timeout=3).complete
    assert calls == ["held", "quick"]


@pytest.mark.parametrize("channel", ["main", "quick"])
def test_same_channel_replaces_running_query_but_preserves_other_channel(service, monkeypatch, channel):
    entered, release, calls, tokens = install_gated_search(service, monkeypatch)
    other = "quick" if channel == "main" else "main"
    first = service.submit("query", {"text": "held", "channel": channel})
    try:
        assert entered.wait(3)
        cross = service.submit("query", {"text": "cross", "channel": other})
        cross_token = YOUR_SECRET]
        newer = service.submit("query", {"text": "newer", "channel": channel})
        assert tokens["held"].is_set()
        assert not cross_token.is_set()
    finally:
        release.set()
    assert_cancelled(first)
    assert cross.result(timeout=3).complete
    assert newer.result(timeout=3).complete
    assert calls == ["held", "cross", "newer"]


def test_queued_obsolete_main_and_quick_never_reach_store(service, monkeypatch):
    calls = []
    def search(query, cancel=None):
        calls.append(query.text)
        return SearchResult(complete=not cancel.is_set())
    monkeypatch.setattr(service.store, "search", search)
    with blocked_search_lane(service):
        old_main = service.submit("query", {"text": "old-main"})
        old_quick = service.submit("query", {"text": "old-quick", "channel": "quick"})
        new_main = service.submit("query", {"text": "new-main", "channel": "main"})
        new_quick = service.submit("query", {"text": "new-quick", "channel": "quick"})
        assert not new_main.done() and not new_quick.done()
    assert_cancelled(old_main)
    assert_cancelled(old_quick)
    assert new_main.result(timeout=3).complete
    assert new_quick.result(timeout=3).complete
    assert calls == ["new-main", "new-quick"]


@pytest.mark.parametrize("target", ["main", "quick"])
def test_cancel_only_selected_channel_is_immediate_even_when_search_lane_blocked(service, monkeypatch, target):
    entered, release, calls, tokens = install_gated_search(service, monkeypatch)
    other = "quick" if target == "main" else "main"
    running = service.submit("query", {"text": "held", "channel": other})
    try:
        assert entered.wait(3)
        queued = service.submit("query", {"text": "queued", "channel": target})
        # Omitted channel must retain the original main cancellation behavior.
        payload = {} if target == "main" else {"channel": "quick"}
        assert service.submit("cancel_query", payload).result(timeout=1) is None
        assert service._query_cancellations[target].is_set()
        assert not tokens["held"].is_set()
    finally:
        release.set()
    assert running.result(timeout=3).complete
    assert_cancelled(queued)
    assert calls == ["held"]
    assert service.call("query", {"text": "fresh", "channel": target}).complete


def test_legacy_main_cancel_alias_does_not_point_to_quick(service, monkeypatch):
    monkeypatch.setattr(service.store, "search", lambda query, cancel=None: SearchResult(complete=not cancel.is_set()))
    with blocked_search_lane(service):
        main = service.submit("query", {"text": "main"})
        original = service._search_cancel
        quick = service.submit("query", {"text": "quick", "channel": "quick"})
        assert service._search_cancel is original
        service._search_cancel.set()
    assert_cancelled(main)
    assert quick.result(timeout=3).complete


@pytest.mark.parametrize("command", ["query", "cancel_query"])
@pytest.mark.parametrize("channel", [
    None, True, False, 0, 1, 1.25, [], {}, (), b"main",
    "", "MAIN", "Main", "Quick", "quick ", " quick", "preview", "other", "main\0", "quick\n",
])
def test_invalid_channel_fails_before_cancellation_or_capacity_side_effects(service, command, channel, monkeypatch):
    monkeypatch.setattr(service.store, "search", lambda query, cancel=None: SearchResult(complete=not cancel.is_set()))
    service._pending_slots = threading.BoundedSemaphore(2)
    service._quick_query_slots = threading.BoundedSemaphore(2)
    errors = []
    with blocked_search_lane(service):
        main = service.submit("query", {"text": "main"})
        quick = service.submit("query", {"text": "quick", "channel": "quick"})
        main_token = YOUR_SECRET"main"]
        quick_token = YOUR_SECRET"quick"]
        rejected = service.submit(command, {"channel": channel}, on_error=errors.append)
        assert rejected.done(), "invalid channel should not wait behind active work"
        with pytest.raises(NotesError) as failure:
            rejected.result(timeout=1)
        assert failure.value.code == "invalid_channel"
        assert errors and errors[0].code == "invalid_channel"
        assert not main_token.is_set() and not quick_token.is_set()
    assert main.result(timeout=3).complete and quick.result(timeout=3).complete
    assert_capacity(service, 2)
    assert_capacity(service, 2, "_quick_query_slots")


@pytest.mark.parametrize("channel", ["main", "quick"])
def test_future_cancelled_queued_work_releases_exactly_one_slot(service, monkeypatch, channel):
    monkeypatch.setattr(service.store, "search", lambda query, cancel=None: SearchResult(complete=not cancel.is_set()))
    attribute = "_pending_slots" if channel == "main" else "_quick_query_slots"
    other = "quick" if channel == "main" else "main"
    setattr(service, attribute, threading.BoundedSemaphore(1))
    with blocked_search_lane(service):
        discarded = service.submit("query", {"text": "discard", "channel": channel})
        cross = service.submit("query", {"text": "cross", "channel": other})
        current = service._query_cancellations[channel]
        with pytest.raises(NotesError) as busy:
            service.submit("query", {"text": "overflow", "channel": channel}).result(timeout=1)
        assert busy.value.code == "busy"
        assert not current.is_set(), "capacity rejection must not cancel accepted work"
        assert discarded.cancel()
        replacement = service.submit("query", {"text": "discard-again", "channel": channel})
        assert not replacement.done()
        assert replacement.cancel()
        final = service.submit("query", {"text": "final", "channel": channel})
        assert not final.done()
    assert_cancelled(discarded)
    assert_cancelled(replacement)
    assert cross.result(timeout=3).complete and final.result(timeout=3).complete
    assert_capacity(service, 1, attribute)


def test_disabling_hotkey_configuration_does_not_cancel_queries_or_other_settings(service, monkeypatch):
    entered, release, _, tokens = install_gated_search(service, monkeypatch)
    service.store.set_setting("editor_path", "untouched-editor-setting")
    service.call("set_quick_open_hotkey", {"enabled": True})
    first = service.submit("query", {"text": "held", "channel": "main"})
    try:
        assert entered.wait(3)
        quick = service.submit("query", {"text": "quick", "channel": "quick"})
        quick_token = YOUR_SECRET"quick"]
        service.call("set_quick_open_hotkey", {"enabled": False})
        assert not tokens["held"].is_set() and not quick_token.is_set()
        state = service.call("state")
        assert not state.quick_open_hotkey_enabled
        assert state.editor_path == "untouched-editor-setting"
    finally:
        release.set()
    assert first.result(timeout=3).complete and quick.result(timeout=3).complete


def test_query_payload_channel_is_consumed_without_mutating_callers_dict(service):
    payload = {"channel": "quick", "text": "", "mode": "name", "page_size": 20}
    expected = dict(payload)
    assert service.call("query", payload).complete
    assert payload == expected
    assert service.call("query").complete


def test_close_cancels_both_channels_and_joins_owned_threads(tmp_path, monkeypatch):
    existing = set(threading.enumerate())
    service = ApplicationService(tmp_path / "metadata", monitoring=False)
    entered, release, _, tokens = install_gated_search(service, monkeypatch)
    callbacks, close_errors = [], []
    closed = threading.Event()
    closer = None
    try:
        service.call("state")  # Start the actor.
        service._preview.submit(lambda: None).result(timeout=3)
        main = service.submit("query", {"text": "held"}, on_success=callbacks.append)
        assert entered.wait(3)
        quick = service.submit("query", {"text": "quick", "channel": "quick"}, on_success=callbacks.append)
        def closing():
            try:
                service.close()
            except BaseException as error:
                close_errors.append(error)
            finally:
                closed.set()
        closer = threading.Thread(target=closing, name="query-channel-test-close")
        closer.start()
        assert service._stop.wait(3)
        with service._lock:
            assert tokens["held"].is_set()
            assert service._query_cancellations["quick"].is_set()
        release.set()
        assert closed.wait(5)
        closer.join(timeout=1)
        assert not close_errors and not callbacks
        assert_cancelled(main)
        assert_cancelled(quick)
        assert not [thread for thread in threading.enumerate()
                    if thread not in existing and thread.is_alive()]
    finally:
        release.set()
        if closer:
            closer.join(timeout=5)
        service.close()
