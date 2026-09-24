"""Probe: what does the backdrop sampler make of the desktop right now?

    .venv\\Scripts\\python.exe tools\\probe-vibrancy.py

Run it while the widget is open.  It prints the luminance of the ring around the
window and the glass density that maps to, plus the raw samples, so the
thresholds in app.py can be checked against a real screen instead of guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

handle = app.find_window(app.WINDOW_TITLE)
if not handle:
    print("the widget is not running — start it with run.cmd first")
    raise SystemExit(1)

rect = app.window_rect(handle)
print(f"window rect      {rect}")
sample = app.backdrop_luminance(handle)
print(f"backdrop         {sample}")
if sample:
    luminance = sample[0]
    glass = "dense" if luminance >= 0.55 else "clear" if luminance <= 0.40 else "hold (dead zone)"
    print(f"luminance        {luminance:.3f} over {sample[1]} samples")
    print(f"-> glass         {glass}")

hdc = app._user32.GetDC(None)
try:
    left, top, right, bottom = rect
    print("\nring samples (r,g,b  luminance):")
    for label, x, y in (
        ("above", (left + right) // 2, top - 26),
        ("below", (left + right) // 2, bottom + 26),
        ("left ", left - 26, (top + bottom) // 2),
        ("right", right + 26, (top + bottom) // 2),
    ):
        colour = int(app._gdi32.GetPixel(hdc, x, y))
        if colour == 0xFFFFFFFF:
            print(f"  {label}  off-screen")
            continue
        red, green, blue = colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF
        print(
            f"  {label}  {red:>3},{green:>3},{blue:>3}  "
            f"{app.relative_luminance(red, green, blue):.3f}  at {x},{y}"
        )
finally:
    app._user32.ReleaseDC(None, hdc)
