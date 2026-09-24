"""
Sticky Note — Windows desktop widget.

A frameless glass card holding a checklist. Clicking an item marks it done and strikes
it through. Everything is kept in notes.json next to this file.

    run.cmd                  start the widget (no console window)
    run-debug.cmd            start it with a console, so errors are visible
    install-autostart.ps1    start it with Windows
    uninstall-autostart.ps1  stop starting it with Windows

Window hosting is pywebview (Edge WebView2, already part of Windows); the tray icon is
pystray.

All note logic lives in notes.py — this file only hosts the window, keeps preferences,
and passes clicks between the page and the checklist.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import notes as engine

import pystray
import webview
from PIL import Image, ImageDraw

# --------------------------------------------------------------------------- #
# Paths and constants
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
ASSET_DIR = ROOT / "assets"
CONFIG_FILE = ROOT / "config.json"
LOG_FILE = ROOT / "widget.log"
ICON_FILE = ASSET_DIR / "icon.ico"
PREVIEW_FILE = UI_DIR / "state.preview.json"
TMP_DIR = ROOT / "tmp"

SHORTCUT_NAME = "Sticky Note Widget.lnk"
WINDOW_TITLE = "Sticky Note"

DEFAULT_WINDOW = {"width": 240, "height": 280, "x": None, "y": None}
WINDOW_MARGIN = 18
TRAY_REFRESH_SECONDS = 5.0

#: Native glass.  The window is fully transparent and DWM blurs whatever is
#: behind it; the CSS card only adds a neutral shade, a rim and white text.
#: GLASS_TINT_* are ABGR (alpha + blue/green/red) and both are the same
#: colourless near-black, only thicker — white text needs a darker backdrop, so
#: over a bright desktop the glass densifies instead of turning light.
GLASS_RADIUS = 14
GLASS_TINT_CLEAR = 0x5A181C22
GLASS_TINT_DENSE = 0x90181C22

OK_RGB = (82, 221, 143)
LOW_RGB = (224, 118, 127)
STALE_RGB = (232, 190, 108)
INK_RGB = (11, 18, 28)
NEUTRAL_RGB = (150, 170, 190)

MAX_LOG_BYTES = 256 * 1024


def _force_utf8_console() -> None:
    """Windows consoles and pipes default to a legacy codec (cp1252 here), which
    cannot encode the Chinese holiday names that appear in the payload."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_console()


# --------------------------------------------------------------------------- #
# Logging (the widget normally runs under pythonw, which has no console)
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {message}"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def install_excepthook() -> None:
    def handler(kind, value, tb):
        log("unhandled: " + "".join(traceback.format_exception(kind, value, tb)).strip())

    sys.excepthook = handler
    threading.excepthook = lambda info: handler(info.exc_type, info.exc_value, info.exc_traceback)


# --------------------------------------------------------------------------- #
# Native window treatment (acrylic glass, shape, topmost, show/hide)
# --------------------------------------------------------------------------- #
# All of it goes through user32/dwmapi on the window handle.  The JS bridge, the
# tray menu and the monitor loop all run on worker threads, and touching a .NET
# WinForms control from those threads is what made the switches unreliable —
# the window's TopMost/hide/show are done here instead.

WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
#: Windows 11 backdrop (the supported replacement for the accent below).
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_AUTO = 0
DWMSBT_TRANSIENTWINDOW = 3  # acrylic

#: WinForms' default form colour shows behind the page, and it is light grey.  The
#: page therefore paints its own opaque near-black backdrop, and the transparency
#: comes from a *uniform* window alpha — NOT a colour key: a keyed-out window is
#: click-through, which made the card impossible to drag (input never reached the
#: page).  Alpha keeps hit-testing intact.
GLASS_PAGE_BACKDROP = "#05070a"
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002
GLASS_ALPHA = 140  # 0-255: how much of the card you see over the desktop
GLASS_ALPHA_OFF = 255  # the Glass switch off = a solid card
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8
SWP_FRAMECHANGED = 0x0020
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
VK_LBUTTON = 0x01
SW_RESTORE = 9
SW_MINIMIZE = 6
GWL_WNDPROC = -4
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020

#: Smallest card worth looking at, in CSS pixels.  Narrow and short cards switch
#: to a compact layout; below this the countdown stops being legible.
MIN_SIZE = (200, 84)


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _CompositionAttributeData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(_AccentPolicy)),
        ("SizeOfData", ctypes.c_size_t),
    ]


# Handles are pointers: without explicit restypes ctypes would truncate them to
# 32 bits on 64-bit Python.  Declaring them one by one also means a function that
# is missing on an older Windows build degrades that one feature instead of
# killing the widget at import time.
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_dwmapi = ctypes.windll.dwmapi

#: Held for the process lifetime by :func:`already_running`.
_mutex: int | None = None


def _declare(library, name: str, argtypes: list, restype) -> None:
    try:
        function = getattr(library, name)
    except AttributeError:
        log(f"win32: {name} is not available on this Windows build")
        return
    function.argtypes = argtypes
    function.restype = restype


_declare(_user32, "FindWindowW", [ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_void_p)
_declare(_user32, "GetWindowRect", [ctypes.c_void_p, ctypes.POINTER(_Rect)], ctypes.c_bool)
_declare(
    _user32,
    "SetWindowPos",
    [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ],
    ctypes.c_bool,
)
_declare(_user32, "ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_bool)
_declare(_user32, "GetDpiForWindow", [ctypes.c_void_p], ctypes.c_uint)
_declare(_user32, "GetWindowLongW", [ctypes.c_void_p, ctypes.c_int], ctypes.c_long)
_declare(_user32, "SetWindowLongW", [ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long)
_declare(
    _user32,
    "SetLayeredWindowAttributes",
    [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint],
    ctypes.c_bool,
)
_declare(_user32, "GetCursorPos", [ctypes.POINTER(_Point)], ctypes.c_bool)
_declare(_user32, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short)
_declare(_user32, "IsIconic", [ctypes.c_void_p], ctypes.c_bool)
_declare(_user32, "IsWindowVisible", [ctypes.c_void_p], ctypes.c_bool)
_declare(_user32, "WindowFromPoint", [_Point], ctypes.c_void_p)
_declare(_user32, "GetClassNameW", [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int)
_declare(_user32, "GetAncestor", [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p)
_declare(_user32, "GetTopWindow", [ctypes.c_void_p], ctypes.c_void_p)
_declare(_user32, "GetWindow", [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p)
_declare(_user32, "GetWindowTextW", [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int)
_declare(_user32, "GetWindowThreadProcessId", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_ulong)
_declare(_user32, "EnumWindows", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_bool)
_declare(_user32, "keybd_event", [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_void_p], None)
_declare(
    _user32,
    "SendMessageW",
    [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_longlong],
    ctypes.c_longlong,
)
_declare(
    _user32,
    "SetWindowLongPtrW",
    [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p],
    ctypes.c_void_p,
)
_declare(
    _user32,
    "CallWindowProcW",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_longlong],
    ctypes.c_longlong,
)
_declare(
    _user32,
    "AdjustWindowRectEx",
    [ctypes.POINTER(_Rect), ctypes.c_uint, ctypes.c_bool, ctypes.c_uint],
    ctypes.c_bool,
)
_declare(_user32, "SetWindowRgn", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool], ctypes.c_int)
_declare(
    _user32,
    "SetWindowCompositionAttribute",
    [ctypes.c_void_p, ctypes.POINTER(_CompositionAttributeData)],
    ctypes.c_bool,
)
_declare(_gdi32, "CreateRoundRectRgn", [ctypes.c_int] * 6, ctypes.c_void_p)
_declare(_gdi32, "GetPixel", [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_uint)
_declare(_user32, "GetDC", [ctypes.c_void_p], ctypes.c_void_p)
_declare(_user32, "ReleaseDC", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
_declare(
    _dwmapi,
    "DwmSetWindowAttribute",
    [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint],
    ctypes.c_int,
)


def find_window(title: str, timeout: float = 5.0) -> int | None:
    """The widget's window handle, looked up by exact title."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = _user32.FindWindowW(None, title)
        if handle:
            return int(handle)
        time.sleep(0.1)
    return None


def find_own_window(title: str, timeout: float = 5.0) -> int | None:
    """Our own window, by title *and* process id.

    Two copies of the widget would share the title, the config file and the log,
    and a plain title lookup then hands back the other copy's window — which made
    the UI test drive one window while reading the page of another.  Matching the
    process id removes the ambiguity.
    """
    mine = os.getpid()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(handle: int, _param: int) -> bool:
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(owner))
        if owner.value != mine:
            return True
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(ctypes.c_void_p(handle), buffer, 256)
        if buffer.value == title:
            found.append(int(handle))
            return False
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _user32.EnumWindows(visit, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    return None


MUTEX_NAME = "47Lab.StickyNote.Widget"


def already_running() -> bool:
    """Take the single-instance lock, or report that someone else holds it.

    The handle is deliberately kept for the life of the process (Windows releases
    it when the process ends).
    """
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not _mutex:
        return False
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def window_rect(handle: int) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) in physical pixels."""
    try:
        rect = _Rect()
        if not _user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def window_dpi_scale(handle: int) -> float:
    try:
        dpi = int(_user32.GetDpiForWindow(handle))
        return dpi / 96.0 if dpi > 0 else _dpi_scale()
    except Exception:
        return _dpi_scale()


def _set_accent(handle: int, state: int, tint: int) -> bool:
    """Enable/disable the DWM blur behind a window."""
    try:
        policy = _AccentPolicy(state, 2, tint, 0)
        data = _CompositionAttributeData(
            WCA_ACCENT_POLICY,
            ctypes.pointer(policy),
            ctypes.sizeof(policy),
        )
        return bool(_user32.SetWindowCompositionAttribute(handle, ctypes.byref(data)))
    except Exception as error:
        log(f"could not set the window accent: {error}")
        return False


def _round_window(handle: int, radius: int) -> None:
    """Clip the window (and with it the blur) to a rounded rectangle."""
    rect = window_rect(handle)
    if not rect:
        return
    left, top, right, bottom = rect
    scale = window_dpi_scale(handle)
    physical = max(2, int(round(radius * scale)))
    try:
        region = _gdi32.CreateRoundRectRgn(
            0, 0, right - left + 1, bottom - top + 1, physical * 2, physical * 2
        )
        if not region:
            log("could not create the rounded window region")
            return
        _user32.SetWindowRgn(handle, region, True)
    except Exception as error:
        log(f"could not round the window: {error}")


def _set_backdrop(handle: int, kind: int) -> bool:
    """Ask DWM for a system backdrop (acrylic).  False = this build says no.

    This is the modern path: the accent policy below still returns success on
    Windows 11 but is *ignored*, which silently left the window opaque — the card
    turned milky grey on build 26200 while every call reported success.
    """
    value = ctypes.c_int(kind)
    try:
        result = _dwmapi.DwmSetWindowAttribute(
            handle, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(value), ctypes.sizeof(value)
        )
        return int(result) == 0
    except Exception:
        return False


def _set_window_alpha(handle: int, alpha: int = GLASS_ALPHA) -> bool:
    """Blend the whole window with what is behind it — uniformly, and clickable.

    A colour key would make the keyed pixels click-through and the card could not be
    dragged at all (the page never saw a mousedown).  A uniform alpha keeps
    hit-testing working, and the card is opaque enough that the desktop reads through
    it without washing the text out.
    """
    try:
        ex_style = int(_user32.GetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE))
        if not ex_style & WS_EX_LAYERED:
            _user32.SetWindowLongW(
                ctypes.c_void_p(handle), GWL_EXSTYLE, ex_style | WS_EX_LAYERED
            )
        return bool(
            _user32.SetLayeredWindowAttributes(ctypes.c_void_p(handle), 0, alpha, LWA_ALPHA)
        )
    except Exception as error:
        log(f"could not set the window alpha: {error}")
        return False


def _clear_window_alpha(handle: int) -> None:
    """Back to a fully opaque window (the Glass switch is off)."""
    try:
        _user32.SetLayeredWindowAttributes(ctypes.c_void_p(handle), 0, GLASS_ALPHA_OFF, LWA_ALPHA)
        ex_style = int(_user32.GetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE))
        _user32.SetWindowLongW(
            ctypes.c_void_p(handle), GWL_EXSTYLE, ex_style & ~WS_EX_LAYERED
        )
    except Exception as error:
        log(f"could not restore the window opacity: {error}")


def enable_glass(handle: int, radius: int, tint: int) -> tuple[bool, bool]:
    """Transparent glass.  Returns (blur applied, window blended with the desktop)."""
    # Dark mode first, so any blur tints to match the card rather than to the system
    # theme.
    try:
        dark = ctypes.c_int(1)
        _dwmapi.DwmSetWindowAttribute(
            handle, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), ctypes.sizeof(dark)
        )
    except Exception:
        pass
    blended = _set_window_alpha(handle)
    # Blur behind the card where the build supports it: the supported system backdrop
    # first, and the legacy accent as a fallback.  The accent still reports success on
    # Windows 11 build 26200 and is then ignored, so it must never be the only
    # mechanism.
    blurred = _set_backdrop(handle, DWMSBT_TRANSIENTWINDOW)
    if not blurred:
        blurred = _set_accent(handle, ACCENT_ENABLE_ACRYLICBLURBEHIND, tint)
    try:
        preference = ctypes.c_int(DWMWCP_ROUND)
        _dwmapi.DwmSetWindowAttribute(
            handle,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except Exception:
        pass
    _round_window(handle, radius)
    return blurred, blended


def disable_glass(handle: int) -> None:
    _clear_window_alpha(handle)
    _set_backdrop(handle, DWMSBT_AUTO)
    _set_accent(handle, ACCENT_DISABLED, 0)
    try:
        _user32.SetWindowRgn(handle, None, True)
    except Exception:
        pass


def set_topmost(handle: int, enabled: bool) -> None:
    _user32.SetWindowPos(
        handle,
        HWND_TOPMOST if enabled else HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def set_visible(handle: int, visible: bool) -> None:
    _user32.ShowWindow(handle, SW_SHOWNA if visible else SW_HIDE)


def minimized_or_hidden(handle: int) -> bool:
    try:
        return bool(_user32.IsIconic(handle)) or not bool(_user32.IsWindowVisible(handle))
    except Exception:
        return False


#: Windows' three-finger "show desktop" swipe and Win+D are the same shortcut.
VK_LWIN, VK_D, KEYEVENTF_KEYUP = 0x5B, 0x44, 0x0002

#: The shell's desktop windows.  Only the top-level ones matter for the Z-order
#: walk below; the icon host (SysListView32 / SHELLDLL_DefView) is a child of one.
DESKTOP_CLASSES = ("Progman", "WorkerW")

GA_ROOT = 2
GW_HWNDNEXT = 2


def window_class(handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(ctypes.c_void_p(handle), buffer, 256)
    return buffer.value


def desktop_covering(handle: int) -> bool:
    """Is the card behind the *desktop* (show desktop) rather than behind an app?

    "Show desktop" (Win+D, or the three-finger swipe) does not minimize this card —
    the guard refuses that — but the shell raises the desktop layer above it, so the
    card ends up visible, un-minimized and hidden behind the wallpaper.  That is what
    the user sees when they say "the widget goes too with the desktop".

    This walks the top-level Z-order from the front and reports what it meets first:
    the card itself (fine), a desktop window (buried — lift the card back over it), or
    a normal window (the user's app, which the card is *meant* to sit behind).  A
    WindowFromPoint test cannot be used: the card is a layered window with its
    background punched out, so hit-testing skips straight past it and every point on
    the card reports whatever is behind it — it looked permanently buried.
    """
    current = int(_user32.GetTopWindow(None) or 0)
    while current:
        if current == handle:
            return False
        if window_class(current) in DESKTOP_CLASSES:
            return True
        current = int(_user32.GetWindow(ctypes.c_void_p(current), GW_HWNDNEXT) or 0)
    return False


def desktop_in_front() -> bool:
    """Is the shell's desktop what the screen is showing?  (Win+D / the swipe.)

    Asked at points spread across the screen rather than about the card itself.  The
    card's own Z-order decided whether the card was *covered*, which flapped: lifting
    the card made it "not buried", the topmost flag went straight back, and the two
    states alternated once per poll.  A majority of desktop hits across the screen
    means the desktop really is showing, and the answer does not change when the card
    moves in the Z-order.
    """
    width = int(_user32.GetSystemMetrics(0))
    height = int(_user32.GetSystemMetrics(1))
    if width <= 0 or height <= 0:
        return False
    points = ((0.5, 0.5), (0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7))
    hits = 0
    for fraction_x, fraction_y in points:
        point = _Point(int(width * fraction_x), int(height * fraction_y))
        target = int(_user32.WindowFromPoint(point) or 0)
        if not target:
            continue
        root = int(_user32.GetAncestor(ctypes.c_void_p(target), GA_ROOT) or target)
        if window_class(root) in DESKTOP_CLASSES or window_class(target) in (
            "SysListView32",
            "SHELLDLL_DefView",
        ):
            hits += 1
    return hits * 2 > len(points)


#: Windows that are part of the shell rather than the user's work.  They sit near the
#: front and must not count as "an app window" when the card is placed back.
NON_APP_CLASSES = DESKTOP_CLASSES + (
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "Windows.UI.Core.CoreWindow",
    "XamlExplorerHostIslandWindow",
    "TaskListThumbnailWnd",
    "MultitaskingViewFrame",
    "ForegroundStaging",
)


def send_to_desktop_layer(handle: int) -> bool:
    """Put the card back on the desktop: below every app window, above the wallpaper.

    Clearing WS_EX_TOPMOST is not enough.  The card keeps the Z-order slot it was
    lifted to, so it stayed in front of the user's windows — which is exactly what
    "press Win+D twice and the widget is on top of everything" looked like.  Windows'
    own answer is to sit immediately above the desktop, so this finds the lowest app
    window (the one directly above the desktop) and inserts the card just below it.
    """
    target = 0
    current = int(_user32.GetTopWindow(None) or 0)
    while current:
        if current != handle and is_app_window(current):
            target = current  # keep walking: the last app window is the lowest one
        current = int(_user32.GetWindow(ctypes.c_void_p(current), GW_HWNDNEXT) or 0)
    if not target:
        return False
    _user32.SetWindowPos(
        ctypes.c_void_p(handle),
        ctypes.c_void_p(target),
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )
    return True


def is_app_window(handle: int) -> bool:
    """Is this one of the user's own windows?

    The shell is full of near-invisible helpers that sit at the front (IME windows,
    thumbnails, staging hosts) and they are NOT in the documented desktop classes, so
    a class list alone is not enough: those helpers were picked as "the lowest window"
    and the card was inserted under them.  Visible, not minimized, not a tool window and
    carrying a title is what an app looks like.
    """
    if not handle:
        return False
    if not bool(_user32.IsWindowVisible(handle)) or bool(_user32.IsIconic(handle)):
        return False
    if window_class(handle) in NON_APP_CLASSES:
        return False
    if int(_user32.GetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE)) & WS_EX_TOOLWINDOW:
        return False
    title = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(ctypes.c_void_p(handle), title, 256)
    return bool(title.value.strip())


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_ulonglong,
    ctypes.c_longlong,
)


class MinimizeGuard:
    """Keep the card on screen when Windows shows the desktop.

    "Show desktop" (Win+D, or the three-finger swipe) minimizes or hides every
    top-level window, which used to take the widget with it.  This wraps the
    window procedure and refuses SC_MINIMIZE; :func:`minimized_or_hidden` plus
    the watcher catch the shell paths that do not send that message.
    """

    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.enabled = True
        self.installed = False
        self._previous = 0
        self._callback = WNDPROC(self._handler)

    def install(self) -> bool:
        setter = getattr(_user32, "SetWindowLongPtrW", None) or getattr(
            _user32, "SetWindowLongW", None
        )
        if setter is None:
            return False
        try:
            self._previous = setter(
                self.handle, GWL_WNDPROC, ctypes.cast(self._callback, ctypes.c_void_p)
            )
            self.installed = bool(self._previous)
        except Exception as error:
            log(f"could not hook the window procedure: {error}")
            self.installed = False
        return self.installed

    def _handler(self, hwnd, message, wparam, lparam):
        try:
            if (
                self.enabled
                and self.installed
                and message == WM_SYSCOMMAND
                and (wparam & 0xFFF0) == SC_MINIMIZE
            ):
                return 0
        except Exception:
            pass
        if not self.installed:
            return 0
        return _user32.CallWindowProcW(self._previous, hwnd, message, wparam, lparam)


def fit_window(handle: int, width: int, height: int, note: str = "") -> tuple[int, int] | None:
    """Force the *client* area to ``width`` x ``height`` CSS pixels.

    pywebview asks WinForms for ``size * dpi_scale`` physical pixels, but the
    form then re-runs its own DPI auto-scaling after it appears: a 404x176
    request arrived as a 390x139 CSS viewport, which pushed the bottom of the
    card outside the window.  Anything that lives below the window's edge is
    unclickable, so the size has to be exact.  AdjustWindowRectEx converts the
    client size we want into the outer size the window needs, frame or not.
    """
    scale = window_dpi_scale(handle)
    client = (int(round(width * scale)), int(round(height * scale)))

    rect = window_rect(handle)
    if rect and (rect[2] - rect[0], rect[3] - rect[1]) == client:
        log(f"window size confirmed {client[0]}x{client[1]} physical ({note})")
        return client
    if not set_client_size_pixels(handle, client[0], client[1]):
        log(f"could not set the window size ({note})")
        return None
    log(
        f"window forced to {client[0]}x{client[1]} physical client "
        f"= {width}x{height} CSS at {scale:g}x ({note})"
    )
    return client


def set_client_size_pixels(handle: int, width: int, height: int) -> bool:
    """Set the client area to an exact physical pixel size.

    AdjustWindowRectEx turns the client size we want into the outer size the
    window needs, so this stays correct whether or not the window has a frame.
    """
    style = int(_user32.GetWindowLongW(handle, GWL_STYLE))
    ex_style = int(_user32.GetWindowLongW(handle, GWL_EXSTYLE))
    bounds = _Rect(0, 0, int(width), int(height))
    if not _user32.AdjustWindowRectEx(ctypes.byref(bounds), style, False, ex_style):
        bounds = _Rect(0, 0, int(width), int(height))
    return bool(
        _user32.SetWindowPos(
            handle,
            None,
            0,
            0,
            bounds.right - bounds.left,
            bounds.bottom - bounds.top,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    )


def set_client_size(handle: int, css_width: int, css_height: int) -> bool:
    """As above, but in CSS pixels — the unit the page's layout is written in."""
    scale = window_dpi_scale(handle)
    return set_client_size_pixels(
        handle, int(round(css_width * scale)), int(round(css_height * scale))
    )


def hide_from_taskbar(handle: int) -> bool:
    try:
        ex_style = int(_user32.GetWindowLongW(handle, GWL_EXSTYLE))
        wanted = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if wanted != ex_style:
            _user32.SetWindowLongW(handle, GWL_EXSTYLE, wanted)
            _user32.SetWindowPos(
                handle,
                None,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
        return bool(wanted & WS_EX_TOOLWINDOW)
    except Exception as error:
        log(f"could not hide the window from the taskbar: {error}")
        return False


def cursor_position() -> tuple[int, int] | None:
    try:
        point = _Point()
        if _user32.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
    except Exception:
        pass
    return None


def synthetic_path(dx: int, dy: int, steps: int = 4, origin: tuple[int, int] | None = None):
    """A fake cursor+button sampler for the UI test.

    Walks ``dx``/``dy`` physical pixels over a few frames, then reports the
    button released so the gesture loop finishes — the same loop a real drag
    runs, without moving the user's cursor.  Pass a fixed ``origin``: reading the
    live cursor made this test depend on the real mouse, so a nudge of the mouse
    mid-run threw the round trip off by a few pixels.
    """
    raw = origin or cursor_position() or (0, 0)
    # Win32 wants whole pixels, and a work area can arrive as floats.
    start = (int(raw[0]), int(raw[1]))
    state = {"step": 0}

    def sample() -> tuple[tuple[int, int], bool]:
        state["step"] += 1
        if state["step"] > steps:
            return start, False
        factor = state["step"] / steps
        return (start[0] + int(dx * factor), start[1] + int(dy * factor)), True

    return sample


def left_button_down() -> bool:
    try:
        return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
    except Exception:
        return False


def backdrop_luminance(handle: int) -> tuple[float, int] | None:
    """Average brightness of the desktop just outside the card.

    The pixels *behind* the card cannot be read — the card is drawn on top of
    them — so a ring around the window is sampled instead.  A window's immediate
    surroundings are almost always representative of what is under it, and
    points that fall off-screen are skipped.
    """
    rect = window_rect(handle)
    if not rect:
        return None
    left, top, right, bottom = rect
    middle_x, middle_y = (left + right) // 2, (top + bottom) // 2
    points = (
        [(left - 10, top - 26), (right + 10, top - 26), (middle_x, top - 26)]
        + [(left - 10, bottom + 26), (right + 10, bottom + 26), (middle_x, bottom + 26)]
        + [(left - 26, top - 10), (left - 26, bottom + 10), (left - 26, middle_y)]
        + [(right + 26, top - 10), (right + 26, bottom + 10), (right + 26, middle_y)]
    )

    hdc = _user32.GetDC(None)
    if not hdc:
        return None
    total = 0.0
    count = 0
    try:
        get_pixel = _gdi32.GetPixel
        for x, y in points:
            colour = int(get_pixel(hdc, x, y))
            if colour == 0xFFFFFFFF or colour < 0:  # CLR_INVALID / off-screen
                continue
            total += relative_luminance(
                colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF
            )
            count += 1
    except Exception as error:
        log(f"could not sample the backdrop: {error}")
        return None
    finally:
        _user32.ReleaseDC(None, hdc)
    return (total / count, count) if count else None


def relative_luminance(red: int, green: int, blue: int) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _rect_to_geometry(handle: int) -> dict | None:
    rect = window_rect(handle)
    if not rect:
        return None
    left, top, right, bottom = rect
    scale = window_dpi_scale(handle)
    return {
        "x": int(round(left / scale)),
        "y": int(round(top / scale)),
        "width": int(round((right - left) / scale)),
        "height": int(round((bottom - top) / scale)),
    }


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    defaults = {
        "on_top": True,
        "notify": True,
        "autostart": True,
        "glass": True,
        "stay_on_desktop": True,
        "draggable": True,
        "auto_density": True,
        "window": dict(DEFAULT_WINDOW),
    }
    try:
        # utf-8-sig: a config edited by another tool (PowerShell's Set-Content) can start
        # with a BOM, and json.loads rejects that -- which silently fell back to the
        # defaults and reset every preference, "always on top" included.
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return defaults
    merged = {**defaults, **{key: value for key, value in raw.items() if key in defaults}}
    merged["window"] = {**DEFAULT_WINDOW, **(raw.get("window") or {})}
    return merged


def save_config(config: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        log(f"could not save config: {error}")


# --------------------------------------------------------------------------- #
# Start with Windows (a shortcut in the user's Startup folder)
# --------------------------------------------------------------------------- #


def startup_shortcut() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / SHORTCUT_NAME
    )


def autostart_installed() -> bool:
    try:
        return startup_shortcut().exists()
    except OSError:
        return False


def _pythonw() -> Path:
    """The windowless interpreter, so autostart does not flash a console."""
    candidate = Path(sys.executable)
    windowless = candidate.with_name("pythonw.exe")
    return windowless if windowless.exists() else candidate


def set_autostart(enabled: bool) -> bool:
    shortcut = startup_shortcut()
    if not enabled:
        try:
            shortcut.unlink(missing_ok=True)
            log("autostart removed")
            return True
        except OSError as error:
            log(f"could not remove autostart shortcut: {error}")
            return False

    script = TMP_DIR / "autostart.ps1"
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    icon = ICON_FILE if ICON_FILE.exists() else _pythonw()
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$link = (New-Object -ComObject WScript.Shell).CreateShortcut('{shortcut}')\n"
        f"$link.TargetPath = '{_pythonw()}'\n"
        f"$link.Arguments = '\"{ROOT / 'app.py'}\"'\n"
        f"$link.WorkingDirectory = '{ROOT}'\n"
        f"$link.IconLocation = '{icon}'\n"
        "$link.Description = 'DeepSeek wallet balance widget'\n"
        "$link.Save()\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log(f"autostart installed -> {shortcut}")
        return True
    except (subprocess.SubprocessError, OSError) as error:
        detail = getattr(error, "stderr", b"") or b""
        log(f"could not install autostart: {error} {detail.decode('utf-8', 'replace').strip()}")
        return False


# --------------------------------------------------------------------------- #
# Geometry: park the widget in a corner of the primary work area
# --------------------------------------------------------------------------- #


def _dpi_scale() -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        return dpi / 96.0 if dpi > 0 else 1.0
    except Exception:
        return 1.0


def primary_work_area() -> tuple[float, float, float, float] | None:
    """Primary monitor work area in *logical* pixels (taskbar excluded)."""
    try:
        rect = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None
        scale = _dpi_scale()
        return (
            rect.left / scale,
            rect.top / scale,
            rect.right / scale,
            rect.bottom / scale,
        )
    except Exception as error:
        log(f"could not read the work area: {error}")
        return None


def initial_position(config: dict, size: tuple[int, int]) -> tuple[int, int] | None:
    width, height = int(size[0]), int(size[1])
    area = primary_work_area()
    saved = (config["window"].get("x"), config["window"].get("y"))

    if area and saved[0] is not None and saved[1] is not None:
        left, top, right, bottom = area
        x, y = float(saved[0]), float(saved[1])
        # keep a previous position usable after a monitor or resolution change
        x = min(max(x, left), max(left, right - width))
        y = min(max(y, top), max(top, bottom - height))
        return int(x), int(y)

    if saved[0] is not None and saved[1] is not None:
        return int(saved[0]), int(saved[1])

    if not area:
        return None
    left, top, right, bottom = area
    return int(right - width - WINDOW_MARGIN), int(bottom - height - WINDOW_MARGIN)


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #


def _dot(colour: tuple[int, int, int]) -> Image.Image:
    """A flat, legible status dot: mint = usable, coral = out of credit, amber = old."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((3, 3, size - 4, size - 4), fill=(*colour, 255))
    draw.ellipse((19, 19, size - 20, size - 20), fill=(*INK_RGB, 235))
    draw.ellipse((27, 27, size - 28, size - 28), fill=(*colour, 255))
    return image


def state_colour(state: str) -> tuple[int, int, int]:
    if state == "low":
        return LOW_RGB
    if state in ("stale", "offline", "no_key", "auth", "empty", "error"):
        return STALE_RGB
    return OK_RGB


def tray_image(state: str = "ok") -> Image.Image:
    return _dot(state_colour(state))


def ensure_icon_file() -> Path | None:
    """A multi-size .ico for the shortcut and the taskbar."""
    if ICON_FILE.exists():
        return ICON_FILE
    try:
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        image = _dot(NEUTRAL_RGB)
        image.save(
            ICON_FILE,
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        return ICON_FILE
    except Exception as error:
        log(f"could not write the icon: {error}")
        return None


# --------------------------------------------------------------------------- #
# The widget
# --------------------------------------------------------------------------- #


class Widget:
    def __init__(self) -> None:
        self.config = load_config()
        # The note lives in a file beside the widget, so the checklist survives a
        # restart, a crash, and the user turning the PC off mid-sentence.
        self.note = engine.Note(ROOT / "notes.json")
        self.window: webview.Window | None = None
        self.icon: pystray.Icon | None = None
        self.hwnd: int | None = None
        self.visible = True
        self.shown = False
        self.stop = threading.Event()
        self._tray_at = 0.0
        self._size_check_running = False
        self._gesture_active = False
        self.guard: MinimizeGuard | None = None
        self._restores = 0
        self._on_desktop_layer = False
        # The glass is re-applied once the page is alive; see boot_report.
        self._glass_ready = False
        self.glass_on = bool(self.config.get("glass", True))
        # Glass density, sampled from the desktop (see watch_glass).
        self.glass_mode = "clear"

    # ---------------------------------------------------------------- state

    def summary(self) -> str:
        done, total = self.note.counts()
        if not total:
            return "empty note"
        return f"{done}/{total} done"

    def payload(self) -> dict:
        """What the card renders. Built from the file, never from the page, so the
        note on screen is always the note on disk."""
        done, total = self.note.counts()
        return {
            "items": self.note.as_payload(),
            "done": done,
            "total": total,
            "empty": not self.note.items,
            "summary": self.summary(),
            "prefs": self.prefs(),
            "glass": self.glass_mode,
            "glass_enabled": self.glass_on,
            "limits": {"text": engine.MAX_TEXT, "items": engine.MAX_ITEMS},
        }

    #: The foot has to fit a 200px card, so it is money + the time of the reading;
    #: the tooltip carries the sentence.
    def footer(self, reading: engine.Reading | None, state: str) -> str:
        if reading is None:
            if self.error is None:
                return "click the card to read the wallet"
            if self.error.kind == "no_key":
                return f"save the key in {engine.DEFAULT_KEY_FILE.parent.name}"
            return str(self.error)
        if state in ("auth", "empty", "error"):
            return str(self.error)
        if state == "low":
            return "out of credit \u00b7 " + engine.clock(reading.at)
        if reading.topup is not None:
            return f"topped up {engine.money(reading.topup, reading.currency)} \u00b7 {engine.clock(reading.at)}"
        if reading.spent is None:
            return f"first reading \u00b7 {engine.clock(reading.at)}"
        if reading.spent == 0:
            return f"no spend yet \u00b7 {engine.clock(reading.at)}"
        return f"spent {engine.money(reading.spent, reading.currency)} \u00b7 {engine.clock(reading.at)}"

    def tooltip(self, reading: engine.Reading | None, state: str) -> str:
        if reading is None:
            if self.error is not None and self.error.kind == "no_key":
                return f"No API key yet.\nSave it in: {engine.DEFAULT_KEY_FILE}"
            return str(self.error) if self.error else "No reading yet"
        parts = [
            f"Read {engine.clock(reading.at)} ({engine.humanise(time.time() - reading.at)})",
            f"Granted {engine.money(reading.granted, reading.currency)}\nTopped up {engine.money(reading.topped_up, reading.currency)}",
        ]
        if reading.spent is not None and reading.spent > 0:
            parts.insert(1, f"Spent {engine.money(reading.spent, reading.currency)} since the previous reading")
        if reading.topup is not None:
            parts.insert(1, f"Topped up {engine.money(reading.topup, reading.currency)} since the previous reading")
        window = self.store.spent_within(30)
        if window is not None:
            parts.append(f"Spent {engine.money(window, reading.currency)} in 30 days")
        if self.error is not None:
            parts.append(f"Last refresh failed: {self.error}")
        parts.append(f"{reading.currency} \u00b7 click to refresh")
        return "\n".join(parts)

    def prefs(self) -> dict:
        return {
            "on_top": bool(self.config["on_top"]),
            "autostart": autostart_installed(),
            "glass": bool(self.config.get("glass", True)),
            "stay_on_desktop": bool(self.config.get("stay_on_desktop", True)),
            "draggable": bool(self.config.get("draggable", True)),
        }

    def save(self) -> None:
        save_config(self.config)

    def push(self) -> None:
        """Send a fresh payload to the page (used when the price flips)."""
        if not self.window:
            return
        try:
            blob = json.dumps(self.payload(), ensure_ascii=False)
            self.window.evaluate_js(f"window.__widgetPush && window.__widgetPush({blob})")
        except Exception as error:
            log(f"could not push state to the page: {error}")

    # --------------------------------------------------------------- window

    def set_visible(self, visible: bool) -> None:
        self.visible = visible
        try:
            if self.hwnd:
                set_visible(self.hwnd, visible)
                if visible and not self._glass_ready:
                    return  # still starting up; the page-ready pass handles it
                if visible:
                    # Showing a window again can drop the DWM backdrop with it.
                    self.set_glass(bool(self.config.get("glass", True)), why="shown again")
            elif self.window and visible:
                self.window.show()
            elif self.window:
                self.window.hide()
            log("shown" if visible else "hidden to tray")
        except Exception as error:
            log(f"could not change visibility: {error}")

    def hide(self) -> None:
        self.set_visible(False)

    def toggle_visible(self, *_args) -> None:
        self.set_visible(not self.visible)
        self.refresh_tray()

    def set_on_top(self, enabled: bool) -> None:
        self.config["on_top"] = bool(enabled)
        self.save()
        try:
            if self.hwnd:
                set_topmost(self.hwnd, bool(enabled))
            elif self.window:
                self.window.on_top = bool(enabled)
            log(f"always on top -> {enabled}")
        except Exception as error:
            log(f"could not change always-on-top: {error}")
        self.refresh_tray()

    def glass_tint(self) -> int:
        return GLASS_TINT_DENSE if self.glass_mode == "dense" else GLASS_TINT_CLEAR

    def set_glass(self, enabled: bool, why: str = "") -> None:
        self.config["glass"] = bool(enabled)
        self.glass_on = bool(enabled)
        self.save()
        if not self.hwnd:
            log("glass preference saved (window handle not available yet)")
            return
        reason = f", {why}" if why else ""
        try:
            if enabled:
                blurred, blended = enable_glass(self.hwnd, GLASS_RADIUS, self.glass_tint())
                log(
                    f"glass on (blur={'yes' if blurred else 'no'}, "
                    f"alpha={'yes' if blended else 'no'}{reason})"
                )
            else:
                disable_glass(self.hwnd)
                log("glass off")
        except Exception as error:
            log(f"could not change the glass: {error}")

    def apply_glass_mode(self) -> None:
        """Re-tint the native blur and tell the page which density to use."""
        if self.hwnd and self.config.get("glass", True):
            enable_glass(self.hwnd, GLASS_RADIUS, self.glass_tint())
        self.push()

    def settle_glass(self) -> None:
        """Re-apply the glass a few times while the window finishes appearing.

        Applying it once is not reliable: pywebview's transparent-window start-up
        hides and re-shows the window, and DWM drops the backdrop when that happens,
        leaving the card opaque and milky with nothing in the log to show for it.
        Re-running it is idempotent and costs nothing, so: page ready, then settle.
        """
        for label, delay in (("page ready", 0.0), ("+0.5s", 0.5), ("+2s", 1.5), ("+5s", 3.0)):
            if delay:
                time.sleep(delay)
            if self.stop.is_set():
                return
            self.set_glass(bool(self.config.get("glass", True)), why=label)

    # --------------------------------------------------------------- wallet

    def refresh_balance(self, why: str, wait: bool = False) -> None:
        """Read the wallet. The ONLY place in the host that touches the network.

        Called from a card click, the tray item or --dump-preview, never on a
        timer: the widget is silent until it is asked, which is the design.
        """
        if self.refreshing:
            return
        self.refreshing = True
        self.push()  # show the spinner state straight away

        def work() -> None:
            try:
                previous = self.reading
                reading = engine.fetch(engine.read_key() or "")
                reading = engine.with_spend(previous, reading)
                self.store.append(reading)
                self.reading = reading
                self.error = None
                change = ""
                if reading.spent:
                    change = f", spent {engine.money(reading.spent, reading.currency)}"
                elif reading.topup:
                    change = f", topped up {engine.money(reading.topup, reading.currency)}"
                log(f"refresh ({why}): {engine.money(reading.total, reading.currency)}{change}")
                if previous is not None and previous.is_available and not reading.is_available:
                    self.warn_low()
            except engine.BalanceError as error:
                self.error = error
                log(f"refresh ({why}) failed: {error} [{error.kind}]")
            except Exception:
                self.error = engine.BalanceError("unexpected failure")
                log("refresh error:\n" + traceback.format_exc())
            finally:
                self.refreshing = False
                self.push()
                self.refresh_tray()

        thread = threading.Thread(target=work, name="refresh", daemon=True)
        thread.start()
        if wait:
            thread.join(timeout=30)

    def warn_low(self) -> None:
        """One notification, the moment the wallet stops covering API calls."""
        if not bool(self.config["notify"]) or not self.icon:
            return
        try:
            self.icon.notify(
                "API calls will fail until you top up.",
                "DeepSeek balance is empty",
            )
        except Exception as error:
            log(f"notification failed: {error}")

    def watch_glass(self) -> None:
        """Keep the glass density matched to the desktop behind the card.

        The card is genuinely see-through and the text is white, so the only
        thing that has to change on a bright backdrop is how thick the glass is —
        the material itself stays the same colourless dark glass.  Sampling is
        skipped mid-gesture: re-tinting re-applies the window frame, which nudges
        a window the user is dragging by a pixel.
        """
        log("glass watch started")
        time.sleep(2.0)
        while not self.stop.is_set():
            try:
                self.refresh_glass_mode()
            except Exception:
                log("glass watch error:\n" + traceback.format_exc())
            self.stop.wait(1.5)

    def sample_glass_mode(self) -> tuple[str | None, float, int]:
        """How thick the glass needs to be right now (None inside the dead zone)."""
        sample = backdrop_luminance(self.hwnd) if self.hwnd else None
        if not sample:
            return None, 0.0, 0
        luminance, count = sample
        if luminance >= 0.55:
            return "dense", luminance, count
        if luminance <= 0.40:
            return "clear", luminance, count
        # In between the card keeps what it has, so a window moving past does not
        # make it flicker.
        return None, luminance, count

    def refresh_glass_mode(self) -> None:
        if self._gesture_active or not bool(self.config.get("auto_density", True)):
            return
        wanted, luminance, count = self.sample_glass_mode()
        if wanted and wanted != self.glass_mode:
            self.glass_mode = wanted
            log(f"glass -> {wanted} (backdrop luminance {luminance:.2f} from {count} samples)")
            self.apply_glass_mode()

    def desired_size(self) -> tuple[int, int]:
        """The CSS size the card should be.  Resizing writes here, so the size
        the user picked survives a restart."""
        size = self.config["window"]
        width = int(size.get("width") or DEFAULT_WINDOW["width"])
        height = int(size.get("height") or DEFAULT_WINDOW["height"])
        area = primary_work_area()
        if area:
            width = min(width, int(area[2] - area[0]))
            height = min(height, int(area[3] - area[1]))
        return max(MIN_SIZE[0], width), max(MIN_SIZE[1], height)

    def set_size_css(self, width: int, height: int, note: str = "resize") -> bool:
        """Resize the card, clamp it to something usable, and remember it."""
        if not self.hwnd:
            return False
        area = primary_work_area()
        width = max(MIN_SIZE[0], int(width))
        height = max(MIN_SIZE[1], int(height))
        if area:
            width = min(width, int(area[2] - area[0]))
            height = min(height, int(area[3] - area[1]))
        if not fit_window(self.hwnd, width, height, note):
            return False
        geometry = _rect_to_geometry(self.hwnd) or {}
        self.config["window"] = {
            "x": geometry.get("x"),
            "y": geometry.get("y"),
            "width": width,
            "height": height,
        }
        self.save()
        if self.config.get("glass", True):
            _round_window(self.hwnd, GLASS_RADIUS)
        return True

    # -------------------------------------------------------------- gestures

    def begin_move(self) -> None:
        """Start a window drag (called on mousedown over the card)."""
        if not self.can_drag("move"):
            return
        threading.Thread(target=self._gesture, args=("move",), daemon=True).start()

    def begin_resize(self) -> None:
        """Start a resize (called on mousedown over the corner grip)."""
        if not self.can_drag("resize"):
            return
        threading.Thread(target=self._gesture, args=("size",), daemon=True).start()

    def can_drag(self, gesture: str) -> bool:
        """Honour the Draggable lock.  The host decides, not the page: a page-side
        guard could be bypassed by any stray script call, and the page and the host
        would then disagree about whether the card may be moved."""
        if bool(self.config.get("draggable", True)):
            return True
        log(f"{gesture} ignored (draggable is off)")
        return False

    def _gesture(self, mode: str, sample=None) -> None:
        """Move or resize while the left button is held.

        pywebview's own drag never moved this window, and the OS resize loop
        cannot run either because a frameless window has no WS_THICKFRAME.  So
        both gestures live here: a worker thread polls the cursor and repositions
        the window directly.

        ``sample`` swaps the input source so the UI test can drive this exact
        loop with synthetic steps instead of the real mouse.
        """
        if not self.hwnd:
            return
        read = sample or (lambda: (cursor_position(), left_button_down()))
        start_point, pressed = read()
        start_rect = window_rect(self.hwnd)
        if not start_point or not start_rect or not pressed:
            return

        self._gesture_active = True
        changed = False
        last_cut = 0.0
        area = primary_work_area()
        max_width = int(area[2] - area[0]) if area else 4096
        max_height = int(area[3] - area[1]) if area else 4096
        try:
            while True:
                point, pressed = read()
                if not point or not pressed:
                    break
                dx = point[0] - start_point[0]
                dy = point[1] - start_point[1]
                if mode == "move":
                    _user32.SetWindowPos(
                        self.hwnd,
                        None,
                        start_rect[0] + dx,
                        start_rect[1] + dy,
                        0,
                        0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                    )
                else:
                    scale = window_dpi_scale(self.hwnd) or 1.0
                    width_css = max(
                        MIN_SIZE[0],
                        int(round((start_rect[2] - start_rect[0]) / scale)) + int(round(dx / scale)),
                    )
                    height_css = max(
                        MIN_SIZE[1],
                        int(round((start_rect[3] - start_rect[1]) / scale)) + int(round(dy / scale)),
                    )
                    # whole CSS pixels, so the remembered size and the page's
                    # viewport never disagree by a rounded-off pixel
                    set_client_size(
                        self.hwnd,
                        min(width_css, max_width),
                        min(height_css, max_height),
                    )
                    now = time.monotonic()
                    if now - last_cut > 0.15 and self.config.get("glass", True):
                        _round_window(self.hwnd, GLASS_RADIUS)
                        last_cut = now
                changed = changed or bool(dx or dy)
                time.sleep(0.008)  # ~120 Hz
        finally:
            self._gesture_active = False

        if not changed:
            return
        if mode == "size":
            geometry = _rect_to_geometry(self.hwnd)
            if geometry:
                # snap to whole CSS pixels so the remembered size and the page's
                # viewport agree exactly
                self.set_size_css(geometry["width"], geometry["height"], "gesture")
        elif self.config.get("glass", True):
            _round_window(self.hwnd, GLASS_RADIUS)
        geometry = _rect_to_geometry(self.hwnd) or {}
        log(
            f"{mode} gesture done -> {geometry.get('x')},{geometry.get('y')} "
            f"{geometry.get('width')}x{geometry.get('height')} CSS"
        )

    def remember_geometry(self) -> None:
        if not self.shown:
            return
        geometry = _rect_to_geometry(self.hwnd) if self.hwnd else None
        if geometry:
            self.config["window"] = geometry
            self.save()
            return
        try:
            if self.window:
                self.config["window"] = {
                    "x": int(self.window.x),
                    "y": int(self.window.y),
                    "width": int(DEFAULT_WINDOW["width"]),
                    "height": int(DEFAULT_WINDOW["height"]),
                }
                self.save()
        except Exception as error:
            log(f"could not remember the window position: {error}")

    def quit(self, *_args) -> None:
        log("quit requested")
        self.stop.set()
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        try:
            self.remember_geometry()
            if self.window:
                self.window.destroy()
        except Exception as error:
            log(f"destroy failed: {error}")
        # safety net: never leave an invisible process behind
        threading.Timer(3.0, lambda: os._exit(0)).start()

    # ----------------------------------------------------------------- tray

    def refresh_tray(self) -> None:
        self._tray_at = 0.0
        self.update_tray()

    def update_tray(self, force: bool = False) -> None:
        if not self.icon:
            return
        now = time.monotonic()
        if not force and now - self._tray_at < TRAY_REFRESH_SECONDS:
            return
        self._tray_at = now
        try:
            state = self.payload()
            self.icon.title = "Sticky Note — " + str(state["summary"])
            if force or getattr(self, "_tray_state", None) != (state["done"], state["total"]):
                self._tray_state = (state["done"], state["total"])
                self.icon.icon = tray_image("ok" if state["total"] - state["done"] else "done")
            self.icon.update_menu()
        except Exception as error:
            log(f"tray update failed: {error}")

    def start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("New item", self.new_item_from_tray, default=True),
            pystray.MenuItem("Show / hide widget", self.toggle_visible),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Always on top",
                self.toggle_on_top,
                checked=lambda _item: bool(self.config["on_top"]),
            ),
            pystray.MenuItem(
                "Start with Windows",
                self.toggle_autostart,
                checked=lambda _item: autostart_installed(),
            ),
            pystray.MenuItem(
                "Stay on desktop",
                self.toggle_stay_on_desktop,
                checked=lambda _item: bool(self.config.get("stay_on_desktop", True)),
            ),
            pystray.MenuItem(
                "Draggable",
                self.toggle_draggable,
                checked=lambda _item: bool(self.config.get("draggable", True)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open widget folder", self.open_folder),
            pystray.MenuItem("Quit", self.quit),
        )
        try:
            self.icon = pystray.Icon(
                "deepseek-balance",
                tray_image("ok"),
                "DeepSeek balance",
                menu,
            )
            self.icon.run_detached()
            log("tray icon started")
        except Exception as error:
            log(f"could not start the tray icon: {error}")
            self.icon = None
        self.update_tray(force=True)

    def new_item_from_tray(self, *_args) -> None:
        self.add_item("New item")

    # ------------------------------------------------------------- the note

    def add_item(self, text: object = "", *, at_top: bool = False) -> dict:
        """Add one item and tell the page. Empty text is refused by the engine."""
        self.note.add(text, at_top=at_top)
        return self.push_note()

    def toggle_item(self, item_id: int) -> dict:
        self.note.toggle(int(item_id))
        return self.push_note()

    def edit_item(self, item_id: int, text: object) -> dict:
        # Emptying a line deletes it, which is what pressing Enter on a cleared row
        # should do (notes.set_text decides that).
        self.note.set_text(int(item_id), text)
        return self.push_note()

    def remove_item(self, item_id: int) -> dict:
        self.note.remove(int(item_id))
        return self.push_note()

    def push_note(self) -> dict:
        """Send the note to the page and refresh the tray, in one place."""
        payload = self.payload()
        if self.window:
            try:
                blob = json.dumps(payload, ensure_ascii=False)
                self.window.evaluate_js(f"window.__widgetPush && window.__widgetPush({blob})")
            except Exception as error:
                log(f"could not push the note to the page: {error}")
        self.refresh_tray()
        return payload

    def toggle_on_top(self, *_args) -> None:
        self.set_on_top(not bool(self.config["on_top"]))

    def toggle_notify(self, *_args) -> None:
        self.config["notify"] = not bool(self.config["notify"])
        self.save()
        log(f"notify -> {self.config['notify']}")
        self.refresh_tray()

    def toggle_stay_on_desktop(self, *_args) -> None:
        wanted = not bool(self.config.get("stay_on_desktop", True))
        self.config["stay_on_desktop"] = wanted
        if self.guard:
            self.guard.enabled = wanted
        self.save()
        log(f"stay on desktop -> {wanted}")
        self.refresh_tray()

    def toggle_draggable(self, *_args) -> None:
        """Lock the card where it is.  Once off, the host refuses both gestures, so
        the card cannot be nudged by accident."""
        wanted = not bool(self.config.get("draggable", True))
        self.config["draggable"] = wanted
        self.save()
        log(f"draggable -> {wanted}")
        self.push()
        self.refresh_tray()

    def toggle_autostart(self, *_args) -> None:
        wanted = not autostart_installed()
        if set_autostart(wanted):
            self.config["autostart"] = wanted
            self.save()
        self.refresh_tray()

    def open_folder(self, *_args) -> None:
        try:
            os.startfile(ROOT)  # noqa: S606 - opening our own folder
        except Exception as error:
            log(f"could not open the folder: {error}")

    # -------------------------------------------------------------- monitor

    def monitor(self) -> None:
        """Keep the tray tooltip honest. No network here: readings are explicit."""
        log("monitor started")
        while not self.stop.is_set():
            try:
                self.update_tray()
            except Exception:
                log("monitor error:\n" + traceback.format_exc())
            self.stop.wait(5.0)

    def watch_desktop(self) -> None:
        """Undo a shell-initiated minimize (show desktop / three-finger swipe).

        Only while the card is *meant* to be visible, so hiding it to the tray
        still works.
        """
        log("desktop watch started")
        time.sleep(1.5)  # let the window finish appearing before judging it
        while not self.stop.is_set():
            try:
                if (
                    bool(self.config.get("stay_on_desktop", True))
                    and self.visible
                    and self.hwnd
                ):
                    if minimized_or_hidden(self.hwnd):
                        # SW_SHOWNOACTIVATE: come back without stealing focus
                        _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                        _user32.SetWindowPos(
                            self.hwnd,
                            HWND_TOPMOST if bool(self.config["on_top"]) else HWND_NOTOPMOST,
                            0,
                            0,
                            0,
                            0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                        )
                        self._restores += 1
                        if self._restores <= 3:
                            log(f"restored after a shell minimize (x{self._restores})")
                    else:
                        self.stay_on_desktop()
            except Exception:
                log("desktop watch error:\n" + traceback.format_exc())
            self.stop.wait(0.25)

    def stay_on_desktop(self) -> None:
        """Keep the card visible when the shell shows the desktop.

        Nothing can be inserted just above the shell's desktop layer — its icon
        host sits above the desktop window that owns it, so "above the desktop"
        is not a place you can ask for.  Instead the card borrows WS_EX_TOPMOST
        for exactly as long as the desktop is drawn over it, then goes back to
        what it should be: on top when "always on top" is on, and otherwise below
        the restored windows, which the shell arranges by itself.

        This runs whatever the on-top preference says.  The shell's desktop layer
        covers the card even when it is topmost, and skipping this whenever
        "always on top" was set meant nothing ever brought the card back.
        """
        if not self.hwnd:
            return
        if desktop_in_front():
            if not self._on_desktop_layer:
                set_topmost(self.hwnd, True)
                self._on_desktop_layer = True
                log("desktop shown — lifted the card back over it")
            return
        if self._on_desktop_layer:
            self._on_desktop_layer = False
            on_top = bool(self.config["on_top"])
            set_topmost(self.hwnd, on_top)
            if on_top:
                log("desktop hidden again — card stays on top")
            else:
                # Clearing the topmost flag leaves the card at the front of the ordinary
                # band, i.e. over the user's windows, so it has to be placed back.
                placed = send_to_desktop_layer(self.hwnd)
                log(
                    "desktop hidden again — card placed back on the desktop"
                    if placed
                    else "desktop hidden again — card back below the windows"
                )

    # ---------------------------------------------------------------- start

    def after_start(self) -> None:
        """Runs on a worker thread once the window is on screen."""
        self.shown = True
        self.hwnd = find_own_window(WINDOW_TITLE)
        if self.hwnd:
            log(f"window handle {self.hwnd:#x}")
            # Order matters: the taskbar flag re-computes the frame, so it has to
            # happen before the client size is set, and the rounded region is cut
            # from the window rectangle, so it goes last.
            if hide_from_taskbar(self.hwnd):
                log("window hidden from the taskbar (tool window)")
            fit_window(self.hwnd, *self.desired_size(), note="card")
            self.set_glass(bool(self.config.get("glass", True)))
            # pywebview sets Form.TopMost while the window still has a border,
            # and applying the frameless style afterwards throws it away — so
            # the widget was never actually on top.  Re-assert it here.
            set_topmost(self.hwnd, bool(self.config["on_top"]))
            log(f"always on top -> {bool(self.config['on_top'])}")
            if not bool(self.config["on_top"]) and bool(self.config.get("stay_on_desktop", True)):
                # Start life on the desktop rather than on top of whatever happens
                # to be open: that is where a desktop widget belongs.  A new window
                # starts at the front of the non-topmost band, so it has to be placed
                # explicitly, or it sits over the user's windows until the next
                # show-desktop cycle happens to move it.
                self.stay_on_desktop()
                send_to_desktop_layer(self.hwnd)
                log("card placed on the desktop layer")
            self.guard = MinimizeGuard(self.hwnd)
            log(
                "minimize guard installed (the card stays on the desktop)"
                if self.guard.install()
                else "minimize guard unavailable — show desktop will hide the card"
            )
        else:
            log("no window handle found — native glass unavailable")
        log(f"window ready (items={len(self.note.items)}, glass={self.glass_on})")
        self.start_tray()
        threading.Thread(target=self.monitor, name="monitor", daemon=True).start()
        threading.Thread(target=self.enforce_size, name="size-watch", daemon=True).start()
        threading.Thread(target=self.watch_desktop, name="desktop-watch", daemon=True).start()
        threading.Thread(target=self.watch_glass, name="glass-watch", daemon=True).start()
        self.push()

    def ui_test(self) -> None:
        """Exercise the widget's interactive surface and report to the log.

        Clicks all four switches through the real DOM (the exact path a user's
        click takes), then drives the move and resize gestures through the same
        loop the mouse drives but with synthetic input, so neither needs a real
        cursor.  Then it restores everything and quits.
        """
        ids = "['sw-on-top','sw-notify','sw-glass','sw-autostart','sw-draggable']"
        read = (
            f"{ids}.map(function (i) "
            "{ return document.getElementById(i).getAttribute('aria-checked'); }).join(',')"
        )
        click = f"{ids}.forEach(function (i) {{ document.getElementById(i).click(); }})"
        try:
            time.sleep(4.0)
            self.check_widget_window()

            before = str(self.window.evaluate_js(read))
            log(f"ui-test: switches before  = {before}")
            self.window.evaluate_js(click)
            time.sleep(3.0)
            after = str(self.window.evaluate_js(read))
            log(f"ui-test: switches after   = {after}")
            flipped = [a != b for a, b in zip(before.split(","), after.split(","))]
            log(
                "ui-test: PASS — all five switches toggled"
                if flipped and all(flipped) and len(flipped) == 5
                else f"ui-test: FAIL — only {sum(flipped)}/5 switches toggled"
            )
            self.window.evaluate_js(click)
            time.sleep(3.0)
            log(f"ui-test: switches restored = {self.window.evaluate_js(read)}")

            # The move/resize checks below need the gestures enabled, whatever the
            # user's Draggable setting is.
            was_draggable = bool(self.config.get("draggable", True))
            self.config["draggable"] = True
            self.check_gestures()
            self.config["draggable"] = was_draggable
            self.check_show_desktop()
            self.check_desktop_persistence()
        except Exception as error:
            log(f"ui-test: ERROR {error}")
        finally:
            time.sleep(0.4)
            self.quit()

    def wait_for_viewport(self, size: tuple[int, int], tries: int = 12, delay: float = 0.12) -> str:
        """Read the page's viewport back, letting the webview catch up.

        The window resize lands at once but the compositor can still report the
        old innerWidth for a frame or two, which made this check flaky (the window
        was provably 275x130 while the page still said 200x100).
        """
        want = f"{size[0]}x{size[1]}"
        viewport = ""
        for _ in range(tries):
            viewport = str(
                self.window.evaluate_js("window.innerWidth + 'x' + window.innerHeight")
            )
            if viewport == want:
                return viewport
            time.sleep(delay)
        return viewport

    def check_show_desktop(self) -> None:
        """Press Win+D for real and check the card is still the card.

        The three-finger gesture and Win+D are the same thing.  It does NOT
        minimize the card (the guard refuses that) — the shell raises the desktop
        layer over everything, which leaves a visible, un-minimized window hidden
        behind the wallpaper.  So the test has to ask what is actually under the
        card's own centre, not whether it is minimized.
        """

        def press_win_d() -> None:
            _user32.keybd_event(VK_LWIN, 0, 0, None)
            _user32.keybd_event(VK_D, 0, 0, None)
            _user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, None)
            _user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, None)

        def buried() -> bool:
            return desktop_covering(self.hwnd)

        try:
            press_win_d()
            time.sleep(1.2)
            # The watcher runs at 4 Hz; give it a moment to notice and pin back.
            time.sleep(1.0)
            still_visible = not minimized_or_hidden(self.hwnd)
            covered = buried()
            log(
                f"ui-test: {'PASS' if still_visible and not covered else 'FAIL'} — "
                f"survived Win+D (on screen: {still_visible}, "
                f"buried under the desktop: {covered})"
            )
        except Exception as error:
            log(f"ui-test: show-desktop check error: {error}")
        finally:
            press_win_d()  # put the user's desktop back the way it was
            time.sleep(1.0)

    def check_widget_window(self) -> None:
        """Taskbar/Alt+Tab exclusion and always-on-top, straight from the OS."""
        ex_style = int(_user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE))
        tool = bool(ex_style & WS_EX_TOOLWINDOW)
        app_window = bool(ex_style & WS_EX_APPWINDOW)
        topmost = bool(ex_style & 0x00000008)
        log(
            f"ui-test: ex-style 0x{ex_style:08X} tool-window={tool} "
            f"app-window={app_window} topmost={topmost}"
        )
        log(
            "ui-test: PASS — no taskbar button"
            if tool and not app_window
            else "ui-test: FAIL — the window would still show in the taskbar"
        )

    def check_desktop_persistence(self) -> None:
        """Make sure "show desktop" cannot take the card with it.

        Two paths: the SC_MINIMIZE message the shell sends (refused by the window
        procedure hook) and a direct minimize that bypasses messages (undone by
        the desktop watcher).
        """
        _user32.SendMessageW(self.hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
        time.sleep(0.4)
        by_message = minimized_or_hidden(self.hwnd)

        _user32.ShowWindow(self.hwnd, SW_MINIMIZE)
        time.sleep(0.9)
        by_direct = minimized_or_hidden(self.hwnd)

        log(
            f"ui-test: {'PASS' if not by_message and not by_direct else 'FAIL'} — "
            f"stayed on screen (shell message hid it: {by_message}, "
            f"direct minimize kept it hidden: {by_direct})"
        )

    def check_gestures(self) -> None:
        """Move and resize through the real gesture loop, with fake input."""
        start = _rect_to_geometry(self.hwnd) or {}
        # One fixed fake cursor for both legs, so the move cancels out exactly and
        # the result cannot depend on where the user's real mouse happens to be.
        area = primary_work_area() or (0, 0, 1600, 900)
        origin = (int((area[0] + area[2]) // 2), int((area[1] + area[3]) // 2))
        self._gesture("move", sample=synthetic_path(36, 22, origin=origin))
        self._gesture("move", sample=synthetic_path(-36, -22, origin=origin))
        moved = _rect_to_geometry(self.hwnd) or {}
        drift = abs(moved.get("x", 0) - start.get("x", 0)) + abs(moved.get("y", 0) - start.get("y", 0))
        ok_move = drift <= 1
        log(
            f"ui-test: {'PASS' if ok_move else 'FAIL'} — move gesture "
            f"{start.get('x')},{start.get('y')} -> {moved.get('x')},{moved.get('y')} and back"
            f" (drift {drift}px)"
        )

        self._gesture("size", sample=synthetic_path(150, 60, origin=origin))
        size = self.desired_size()
        viewport = self.wait_for_viewport(size)
        ok_size = viewport == f"{size[0]}x{size[1]}"
        log(
            f"ui-test: {'PASS' if ok_size else 'FAIL'} — resize gesture "
            f"viewport {viewport}, remembered size {size[0]}x{size[1]}, "
            f"content {self.window.evaluate_js('document.documentElement.scrollHeight')}px"
        )
        self._gesture("size", sample=synthetic_path(-150, -60, origin=origin))
        log(f"ui-test: resized back to {self.desired_size()}")

    def on_resized(self, *args) -> None:
        """WinForms can re-scale the window after it appears (it shrank the card
        the first time this ran), so verify the viewport again after any resize."""
        if self._size_check_running:
            return
        self._size_check_running = True

        def run() -> None:
            try:
                time.sleep(0.4)
                self.enforce_size(attempts=2)
            finally:
                self._size_check_running = False

        threading.Thread(target=run, name="size-recheck", daemon=True).start()

    def enforce_size(self, attempts: int = 6) -> None:
        """Keep the page's viewport at the size the card was designed for.

        The page reports its own innerWidth/innerHeight, which is the only
        measurement that actually matters: WinForms can re-scale the window
        after it appears, and silently shrank the card the first time this ran.
        """
        if not (self.hwnd and self.window) or self._gesture_active:
            return
        for attempt in range(1, attempts + 1):
            if self.stop.is_set():
                return
            time.sleep(0.5 if attempt == 1 else 0.9)
            if self._gesture_active:
                # A drag finished between the check and this pass: whatever size
                # the user just chose is the intended one.
                return
            # Read the intended size *each* pass.  Capturing it once meant that a
            # resize landing while this was sleeping was treated as a mistake and
            # reverted — the card snapped back to the size it had when the check
            # started.
            expected = self.desired_size()
            try:
                raw = self.window.evaluate_js(
                    "window.innerWidth + ' ' + window.innerHeight"
                )
            except Exception as error:
                log(f"could not read the viewport: {error}")
                return
            if raw is None:
                return  # the window is going away
            parts = str(raw).split()
            if len(parts) != 2:
                return
            current = (int(float(parts[0])), int(float(parts[1])))
            if current == expected:
                log(f"viewport confirmed {current[0]}x{current[1]} CSS (check {attempt})")
                return
            log(
                f"viewport {current[0]}x{current[1]} != {expected[0]}x{expected[1]} CSS — "
                f"re-applying the window size (config says {self.config['window']})"
            )
            fit_window(self.hwnd, expected[0], expected[1], f"correction {attempt}")
            self.set_glass(bool(self.config.get("glass", True)))
        log("viewport still off after corrections — the card adapts to whatever it gets")


class Api:
    """Exposed to the page as ``window.pywebview.api.*``.  Every setter returns
    a fresh payload so the UI never has to guess the result."""

    def __init__(self, widget: Widget) -> None:
        self._widget = widget
        self._connected = False

    def _touch(self) -> None:
        if not self._connected:
            self._connected = True
            log("page connected to the host")

    def get_state(self) -> dict:
        self._touch()
        return self._widget.payload()

    def resync(self) -> dict:
        self._touch()
        return self._widget.payload()

    def set_tz(self, tz_id: str) -> dict:  # kept so an old page cannot break the host
        return self._widget.payload()

    def toggle_item(self, item_id: int) -> dict:
        """Clicking a row. The page sends the id, the host decides what it means."""
        self._touch()
        return self._widget.toggle_item(item_id)

    def add_item(self, text: str) -> dict:
        self._touch()
        return self._widget.add_item(text)

    def edit_item(self, item_id: int, text: str) -> dict:
        self._touch()
        return self._widget.edit_item(item_id, text)

    def remove_item(self, item_id: int) -> dict:
        self._touch()
        return self._widget.remove_item(item_id)

    def set_pref(self, key: str, value: bool) -> dict:
        widget = self._widget
        if key == "autostart":
            wanted = bool(value)
            if set_autostart(wanted):
                widget.config["autostart"] = wanted
        elif key == "on_top":
            widget.set_on_top(bool(value))
        elif key == "glass":
            widget.set_glass(bool(value))
        elif key == "notify":
            widget.config["notify"] = bool(value)
        elif key == "draggable":
            widget.config["draggable"] = bool(value)
        widget.save()
        log(f"pref {key} -> {value}")
        widget.refresh_tray()
        return widget.payload()

    def hide_window(self) -> None:
        self._widget.hide()

    def begin_move(self) -> None:
        self._widget.begin_move()

    def begin_resize(self) -> None:
        self._widget.begin_resize()

    def boot_report(self, stage: str, detail: str = "") -> None:
        """Boot trace from the page, so a UI that never comes up is diagnosable."""
        log(f"page: {stage}" + (f" ({detail})" if detail else ""))
        if not self._widget._glass_ready:
            # The glass is applied as the window is created, but pywebview hides and
            # re-shows a transparent window while it starts, which throws the DWM
            # backdrop away — the card then sat there opaque and milky because every
            # call had reported success.
            self._widget._glass_ready = True
            threading.Thread(
                target=self._widget.settle_glass, name="glass-settle", daemon=True
            ).start()

    def quit_app(self) -> None:
        self._widget.quit()


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def dump_preview() -> int:
    """Write the browser preview payload (ui/state.preview.json)."""
    widget = Widget()
    state = widget.payload()
    state["prefs"] = {**state["prefs"], "on_top": False, "autostart": False, "glass": True}
    PREVIEW_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {PREVIEW_FILE}")
    print(f"  items : {state['total']} ({state['done']} done)")
    return 0


def self_check() -> int:
    """Environment, where the note lives, and what is on it."""
    print(f"python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"pywebview   : {webview.__version__ if hasattr(webview, '__version__') else 'installed'}")
    print(f"renderer    : {webview.renderer}")
    print(f"work area   : {primary_work_area()} (logical px)")
    print(f"autostart   : {autostart_installed()}  {startup_shortcut()}")
    widget = Widget()
    done, total = widget.note.counts()
    print(f"note file   : {widget.note.path}")
    print(f"items       : {total} ({done} done, {total - done} open)")
    for item in widget.note.items[:5]:
        mark = "x" if item["done"] else " "
        print(f"  [{mark}] {item['text'][:60]}")
    return 0


def run(debug: bool, ui_test: bool = False) -> int:
    install_excepthook()
    if not ui_test and already_running():
        # Two copies share the title, the preferences and the log, and then argue
        # over the window size.  One widget is enough.
        log("another copy is already running - leaving it alone and exiting")
        # pythonw can sit in interpreter shutdown after touching Win32; this path
        # has nothing to clean up, so leave without waiting for it.
        os._exit(0)
    widget = Widget()
    ensure_icon_file()

    # Reconcile the "start with Windows" intent with the actual shortcut.
    if bool(widget.config.get("autostart")) and not autostart_installed():
        set_autostart(True)
    if not CONFIG_FILE.exists():
        widget.save()
        log(f"wrote default preferences -> {CONFIG_FILE.name}")

    position = initial_position(widget.config, widget.desired_size())

    widget.window = webview.create_window(
        WINDOW_TITLE,
        url=str(UI_DIR / "index.html") + "?host=1",
        js_api=Api(widget),
        width=int(widget.desired_size()[0]),
        height=int(widget.desired_size()[1]),
        x=position[0] if position else None,
        y=position[1] if position else None,
        frameless=True,
        easy_drag=False,         # pywebview's drag never moved this window; see _gesture
        shadow=False,            # required for a transparent window
        transparent=True,        # let the desktop show through the glass
        on_top=bool(widget.config["on_top"]),
        resizable=False,
        text_select=True,        # the note has to allow typing and selecting text
        zoomable=False,
        background_color="#000000",
    )

    def on_closing() -> None:
        widget.remember_geometry()

    widget.window.events.closing += on_closing
    widget.window.events.resized += widget.on_resized

    if ui_test:
        threading.Thread(target=widget.ui_test, name="ui-test", daemon=True).start()

    log(
        f"starting widget (debug={debug}, pos={position}, "
        f"size={widget.desired_size()[0]}x{widget.desired_size()[1]}, "
        f"items={len(widget.note.items)}, glass={widget.config.get('glass', True)})"
    )
    icon = ICON_FILE if ICON_FILE.exists() else None
    webview.start(
        func=widget.after_start,
        gui="edgechromium",
        debug=debug,
        http_server=True,   # serve ui/ over http so the page behaves like a normal origin
        private_mode=True,
        icon=str(icon) if icon else None,
    )
    log("window closed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek balance Windows widget")
    parser.add_argument("--debug", action="store_true", help="open with devtools and verbose logging")
    parser.add_argument(
        "--state",
        action="store_true",
        help="read the wallet now, print the payload as JSON and exit",
    )
    parser.add_argument("--dump-preview", action="store_true", help="write ui/state.preview.json and exit")
    parser.add_argument("--self-check", action="store_true", help="print environment and one live balance line")
    parser.add_argument(
        "--ui-test",
        action="store_true",
        help="open the widget, click every switch through the DOM, report and exit",
    )
    parser.add_argument("--install-autostart", action="store_true", help="start the widget with Windows")
    parser.add_argument("--remove-autostart", action="store_true", help="stop starting it with Windows")
    args = parser.parse_args(argv)

    if args.install_autostart:
        print("autostart installed" if set_autostart(True) else "could not install autostart")
        return 0 if autostart_installed() else 1
    if args.remove_autostart:
        print("autostart removed" if set_autostart(False) else "could not remove autostart")
        return 0 if not autostart_installed() else 1
    if args.dump_preview:
        return dump_preview()
    if args.self_check:
        return self_check()
    if args.state:
        print(json.dumps(Widget().payload(), indent=2, ensure_ascii=False))
        return 0

    return run(args.debug, ui_test=args.ui_test)


if __name__ == "__main__":
    raise SystemExit(main())
