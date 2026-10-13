"""Hidden activation polling lifecycle checks; no IPC, editor, or hotkeys."""
from concurrent.futures import Future
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NOTES_GUI_TESTS") != "1",
                                reason="Set NOTES_GUI_TESTS=1 for hidden wx desktop tests")


class FakeService:
    def subscribe(self, callback):
        pass

    def submit(self, command, payload, success, error):
        return Future()

    def close(self):
        pass


@pytest.fixture
def frame():
    import wx
    from notes.ui.main_frame import MainFrame

    app = wx.App(False)
    loop = wx.GUIEventLoop()
    activator = wx.EventLoopActivator(loop)
    instance = MainFrame(FakeService())
    yield instance
    if instance and not instance.IsBeingDeleted():
        instance.Destroy()
    app.Yield()
    loop.ProcessIdle()
    del activator
    app.Destroy()


def test_legacy_external_timer_outlives_frame_destroy(frame):
    """Observe only; stop immediately before yielding or native destruction."""
    import wx

    timer = wx.Timer(frame)
    timer.Start(500)
    try:
        assert timer.IsRunning()
        frame.Destroy()
        assert timer.IsRunning()
    finally:
        timer.Stop()
    assert not timer.IsRunning()


def test_poll_is_opt_in_and_uses_500ms_interval(frame):
    assert not frame._activation_timer.IsRunning()
    calls = []
    assert frame.start_activation_poll(lambda: calls.append("poll") or False)
    assert frame._activation_timer.IsRunning()
    assert frame._activation_timer.GetInterval() == 500
    assert not frame.IsShown()
    frame._poll_activation(None)
    assert calls == ["poll"]
    assert not frame.IsShown()


@pytest.mark.parametrize("iconized", [False, True])
def test_activation_binding_preserves_show_raise_and_restore(frame, monkeypatch, iconized):
    import wx

    calls = []
    monkeypatch.setattr(frame, "IsIconized", lambda: iconized)
    monkeypatch.setattr(frame, "Iconize", lambda value: calls.append(("iconize", value)))
    monkeypatch.setattr(frame, "Show", lambda: calls.append("show"))
    monkeypatch.setattr(frame, "Raise", lambda: calls.append("raise"))
    frame.start_activation_poll(lambda: True)
    event = wx.PyCommandEvent(wx.EVT_TIMER.typeId, frame._activation_timer.GetId())
    frame.GetEventHandler().ProcessEvent(event)
    assert calls == ([("iconize", False)] if iconized else []) + ["show", "raise"]
    assert not frame.IsShown()


def test_restart_reuses_owned_timer_and_replaces_callback(frame, monkeypatch):
    calls = []
    timer = frame._activation_timer
    original_stop = timer.Stop

    def record_stop():
        calls.append("stop")
        original_stop()

    monkeypatch.setattr(timer, "Stop", record_stop)
    assert frame.start_activation_poll(lambda: calls.append("old") or False)
    assert frame.start_activation_poll(lambda: calls.append("new") or False)
    assert frame._activation_timer is timer
    assert timer.IsRunning()
    assert calls == ["stop", "stop"]
    frame._poll_activation(None)
    assert calls == ["stop", "stop", "new"]


def test_direct_destroy_stops_owned_timer_and_prevents_restart(frame):
    calls = []
    frame.start_activation_poll(lambda: calls.append("poll") or False)
    timer = frame._activation_timer
    frame.Destroy()
    assert not timer.IsRunning()
    assert frame._activation_requested is None
    assert not frame.start_activation_poll(lambda: True)
    frame._poll_activation(None)
    assert calls == []


def test_close_stops_timer_before_background_shutdown(frame, monkeypatch):
    import wx
    from notes.ui import main_frame

    calls = []

    class PendingThread:
        def __init__(self, **kwargs):
            self.target = kwargs["target"]

        def start(self):
            calls.append("shutdown scheduled")

    monkeypatch.setattr(main_frame, "Thread", PendingThread)
    frame.start_activation_poll(lambda: calls.append("poll") or False)
    event = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
    frame._close(event)
    assert frame._closing
    assert not frame._activation_timer.IsRunning()
    assert frame._activation_requested is None
    assert calls == ["shutdown scheduled"]
    frame._poll_activation(None)
    assert calls == ["shutdown scheduled"]
    assert not frame.start_activation_poll(lambda: True)


def test_late_timer_event_does_not_call_callback_while_closing(frame):
    calls = []
    frame.start_activation_poll(lambda: calls.append("poll") or False)
    frame._closing = True
    frame._poll_activation(None)
    assert not calls


def test_callback_initiated_destroy_does_not_show_or_raise(frame, monkeypatch):
    calls = []
    monkeypatch.setattr(frame, "Show", lambda: calls.append("show"))
    monkeypatch.setattr(frame, "Raise", lambda: calls.append("raise"))

    def begin_close():
        frame.Destroy()
        return True

    frame.start_activation_poll(begin_close)
    frame._poll_activation(None)
    assert not calls
    assert not frame._activation_timer.IsRunning()


def test_failed_timer_start_clears_callback(frame, monkeypatch):
    calls = []
    monkeypatch.setattr(frame._activation_timer, "Start", lambda interval: False)
    assert not frame.start_activation_poll(lambda: calls.append("poll") or False)
    assert frame._activation_requested is None
    assert not frame._activation_timer.IsRunning()
    frame._poll_activation(None)
    assert not calls


def test_child_destruction_does_not_stop_frame_poll(frame):
    import wx

    frame.start_activation_poll(lambda: False)
    child = wx.Panel(frame)
    child.Destroy()
    assert not frame._closing
    assert frame._activation_timer.IsRunning()


def test_native_base_destroy_stops_timer_in_destruction_event(frame):
    import wx

    frame.start_activation_poll(lambda: False)
    timer = frame._activation_timer
    try:
        wx.Frame.Destroy(frame)
        # Process deferred native destruction immediately, without sleeping for
        # the 500ms poll or ever intentionally dispatching an orphan timer.
        wx.GetApp().Yield()
        wx.EventLoopBase.GetActive().ProcessIdle()
        assert frame._closing
        assert not timer.IsRunning()
        assert frame._activation_requested is None
    finally:
        timer.Stop()
