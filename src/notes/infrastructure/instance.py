"""Windows local-session instance ownership without a network listener."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class SingleInstance:
    def __init__(self, data_dir: Path):
        self.primary = True
        self._mutex = None
        self._event = None
        self._kernel = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self._kernel
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = (
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
        )
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        key = hashlib.sha256(os.path.normcase(str(data_dir.resolve())).encode()).hexdigest()[:32]
        self._mutex = kernel.CreateMutexW(None, False, f"Local\\Notes-{key}")
        if not self._mutex:
            raise OSError("Unable to acquire application instance ownership")
        self.primary = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        self._event = kernel.CreateEventW(None, False, False, f"Local\\Notes-Activate-{key}")
        if not self._event:
            self.close()
            raise OSError("Unable to create activation event")
        if not self.primary:
            kernel.SetEvent(self._event)

    def activation_requested(self) -> bool:
        return bool(self._event and self._kernel.WaitForSingleObject(self._event, 0) == 0)

    def close(self):
        if self._kernel:
            for handle in (self._event, self._mutex):
                if handle:
                    self._kernel.CloseHandle(handle)
        self._event = self._mutex = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
