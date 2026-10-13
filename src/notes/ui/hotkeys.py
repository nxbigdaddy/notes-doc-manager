"""Opt-in Windows Ctrl+Alt+N registration, owned by the GUI thread and frame.

The adapter never changes startup, shell, keyboard, or system settings. Native
calls are injectable; tests must use a fake backend rather than reserving keys.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import weakref

import wx

from notes.domain.models import NotesError

LOG = logging.getLogger(__name__)

# Application-owned IDs must be within 0x0000..0xBFFF. wx.NewIdRef() may return
# negative IDs, so it must not be used for the native RegisterHotKey API.
HOTKEY_ID = 0x4E06
MODIFIERS = 0x0001 | 0x0002 | 0x4000  # ALT | CONTROL | NOREPEAT
KEY_CODE = ord("N")


class _WindowsBackend:
    def __init__(self):
        self.supported = sys.platform == "win32"
        self.last_error = 0
        self._user32 = None
        if not self.supported:
            return
        try:
            from ctypes import wintypes
            # System DLL only; never search writable application directories.
            user32 = ctypes.WinDLL("user32.dll", use_last_error=True, winmode=0x00000800)
            user32.RegisterHotKey.argtypes = (
                wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
            )
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.UnregisterHotKey.restype = wintypes.BOOL
            self._user32 = user32
        except (OSError, AttributeError):
            self.supported = False

    def register(self, hwnd: int, identifier: int, modifiers: int, key: int) -> bool:
        if not self.supported:
            return False
        success = bool(self._user32.RegisterHotKey(hwnd, identifier, modifiers, key))
        self.last_error = 0 if success else ctypes.get_last_error()
        return success

    def unregister(self, hwnd: int, identifier: int) -> bool:
        if not self.supported:
            return False
        success = bool(self._user32.UnregisterHotKey(hwnd, identifier))
        self.last_error = 0 if success else ctypes.get_last_error()
        return success


class GlobalQuickOpenHotkey:
    """One fixed, optional hotkey; construction never registers it.

    ``backend`` exposes ``supported``, ``register(hwnd,id,modifiers,key)->bool``
    and ``unregister(hwnd,id)->bool``. A failed registration does not confer
    ownership, so we never unregister after a conflict or registration error.

    ``close`` permanently suppresses callbacks and makes at most one cleanup
    attempt. A failed unregister leaves ``enabled=True`` conservatively; the UI
    can tell the user that exiting the process will release remaining resources.
    """

    hotkey_id = HOTKEY_ID
    _owners = weakref.WeakKeyDictionary()

    def __init__(self, frame, callback, *, backend=None):
        if not wx.IsMainThread():
            raise NotesError("hotkey_thread", "全局快捷键必须在界面线程配置。")
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._frame = frame
        self._callback = callback
        self._backend = backend if backend is not None else _WindowsBackend()
        self._thread_id = threading.get_ident()
        self._enabled = False
        self._closing = False
        self._close_attempted = False
        self._in_callback = False
        self._hwnd: int | None = None
        self._frame_id = frame.GetId() if hasattr(frame, "GetId") else None
        self._destroy_options = {"id": self._frame_id} if self._frame_id is not None else {}
        self._hotkey_handler = self._on_hotkey
        self._destroy_handler = self._on_destroy
        self._bound = False
        # Bind only this ID. Other consumers' handlers must not be removed.
        frame.Bind(wx.EVT_HOTKEY, self._hotkey_handler, id=HOTKEY_ID)
        try:
            frame.Bind(wx.EVT_WINDOW_DESTROY, self._destroy_handler, **self._destroy_options)
        except Exception:
            frame.Unbind(wx.EVT_HOTKEY, handler=self._hotkey_handler, id=HOTKEY_ID)
            raise
        self._bound = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def supported(self) -> bool:
        return bool(self._backend.supported)

    @property
    def closing(self) -> bool:
        return self._closing

    def _check_thread(self):
        if threading.get_ident() != self._thread_id or not wx.IsMainThread():
            raise NotesError("hotkey_thread", "全局快捷键只能在界面线程注册或释放。")

    def enable(self) -> bool:
        self._check_thread()
        if self._closing:
            raise NotesError("hotkey_closed", "快捷键适配器已关闭。")
        if self._enabled:
            return True
        if not self.supported:
            return False
        owner_ref = self._owners.get(self._frame)
        owner = owner_ref() if owner_ref else None
        if owner is not None and owner is not self and owner.enabled:
            raise NotesError("hotkey_conflict", "此窗口已有快速打开快捷键注册。")
        try:
            hwnd = int(self._frame.GetHandle())
            if hwnd <= 0:
                raise ValueError("window handle unavailable")
            registered = self._backend.register(hwnd, HOTKEY_ID, MODIFIERS, KEY_CODE)
        except Exception:
            raise NotesError("hotkey_register_failed", "无法注册全局快捷键，原系统设置未改动。") from None
        if not registered:
            code = ("hotkey_conflict" if getattr(self._backend, "last_error", 0) in (0, 1409)
                    else "hotkey_register_failed")
            raise NotesError(code, "Ctrl+Alt+N 已被占用或无法注册，请使用窗口内的快速打开。")
        self._hwnd = hwnd
        self._enabled = True
        self._owners[self._frame] = weakref.ref(self)
        return True

    def disable(self) -> bool:
        self._check_thread()
        if not self._enabled:
            return True
        try:
            released = self._backend.unregister(self._hwnd, HOTKEY_ID)
        except Exception:
            released = False
        if not released:
            raise NotesError(
                "hotkey_unregister_failed",
                "全局快捷键尚未成功释放；设置未标记为已关闭，退出程序可释放。",
            )
        self._enabled = False
        self._hwnd = None
        owner_ref = self._owners.get(self._frame)
        if owner_ref and owner_ref() is self:
            self._owners.pop(self._frame, None)
        return True

    def _detach(self):
        if self._bound:
            for event, handler, options in (
                (wx.EVT_HOTKEY, self._hotkey_handler, {"id": HOTKEY_ID}),
                (wx.EVT_WINDOW_DESTROY, self._destroy_handler, self._destroy_options),
            ):
                try:
                    self._frame.Unbind(event, handler=handler, **options)
                except Exception:
                    # Closing has already made retained or queued handlers inert.
                    LOG.warning("quick_open_hotkey_unbind_failed")
            self._bound = False
        self._callback = None

    def close(self) -> bool:
        self._check_thread()
        self._closing = True  # Suppress queued/re-entrant events before native cleanup.
        if self._close_attempted:
            return not self._enabled
        self._close_attempted = True
        try:
            self.disable()
        except NotesError:
            LOG.warning("quick_open_hotkey_cleanup_failed")
        finally:
            self._detach()
        return not self._enabled

    def _on_hotkey(self, event):
        if event.GetId() != HOTKEY_ID:
            event.Skip()
            return
        if (self._closing or not self._enabled or self._in_callback
                or threading.get_ident() != self._thread_id or not wx.IsMainThread()):
            return
        self._in_callback = True
        try:
            self._callback()
        except Exception:
            # Never leak callback arguments, paths, or query text into logs.
            LOG.warning("quick_open_hotkey_callback_failed")
        finally:
            self._in_callback = False

    def _on_destroy(self, event):
        # wxPython can replace the Frame wrapper with a TopLevelWindow wrapper
        # during C++ destruction, so Python object identity alone is insufficient.
        own_id = self._frame_id is not None and event.GetId() == self._frame_id
        if event.GetEventObject() is self._frame or own_id:
            self.close()
        event.Skip()
