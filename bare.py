"""The "no background" face: the widget drawn straight onto the wallpaper.

The glass card is a WebView2 page, and WebView2 always paints its own background, so a card
can be see-through only as a whole (uniform window alpha), never per pixel.  This module is
the other face of the widget: a native per-pixel alpha layered window, the same mechanism
as the clock and battery widgets.  Only the text and marks are painted; every other pixel is
the desktop, and clicks there go straight through to it.

It runs on its own thread with its own message loop, so it never touches pywebview's.  The
host gives it a render function (payload -> Drawing) and a few callbacks.

Kept identical across deepseek-balance, deepseek-offpeak and sticky-notes.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ------------------------------------------------------------------ drawing ---

SS = 3  # supersampling for clean edges; Pillow does not antialias shapes
HIT_ALPHA = 1  # invisible, but the mouse can feel it: drag and right-click work
#: Text on the wallpaper is drawn at this opacity, so it blends in rather than glaring.
BLEND = 0.8
#: The status dot breathes like the card's: opacity 1 -> 0.5 -> 1 every 2.6 s.
PULSE_PERIOD = 2.6
PULSE_MS = 80

FONTS = {
    "text": (("segoeui.ttf", None),),
    "semibold": (("seguisb.ttf", None), ("segoeui.ttf", None)),
    "display": (("bahnschrift.ttf", "SemiBold"), ("seguisb.ttf", None)),
    "light": (("bahnschrift.ttf", "Light"), ("segoeuil.ttf", None)),
}

INKS = {
    #: text colour, halo colour
    "light": ((255, 255, 255), (0, 0, 0)),
    "dark": ((18, 22, 28), (255, 255, 255)),
}


@lru_cache(maxsize=64)
def font(kind: str, size_px: int) -> ImageFont.FreeTypeFont:
    for name, variation in FONTS.get(kind, FONTS["text"]):
        path = Path(r"C:\Windows\Fonts") / name
        if not path.exists():
            continue
        try:
            face = ImageFont.truetype(str(path), max(1, size_px))
            if variation:
                try:
                    face.set_variation_by_name(variation)
                except (OSError, ValueError):
                    pass
            return face
        except OSError:
            continue
    return ImageFont.load_default(max(1, size_px))


@dataclass
class Drawing:
    """A finished face: straight RGBA, the draggable box and the clickable buttons."""

    image: Image.Image
    hit: tuple[int, int, int, int]
    buttons: list[tuple[tuple[int, int, int, int], object]] = field(default_factory=list)
    #: The pulsing parts (the status dot), laid over the image at a breathing opacity.
    pulse: Image.Image | None = None


class Sketch:
    """Draw in CSS pixels; finish() gives the physical-pixel face with a readability halo."""

    def __init__(self, width_css: float, height_css: float, scale: float, ink: str) -> None:
        self.scale = scale
        self.k = scale * SS
        self.ink, self.halo = INKS.get(ink, INKS["light"])
        self.size = (max(1, int(round(width_css * scale))), max(1, int(round(height_css * scale))))
        self.image = Image.new("RGBA", (self.size[0] * SS, self.size[1] * SS), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)
        self.buttons: list[tuple[tuple[float, float, float, float], object]] = []
        self.pulse_image: Image.Image | None = None

    def _xy(self, x: float, y: float) -> tuple[float, float]:
        return x * self.k, y * self.k

    def colour(self, rgb: tuple[int, int, int] | None, alpha: int = 255) -> tuple[int, int, int, int]:
        if rgb is None:
            return (*self.ink, alpha)
        if self.ink == INKS["dark"][0]:
            # Pale accents wash out on a light wallpaper: deepen them with the ink.
            rgb = tuple(int(v * 0.55) for v in rgb)  # type: ignore[assignment]
        return (*rgb, alpha)

    def text(self, x: float, y: float, text: str, size: float, kind: str = "text",
             rgb: tuple[int, int, int] | None = None, alpha: int = 255, anchor: str = "ls",
             spacing: float = 0.0) -> float:
        """Draw text with its baseline at y (anchor 'ls'); returns the width in CSS pixels."""
        face = font(kind, int(round(size * self.k)))
        fill = self.colour(rgb, alpha)
        if not spacing:
            self.draw.text(self._xy(x, y), text, font=face, fill=fill, anchor=anchor)
            return self.draw.textlength(text, font=face) / self.k
        cursor = x
        for char in text:
            self.draw.text(self._xy(cursor, y), char, font=face, fill=fill, anchor=anchor)
            cursor += self.draw.textlength(char, font=face) / self.k + spacing
        return cursor - x - spacing

    def measure(self, text: str, size: float, kind: str = "text") -> float:
        return self.draw.textlength(text, font=font(kind, int(round(size * self.k)))) / self.k

    def fit(self, text: str, size: float, kind: str, width: float) -> str:
        """Shorten with an ellipsis until it fits."""
        if self.measure(text, size, kind) <= width:
            return text
        while text and self.measure(text + "\u2026", size, kind) > width:
            text = text[:-1]
        return text.rstrip() + "\u2026"

    def rounded(self, box: tuple[float, float, float, float], radius: float,
                fill: tuple[int, int, int, int] | None = None,
                outline: tuple[int, int, int, int] | None = None, width: float = 1.0) -> None:
        self.draw.rounded_rectangle([v * self.k for v in box], radius=radius * self.k, fill=fill,
                                    outline=outline, width=max(1, int(round(width * self.k))))

    def dot(self, cx: float, cy: float, r: float, rgba: tuple[int, int, int, int]) -> None:
        self.draw.ellipse(((cx - r) * self.k, (cy - r) * self.k, (cx + r) * self.k, (cy + r) * self.k),
                          fill=rgba)

    def pulse_dot(self, cx: float, cy: float, r: float, rgb: tuple[int, int, int]) -> None:
        """The status dot with its faint ring, on its own layer so it can breathe."""
        if self.pulse_image is None:
            self.pulse_image = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(self.pulse_image)
        ring = r + 2.5
        draw.ellipse(((cx - ring) * self.k, (cy - ring) * self.k, (cx + ring) * self.k, (cy + ring) * self.k),
                     fill=(*rgb, 56))
        draw.ellipse(((cx - r) * self.k, (cy - r) * self.k, (cx + r) * self.k, (cy + r) * self.k),
                     fill=(*rgb, 255))

    def line(self, points: list[tuple[float, float]], rgba: tuple[int, int, int, int], width: float) -> None:
        self.draw.line([self._xy(*p) for p in points], fill=rgba, width=max(1, int(round(width * self.k))),
                       joint="curve")

    def button(self, box: tuple[float, float, float, float], ident: object) -> None:
        self.buttons.append((box, ident))

    def finish(self, pad: float = 6.0) -> Drawing:
        image = self.image.resize(self.size, Image.Resampling.LANCZOS)
        alpha = image.getchannel("A")
        halo_alpha = alpha.filter(ImageFilter.GaussianBlur(max(1.0, 2.2 * self.scale)))
        halo = Image.new("RGBA", self.size, (*self.halo, 0))
        halo.putalpha(halo_alpha.point(lambda v: min(255, v * 150 // 255)))
        image = Image.alpha_composite(halo, image)
        image.putalpha(image.getchannel("A").point(lambda v: int(v * BLEND)))
        pulse = None
        if self.pulse_image is not None:
            pulse = self.pulse_image.resize(self.size, Image.Resampling.LANCZOS)
            pulse.putalpha(pulse.getchannel("A").point(lambda v: int(v * BLEND)))
            alpha = ImageChops.lighter(alpha, pulse.getchannel("A"))
        box = alpha.getbbox() or (0, 0, 1, 1)
        p = int(round(pad * self.scale))
        hit = (max(0, box[0] - p), max(0, box[1] - p),
               min(self.size[0], box[2] + p), min(self.size[1], box[3] + p))
        buttons = [
            (tuple(int(round(v * self.scale)) for v in b), ident)  # type: ignore[misc]
            for b, ident in self.buttons
        ]
        return Drawing(image=image, hit=hit, buttons=buttons, pulse=pulse)  # type: ignore[arg-type]


# -------------------------------------------------------------------- Win32 ---

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # own loader: own argtypes
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", ctypes.c_void_p),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("cx", ctypes.c_int),
                ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_H, _I, _U, _P = wintypes.HWND, ctypes.c_int, wintypes.UINT, ctypes.c_void_p
for _dll, _name, _args, _res in (
    (_user32, "RegisterClassW", [_P], wintypes.WORD),
    (_user32, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  _I, _I, _I, _I, _H, wintypes.HMENU, wintypes.HINSTANCE, _P], _H),
    (_user32, "DestroyWindow", [_H], wintypes.BOOL),
    (_user32, "DefWindowProcW", [_H, _U, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
    (_user32, "GetMessageW", [_P, _H, _U, _U], _I),
    (_user32, "TranslateMessage", [_P], wintypes.BOOL),
    (_user32, "DispatchMessageW", [_P], LRESULT),
    (_user32, "PostMessageW", [_H, _U, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
    (_user32, "PostQuitMessage", [_I], None),
    (_user32, "ShowWindow", [_H, _I], wintypes.BOOL),
    (_user32, "IsIconic", [_H], wintypes.BOOL),
    (_user32, "SetWindowPos", [_H, _H, _I, _I, _I, _I, _U], wintypes.BOOL),
    (_user32, "GetWindowRect", [_H, _P], wintypes.BOOL),
    (_user32, "GetDpiForWindow", [_H], _U),
    (_user32, "SetTimer", [_H, ctypes.c_size_t, _U, _P], ctypes.c_size_t),
    (_user32, "KillTimer", [_H, ctypes.c_size_t], wintypes.BOOL),
    (_user32, "GetDC", [_H], wintypes.HDC),
    (_user32, "ReleaseDC", [_H, wintypes.HDC], _I),
    (_user32, "UpdateLayeredWindow", [_H, wintypes.HDC, _P, _P, wintypes.HDC, _P, wintypes.DWORD, _P,
                                      wintypes.DWORD], wintypes.BOOL),
    (_user32, "LoadCursorW", [wintypes.HINSTANCE, _P], wintypes.HANDLE),
    (_user32, "FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], _H),
    (_user32, "GetWindow", [_H, _U], _H),
    (_user32, "SetWindowLongPtrW", [_H, _I, _P], _P),
    (_user32, "GetCursorPos", [_P], wintypes.BOOL),
    (_user32, "SetForegroundWindow", [_H], wintypes.BOOL),
    (_user32, "CreatePopupMenu", [], wintypes.HMENU),
    (_user32, "AppendMenuW", [wintypes.HMENU, _U, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
    (_user32, "TrackPopupMenu", [wintypes.HMENU, _U, _I, _I, _I, _H, _P], _I),
    (_user32, "DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
    (_gdi32, "GetPixel", [wintypes.HDC, _I, _I], wintypes.DWORD),
    (_gdi32, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
    (_gdi32, "CreateDIBSection", [wintypes.HDC, _P, _U, _P, wintypes.HANDLE, wintypes.DWORD], wintypes.HBITMAP),
    (_gdi32, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
    (_gdi32, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
    (_gdi32, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
    (_kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HINSTANCE),
):
    _f = getattr(_dll, _name)
    _f.argtypes, _f.restype = _args, _res

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80000, 0x80, 0x8000000, 0x8
CS_DBLCLKS = 0x8
SW_HIDE, SW_SHOWNA, SW_RESTORE = 0, 8, 9
HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HTCAPTION, HTCLIENT, HTTRANSPARENT = 2, 1, -1
WM_CLOSE, WM_DESTROY, WM_SIZE, WM_WINDOWPOSCHANGING, WM_NCHITTEST = 0x10, 0x2, 0x5, 0x46, 0x84
WM_NCLBUTTONDBLCLK, WM_NCRBUTTONUP = 0xA3, 0xA5
WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x201, 0x203, 0x205
WM_SYSCOMMAND, WM_TIMER, WM_EXITSIZEMOVE, WM_NULL = 0x112, 0x113, 0x232, 0x0
WM_APP_SHOW, WM_APP_HIDE, WM_APP_REDRAW, WM_APP_CLOSE = 0x8101, 0x8102, 0x8103, 0x8104
SC_MINIMIZE, SC_MASK = 0xF020, 0xFFF0
GW_OWNER, GWLP_HWNDPARENT = 4, -8
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0, 0x8, 0x800
INK_DARK_ABOVE, INK_LIGHT_BELOW = 0.55, 0.42


def _luminance(colour: int) -> float:
    def channel(value: int) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (0.2126 * channel(colour & 0xFF) + 0.7152 * channel((colour >> 8) & 0xFF)
            + 0.0722 * channel((colour >> 16) & 0xFF))


MenuEntry = "tuple[int, str, bool] | None"  # None is a separator


class BareFace:
    """A borderless, backgroundless window for one widget.  Thread-safe public methods."""

    def __init__(
        self,
        title: str,
        render: Callable[[str, float], Drawing],
        *,
        interval: float = 1.0,
        on_click: Callable[[object], None] | None = None,
        on_double: Callable[[], None] | None = None,
        menu: Callable[[], list] | None = None,
        on_choice: Callable[[int], None] | None = None,
        on_moved: Callable[[int, int], None] | None = None,
        draggable: Callable[[], bool] = lambda: True,
        on_top: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = print,
    ) -> None:
        self.title = title
        self.class_name = "BareFace_" + "".join(c for c in title if c.isalnum())
        self.render = render
        self.interval = interval
        self.on_click, self.on_double = on_click, on_double
        self.menu, self.on_choice, self.on_moved = menu, on_choice, on_moved
        self.draggable, self.on_top, self.log = draggable, on_top, log
        self.hwnd = 0
        self.shown = False
        self.ink = "light"
        self.drawing: Drawing | None = None
        self._ready = threading.Event()
        self._wndproc = WNDPROC(self._proc)
        self._pending_xy: tuple[int, int] | None = None
        self._last_ink = 0.0

    # ----------------------------------------------------------- public API

    def start(self) -> bool:
        threading.Thread(target=self._run, name="bare-face", daemon=True).start()
        return self._ready.wait(5.0) and bool(self.hwnd)

    def show(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            self._pending_xy = (int(x), int(y))
        self.shown = True
        self._post(WM_APP_SHOW)

    def hide(self) -> None:
        self.shown = False
        self._post(WM_APP_HIDE)

    def refresh(self) -> None:
        self._post(WM_APP_REDRAW)

    def close(self) -> None:
        self._post(WM_APP_CLOSE)

    def position(self) -> tuple[int, int]:
        rect = wintypes.RECT()
        _user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return rect.left, rect.top

    # ------------------------------------------------------------ internals

    def _post(self, message: int) -> None:
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, message, 0, 0)

    @property
    def scale(self) -> float:
        return (_user32.GetDpiForWindow(self.hwnd) or 96) / 96.0 if self.hwnd else 1.0

    def _run(self) -> None:
        instance = _kernel32.GetModuleHandleW(None)
        wc = _WNDCLASS()
        wc.style = CS_DBLCLKS
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wc.hInstance = instance
        wc.hCursor = _user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wc.lpszClassName = self.class_name
        _user32.RegisterClassW(ctypes.byref(wc))
        progman = _user32.FindWindowW("Progman", None)
        style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = _user32.CreateWindowExW(style, self.class_name, self.title, WS_POPUP,
                                            0, 0, 1, 1, progman, None, instance, None) or 0
        self._ready.set()
        if not self.hwnd:
            self.log(f"no-background face: could not create the window ({ctypes.get_last_error()})")
            return
        _user32.SetTimer(self.hwnd, 1, int(self.interval * 1000), None)
        _user32.SetTimer(self.hwnd, 2, PULSE_MS, None)
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _paint(self) -> None:
        if not self.hwnd or not self.shown:
            return
        try:
            drawing = self.render(self.ink, self.scale)
        except Exception as error:  # a bad frame must never kill the face
            self.log(f"no-background face: render failed: {error}")
            return
        self.drawing = drawing
        self._present()

    def _present(self) -> None:
        drawing = self.drawing
        if drawing is None or not self.hwnd or not self.shown:
            return
        image = drawing.image
        if drawing.pulse is not None:
            phase = (time.monotonic() % PULSE_PERIOD) / PULSE_PERIOD
            factor = 0.75 + 0.25 * math.cos(2 * math.pi * phase)
            dot = drawing.pulse.copy()
            dot.putalpha(dot.getchannel("A").point(lambda v: int(v * factor)))
            image = Image.alpha_composite(image, dot)
        else:
            image = image.copy()
        block = Image.new("L", image.size, 0)
        h = drawing.hit
        ImageDraw.Draw(block).rectangle((h[0], h[1], h[2] - 1, h[3] - 1), fill=HIT_ALPHA)
        image.putalpha(ImageChops.lighter(image.getchannel("A"), block))
        red, green, blue, alpha = image.convert("RGBa").split()
        pixels = Image.merge("RGBa", (blue, green, red, alpha)).tobytes()
        width, height = image.size

        left, top = self.position()
        screen = _user32.GetDC(None)
        memory = _gdi32.CreateCompatibleDC(screen)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth, header.biHeight = width, -height
        header.biPlanes, header.biBitCount = 1, 32
        header.biSizeImage = width * height * 4
        info = (ctypes.c_byte * (ctypes.sizeof(_BITMAPINFOHEADER) + 12))()
        ctypes.memmove(info, ctypes.byref(header), ctypes.sizeof(header))
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(memory, info, 0, ctypes.byref(bits), None, 0)
        if bitmap and bits:
            previous = _gdi32.SelectObject(memory, bitmap)
            ctypes.memmove(bits, pixels, len(pixels))
            size = wintypes.SIZE(width, height)
            src = wintypes.POINT(0, 0)
            dst = wintypes.POINT(left, top)
            blend = _BLEND(0, 0, 255, 1)
            _user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(dst), ctypes.byref(size), memory,
                                        ctypes.byref(src), 0, ctypes.byref(blend), 2)
            _gdi32.SelectObject(memory, previous)
            _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory)
        _user32.ReleaseDC(None, screen)

    def _sample_ink(self) -> None:
        rect = wintypes.RECT()
        _user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 1 or height <= 1:
            return
        m = max(2, int(4 * self.scale))
        points = [(rect.left + int(width * f), rect.top - m) for f in (0.2, 0.5, 0.8)]
        points += [(rect.left + int(width * f), rect.bottom + m) for f in (0.2, 0.5, 0.8)]
        points += [(rect.left - m, rect.top + height // 2), (rect.right + m, rect.top + height // 2)]
        screen = _user32.GetDC(None)
        samples = [_luminance(c) for c in (_gdi32.GetPixel(screen, x, y) for x, y in points)
                   if c not in (0xFFFFFFFF, 0)]
        _user32.ReleaseDC(None, screen)
        if not samples:
            return
        level = sum(samples) / len(samples)
        ink = "dark" if level >= INK_DARK_ABOVE else "light" if level <= INK_LIGHT_BELOW else self.ink
        if ink != self.ink:
            self.ink = ink
            self._paint()

    def _pin(self) -> None:
        progman = _user32.FindWindowW("Progman", None)
        if progman and _user32.GetWindow(self.hwnd, GW_OWNER) != progman:
            _user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, progman)
        if _user32.IsIconic(self.hwnd):
            _user32.ShowWindow(self.hwnd, SW_RESTORE)

    def _layer(self) -> None:
        on_top = self.on_top()
        _user32.SetWindowPos(self.hwnd, HWND_TOPMOST if on_top else HWND_NOTOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if not on_top:
            _user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _button_at(self, x: int, y: int) -> object | None:
        drawing = self.drawing
        if drawing is None:
            return None
        for (left, top, right, bottom), ident in drawing.buttons:
            if left <= x < right and top <= y < bottom:
                return ident
        return None

    def _open_menu(self) -> None:
        if not self.menu or not self.on_choice:
            return
        handle = _user32.CreatePopupMenu()
        for entry in self.menu():
            if entry is None:
                _user32.AppendMenuW(handle, MF_SEPARATOR, 0, None)
            else:
                ident, label, checked = entry
                _user32.AppendMenuW(handle, MF_STRING | (MF_CHECKED if checked else 0), ident, label)
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        _user32.SetForegroundWindow(self.hwnd)
        choice = _user32.TrackPopupMenu(handle, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                        point.x, point.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        _user32.DestroyMenu(handle)
        if choice:
            self.on_choice(choice)

    def _proc(self, hwnd, message, wparam, lparam):  # noqa: C901 - a window procedure
        try:
            if message == WM_TIMER and wparam == 2:
                if self.shown and self.drawing is not None and self.drawing.pulse is not None:
                    self._present()
                return 0
            if message == WM_TIMER:
                if self.shown:
                    self._pin()
                    now = time.monotonic()
                    if now - self._last_ink > 2.0:
                        self._last_ink = now
                        self._sample_ink()
                    self._paint()
                return 0
            if message == WM_APP_REDRAW:
                self._paint()
                return 0
            if message == WM_APP_SHOW:
                if self._pending_xy is not None:
                    x, y = self._pending_xy
                    self._pending_xy = None
                    _user32.SetWindowPos(self.hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                _user32.ShowWindow(self.hwnd, SW_SHOWNA)
                self._pin()
                self._layer()
                self._sample_ink()
                self._paint()
                return 0
            if message == WM_APP_HIDE:
                _user32.ShowWindow(self.hwnd, SW_HIDE)
                return 0
            if message == WM_APP_CLOSE:
                _user32.DestroyWindow(self.hwnd)
                return 0
            if message == WM_WINDOWPOSCHANGING and lparam and not self.on_top():
                # A desktop face never rises over the user's windows.
                position = _WINDOWPOS.from_address(lparam)
                if not position.flags & SWP_NOZORDER:
                    position.hwndInsertAfter = HWND_BOTTOM
                return 0
            if message == WM_SYSCOMMAND and (wparam & SC_MASK) == SC_MINIMIZE:
                return 0
            if message == WM_NCHITTEST:
                drawing = self.drawing
                if drawing is None:
                    return HTTRANSPARENT
                left, top = self.position()
                x = ctypes.c_short(lparam & 0xFFFF).value - left
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value - top
                if self._button_at(x, y) is not None:
                    return HTCLIENT
                h = drawing.hit
                if h[0] <= x < h[2] and h[1] <= y < h[3]:
                    return HTCAPTION if self.draggable() else HTCLIENT
                return HTTRANSPARENT
            if message == WM_LBUTTONDOWN:
                ident = self._button_at(ctypes.c_short(lparam & 0xFFFF).value,
                                        ctypes.c_short((lparam >> 16) & 0xFFFF).value)
                if ident is not None and self.on_click:
                    self.on_click(ident)
                    self._paint()
                return 0
            if message in (WM_LBUTTONDBLCLK, WM_NCLBUTTONDBLCLK):
                if self.on_double:
                    self.on_double()
                return 0
            if message in (WM_RBUTTONUP, WM_NCRBUTTONUP):
                self._open_menu()
                return 0
            if message == WM_EXITSIZEMOVE:
                if self.on_moved:
                    self.on_moved(*self.position())
                self._sample_ink()
                return 0
            if message == WM_DESTROY:
                _user32.KillTimer(self.hwnd, 1)
                _user32.KillTimer(self.hwnd, 2)
                _user32.PostQuitMessage(0)
                return 0
        except Exception as error:
            self.log(f"no-background face: {error}")
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)
