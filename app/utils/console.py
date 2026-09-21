"""Keep a Windows console from freezing the process.

With QuickEdit on (the Windows default), clicking in a console window starts a
text selection, and while it is active every write to that console blocks. A
long conversion logs constantly, so one stray click stops the job -- and the
web server, which waits on the same logging lock -- until someone presses Esc.
"""

from __future__ import annotations

import sys

_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_STD_INPUT_HANDLE = -10


def disable_quick_edit() -> bool:
    """Turn QuickEdit off for this process's console. Returns True if changed."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # no console attached, e.g. output redirected
        new_mode = (mode.value & ~_ENABLE_QUICK_EDIT_MODE) | _ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False
