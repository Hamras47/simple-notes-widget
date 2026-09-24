"""Probe: what does "show desktop" actually do to the widget?

    .venv\\Scripts\\python.exe tools\\probe-desktop.py [--send]

Without ``--send`` it only reports the widget's state.  With ``--send`` it
presses Win+D (the same thing the three-finger "show desktop" swipe does),
reports again, presses Win+D a second time to put the desktop back, and reports
a third time.  It reports the app's own answer (``app.desktop_covering``, which
walks the top-level Z-order) plus what hit-testing sees — the latter is only FYI:
the card is a layered window with its background punched out, so WindowFromPoint
skips past it and names whatever is behind.
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

DESKTOP_CLASSES = {"Progman", "WorkerW", "SysListView32", "SHELLDLL_DefView", "Shell_TrayWnd"}

_user32 = app._user32
_declare = app._declare
_declare(_user32, "WindowFromPoint", [app._Point], ctypes.c_void_p)
_declare(_user32, "GetClassNameW", [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int)
_declare(_user32, "keybd_event", [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p], None)
_declare(_user32, "IsIconic", [ctypes.c_void_p], ctypes.c_int)
_declare(_user32, "IsWindowVisible", [ctypes.c_void_p], ctypes.c_int)

VK_LWIN, VK_D, KEYEVENTF_KEYUP = 0x5B, 0x44, 0x0002


def class_of(handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(ctypes.c_void_p(handle), buffer, 256)
    return buffer.value


def topmost_of(handle: int) -> bool:
    return bool(app._user32.GetWindowLongW(ctypes.c_void_p(handle), app.GWL_EXSTYLE) & 0x8)


def report(label: str, handle: int) -> None:
    rect = app.window_rect(handle)
    if not rect:
        print(f"{label:<22} window gone")
        return
    left, top, right, bottom = rect
    centre = app._Point((left + right) // 2, (top + bottom) // 2)
    under = int(_user32.WindowFromPoint(centre) or 0)
    under_class = class_of(under) if under else "?"
    covered = app.desktop_covering(handle)
    print(
        f"{label:<22} rect={left},{top} {right - left}x{bottom - top} "
        f"visible={bool(_user32.IsWindowVisible(ctypes.c_void_p(handle)))} "
        f"iconic={bool(_user32.IsIconic(ctypes.c_void_p(handle)))} "
        f"topmost={topmost_of(handle)}  "
        f"desktop covering it: {covered}  (hit-test sees {under_class or '?'})"
    )


def press_win_d() -> None:
    _user32.keybd_event(VK_LWIN, 0, 0, None)
    _user32.keybd_event(VK_D, 0, 0, None)
    _user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, None)
    _user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, None)


handle = app.find_window(app.WINDOW_TITLE)
if not handle:
    print("the widget is not running — start it with run.cmd first")
    raise SystemExit(1)

print(f"widget 0x{handle:x} ({app.WINDOW_TITLE})")
report("before", handle)
if "--send" not in sys.argv:
    print("\n(pass --send to press Win+D and watch what the shell does)")
    raise SystemExit(0)

print("\npressing Win+D (show desktop) ...")
press_win_d()
time.sleep(1.2)
report("after show desktop", handle)

print("\npressing Win+D again (restore) ...")
time.sleep(0.3)
press_win_d()
time.sleep(1.2)
report("after restore", handle)

if "--try-topmost" in sys.argv:
    print("\nWin+D again, then forcing the card topmost for a moment ...")
    press_win_d()
    time.sleep(1.2)
    _user32.SetWindowPos(
        ctypes.c_void_p(handle), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013
    )
    time.sleep(0.4)
    report("desktop + topmost", handle)
    _user32.SetWindowPos(
        ctypes.c_void_p(handle), ctypes.c_void_p(-2), 0, 0, 0, 0, 0x0013
    )
    time.sleep(0.3)
    report("topmost cleared", handle)
    press_win_d()
    time.sleep(1.0)
    report("restored again", handle)
