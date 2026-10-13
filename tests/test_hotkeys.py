"""No test in this module registers a real OS hotkey."""

import os
import threading
import time

import pytest
import wx

from notes.domain.models import NotesError
from notes.ui.hotkeys import GlobalQuickOpenHotkey, HOTKEY_ID, KEY_CODE, MODIFIERS


class FakeBackend:
    supported = True

    def __init__(self):
        self.calls = []
        self.register_result = True
        self.unregister_result = True
        self.register_error = None
        self.unregister_error = None
        self.last_error = 0

    def register(self, *args):
        self.calls.append(("register", args, threading.get_ident()))
        if self.register_error:
            raise self.register_error
        return self.register_result

    def unregister(self, *args):
        self.calls.append(("unregister", args, threading.get_ident()))
        if self.unregister_error:
            raise self.unregister_error
        return self.unregister_result


class FakeFrame:
    def __init__(self):
        self.bindings = []
        self.unbindings = []
        self.handle = 1234

    def Bind(self, event, handler, **kwargs):
        self.bindings.append((event, handler, kwargs))

    def Unbind(self, event, handler=None, **kwargs):
        self.unbindings.append((event, handler, kwargs))
        return True

    def GetHandle(self):
        return self.handle


class FakeEvent:
    def __init__(self, identifier=HOTKEY_ID, owner=None):
        self.identifier = identifier
        self.owner = owner
        self.skipped = False

    def GetId(self):
        return self.identifier

    def GetEventObject(self):
        return self.owner

    def Skip(self):
        self.skipped = True


@pytest.fixture
def adapter():
    frame = FakeFrame()
    backend = FakeBackend()
    callbacks = []
    manager = GlobalQuickOpenHotkey(frame, lambda: callbacks.append("activate"), backend=backend)
    try:
        yield manager, frame, backend, callbacks
    finally:
        backend.unregister_error = None
        backend.unregister_result = True
        # Explicit final retry is permissible in tests to avoid retaining a fake
        # ownership marker after testing a failed one-shot close.
        manager.disable()
        manager.close()


def test_construction_is_disabled_and_uses_positive_dedicated_id(adapter):
    manager, frame, backend, _ = adapter
    assert not manager.enabled and not manager.closing and manager.supported
    assert backend.calls == []
    assert 0 <= HOTKEY_ID <= 0xBFFF
    assert manager.hotkey_id == HOTKEY_ID
    assert frame.bindings[0][0] == wx.EVT_HOTKEY
    assert frame.bindings[0][2] == {"id": HOTKEY_ID}


def test_enable_and_disable_are_idempotent_and_use_no_repeat(adapter):
    manager, frame, backend, _ = adapter
    assert manager.enable() and manager.enable()
    assert manager.enabled
    assert backend.calls == [
        ("register", (frame.handle, HOTKEY_ID, MODIFIERS, KEY_CODE), threading.get_ident()),
    ]
    assert MODIFIERS == 0x4003 and KEY_CODE == ord("N")
    assert manager.disable() and manager.disable()
    assert not manager.enabled
    assert len(backend.calls) == 2
    assert backend.calls[-1][1] == (frame.handle, HOTKEY_ID)


def test_conflict_never_unregisters_a_hotkey_that_we_do_not_own(adapter):
    manager, _, backend, _ = adapter
    backend.register_result = False
    backend.last_error = 1409
    with pytest.raises(NotesError) as error:
        manager.enable()
    assert error.value.code == "hotkey_conflict"
    assert not manager.enabled
    assert manager.disable()
    assert manager.close()
    assert [call[0] for call in backend.calls] == ["register"]


def test_registration_exception_and_invalid_handle_do_not_claim_ownership(adapter):
    manager, frame, backend, _ = adapter
    backend.register_error = OSError("registration failed")
    with pytest.raises(NotesError) as error:
        manager.enable()
    assert error.value.code == "hotkey_register_failed" and not manager.enabled
    backend.register_error = None
    frame.handle = 0
    with pytest.raises(NotesError):
        manager.enable()
    manager.close()
    assert [call[0] for call in backend.calls] == ["register"]


@pytest.mark.parametrize("raises", [False, True])
def test_failed_disable_keeps_enabled_and_allows_explicit_retry(adapter, raises):
    manager, _, backend, _ = adapter
    manager.enable()
    backend.unregister_result = False
    if raises:
        backend.unregister_error = OSError("release failure")
    with pytest.raises(NotesError) as error:
        manager.disable()
    assert error.value.code == "hotkey_unregister_failed"
    assert manager.enabled and not manager.closing
    backend.unregister_result = True
    backend.unregister_error = None
    assert manager.disable() and not manager.enabled


def test_close_failure_is_reported_once_and_never_calls_callback_again(adapter):
    manager, frame, backend, callbacks = adapter
    manager.enable()
    backend.unregister_result = False
    assert not manager.close()
    assert manager.enabled and manager.closing
    assert not manager.close()
    assert [call[0] for call in backend.calls].count("unregister") == 1
    manager._on_hotkey(FakeEvent())
    assert callbacks == []
    assert any(item[0] == wx.EVT_HOTKEY and item[2] == {"id": HOTKEY_ID}
               for item in frame.unbindings)
    with pytest.raises(NotesError) as error:
        manager.enable()
    assert error.value.code == "hotkey_closed"


def test_unsupported_backend_is_disabled_without_native_calls(adapter):
    manager, _, backend, _ = adapter
    backend.supported = False
    assert not manager.supported
    assert not manager.enable()
    assert manager.disable() and manager.close()
    assert backend.calls == []


def test_event_filter_disabled_state_and_closed_lifecycle(adapter):
    manager, _, _, callbacks = adapter
    manager._on_hotkey(FakeEvent())
    assert callbacks == []
    manager.enable()
    other = FakeEvent(HOTKEY_ID + 1)
    manager._on_hotkey(other)
    assert other.skipped and callbacks == []
    manager._on_hotkey(FakeEvent())
    assert callbacks == ["activate"]
    manager.disable()
    manager._on_hotkey(FakeEvent())
    assert callbacks == ["activate"]
    manager.enable()
    assert manager.close()
    manager._on_hotkey(FakeEvent())
    assert callbacks == ["activate"]


def test_reentrant_callbacks_and_callback_errors_do_not_escape(adapter):
    manager, _, _, callbacks = adapter
    manager.enable()
    def reentrant():
        callbacks.append("outer")
        manager._on_hotkey(FakeEvent())
        raise RuntimeError("callback failure must not escape GUI dispatch")
    manager._callback = reentrant
    manager._on_hotkey(FakeEvent())
    manager._on_hotkey(FakeEvent())
    assert callbacks == ["outer", "outer"]
    assert manager.enabled and not manager._in_callback


def test_callback_can_close_its_adapter_without_future_delivery(adapter):
    manager, _, backend, callbacks = adapter
    manager.enable()
    def close_during_callback():
        callbacks.append("closing")
        manager.close()
    manager._callback = close_during_callback
    manager._on_hotkey(FakeEvent())
    manager._on_hotkey(FakeEvent())
    assert callbacks == ["closing"]
    assert manager.closing and not manager.enabled
    assert [call[0] for call in backend.calls] == ["register", "unregister"]


def test_only_own_frame_destroy_event_releases_registration(adapter):
    manager, frame, backend, _ = adapter
    manager.enable()
    unrelated = FakeEvent(owner=FakeFrame())
    manager._on_destroy(unrelated)
    assert manager.enabled and not manager.closing and unrelated.skipped
    own = FakeEvent(owner=frame)
    manager._on_destroy(own)
    assert manager.closing and not manager.enabled and own.skipped
    assert backend.calls[-1][0] == "unregister"


def test_second_adapter_for_same_frame_cannot_steal_or_unregister_first(adapter):
    first, frame, first_backend, _ = adapter
    first.enable()
    other_backend = FakeBackend()
    second = GlobalQuickOpenHotkey(frame, lambda: None, backend=other_backend)
    try:
        with pytest.raises(NotesError) as error:
            second.enable()
        assert error.value.code == "hotkey_conflict"
        assert other_backend.calls == []
    finally:
        second.close()
    assert first.enabled
    assert [call[0] for call in first_backend.calls] == ["register"]


def test_worker_cannot_register_unregister_close_or_deliver_callback(adapter):
    manager, _, backend, callbacks = adapter
    manager.enable()
    errors = []
    def worker():
        for operation in (manager.enable, manager.disable, manager.close):
            try:
                operation()
            except NotesError as error:
                errors.append(error.code)
        manager._on_hotkey(FakeEvent())
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == ["hotkey_thread"] * 3
    assert manager.enabled and not manager.closing and callbacks == []
    assert [call[0] for call in backend.calls] == ["register"]


@pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                    reason="Set NOTES_GUI_TESTS=1 for hidden wx plumbing")
def test_hidden_wx_frame_delivers_only_owned_hotkey_with_fake_native_backend():
    app = wx.App(False)
    frame = wx.Frame(None, title="hidden fake-hotkey test")
    frame.Hide()
    backend = FakeBackend()
    callbacks = []
    manager = GlobalQuickOpenHotkey(frame, lambda: callbacks.append("activate"), backend=backend)
    try:
        manager.enable()
        event = wx.KeyEvent(wx.wxEVT_HOTKEY)
        event.SetId(HOTKEY_ID)
        frame.GetEventHandler().ProcessEvent(event)
        assert callbacks == ["activate"]
        manager.close()
        frame.GetEventHandler().ProcessEvent(event)
        assert callbacks == ["activate"]
        assert [call[0] for call in backend.calls] == ["register", "unregister"]
    finally:
        manager.close()
        frame.Destroy()
        app.Yield()
        app.Destroy()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("NOTES_GUI_TESTS") != "1",
                    reason="Hidden Windows wx message routing, fake registration only")
def test_posted_wm_hotkey_routes_to_wx_without_registering_a_system_shortcut():
    import ctypes
    from ctypes import wintypes
    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    frame = wx.Frame(None, title="hidden WM_HOTKEY routing test")
    frame.Hide()
    backend = FakeBackend()
    callbacks = []
    manager = GlobalQuickOpenHotkey(frame, lambda: callbacks.append("activate"), backend=backend)
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True, winmode=0x00000800)
    user32.PostMessageW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL
    try:
        manager.enable()
        # This posts only to our own hidden HWND. It does not call RegisterHotKey,
        # synthesize input, grab focus, or reserve any system key combination.
        assert user32.PostMessageW(frame.GetHandle(), 0x0312, HOTKEY_ID,
                                  (KEY_CODE << 16) | 0x0003)
        deadline = time.monotonic() + 2
        while not callbacks and time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            time.sleep(0.005)
        assert callbacks == ["activate"]
        frame.Destroy()
        deadline = time.monotonic() + 2
        while not manager.closing and time.monotonic() < deadline:
            app.Yield()
            loop.ProcessIdle()
            time.sleep(0.005)
        assert manager.closing and not manager.enabled
        assert [call[0] for call in backend.calls] == ["register", "unregister"]
    finally:
        manager.close()
        if frame:
            frame.Destroy()
        app.Yield()
        del activator
        app.Destroy()
