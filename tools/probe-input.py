"""Does the card still receive real mouse input?

    .venv\\Scripts\\python.exe tools\\probe-input.py [--drag]

--ui-test drives the gesture loop with synthetic input, so it cannot tell whether a
real mouse ever reaches the page.  This does: it raises the card (so the click cannot
land on a window covering it), puts the pointer on the card, clicks, and then reports
what the widget logged.  A click on the card makes the page call `begin_move`, so the
log shows `page: gesture (move)` if — and only if — the page really received the event.
`--drag` additionally drags the card 40px and the corner grip 40x20, with real mouse
events, and reports whether the window moved and whether it resized.

The window is layered with uniform alpha (never a colour key: keyed pixels are
click-through, which is exactly the bug this probe exists to catch).
"""

from __future__ import annotations

import ctypes
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "widget.log"

_user32 = app._user32
app._declare(_user32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], ctypes.c_bool)
app._declare(_user32, "mouse_event", [ctypes.c_uint] * 5, None)

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def log_lines() -> list[str]:
    if not LOG.exists():
        return []
    return LOG.read_text(encoding="utf-8", errors="replace").splitlines()


def log_tail(count: int = 6) -> list[str]:
    lines = log_lines()
    return [line.split("  ", 1)[-1] for line in lines[-count:]]


def log_since(moment: datetime) -> list[str]:
    """Every logged line after ``moment``.

    By timestamp, not by line count: the widget truncates its log when it passes
    256 KB, and a positional marker then points past the end and reports "nothing
    happened" while the card is working perfectly.
    """
    gained = []
    for line in log_lines():
        stamp, _, rest = line.partition("  ")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when >= moment:
            gained.append(rest)
    return gained


def gesture_result(gained: list[str], kind: str) -> tuple[str, str] | None:
    """The last `(position, size)` the widget reported for a finished gesture."""
    for line in reversed(gained):
        if f"{kind} gesture done ->" in line:
            rest = line.split("->", 1)[1].strip().split()
            return rest[0], rest[1]
    return None


def click() -> None:
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.12)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


handle = app.find_window(app.WINDOW_TITLE)
if not handle:
    print("the widget is not running - start it with run.cmd first")
    raise SystemExit(1)

rect = app.window_rect(handle)
if not rect:
    print("no window rect")
    raise SystemExit(1)
left, top, right, bottom = rect
centre_x, centre_y = (left + right) // 2, (top + bottom) // 2
print(f"card rect {left},{top} {right - left}x{bottom - top}  (clicking {centre_x},{centre_y})")

before = log_tail()
mark = datetime.now().replace(microsecond=0)
print(f"log before: {before[-1] if before else '(empty)'}")

# Raise the card first: if it is sitting behind another window, a click aimed at its
# rectangle lands on that window and proves nothing about the card.
app.set_topmost(handle, True)
time.sleep(0.4)

# Park the pointer on the card and click: the page answers with a gesture report.
_user32.SetCursorPos(centre_x, centre_y)
time.sleep(0.25)
click()
time.sleep(0.8)

new = log_since(mark)
print("log after :")
for line in new or ["(nothing new)"]:
    print(f"  {line}")

received = any("gesture" in line or "layout" in line for line in new)
print(f"\n=> the page {'RECEIVES' if received else 'DOES NOT receive'} mouse input")

if "--drag" in sys.argv and received:
    # Release anything the click leg may have left mid-gesture, or the host ignores
    # the next mousedown and the drag looks broken when it is not.
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.6)

    print("\nnow dragging the card 40px right with real mouse events ...")
    mark = datetime.now().replace(microsecond=0)
    start = app.window_rect(handle)
    _user32.SetCursorPos(centre_x, centre_y)
    time.sleep(0.3)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.2)
    for step in range(1, 9):
        _user32.SetCursorPos(centre_x + step * 5, centre_y)
        time.sleep(0.08)  # the host polls at ~120 Hz; do not outrun it
    time.sleep(0.15)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(1.2)
    moved = app.window_rect(handle)
    drag = gesture_result(log_since(mark), "move")
    # The rectangle is the honest witness: it is what the user sees, and it cannot
    # be confused by a neighbouring leg's log lines.
    print(f"  rect {start[0]},{start[1]} -> {moved[0]},{moved[1]}")
    print(
        f"  {'<= the card MOVED' if moved[:2] != start[:2] else '<= the card did not move'}"
        f"  (log: {drag[0] if drag else 'no move gesture reported'})"
    )

    print("\nnow dragging the corner grip with real mouse events ...")
    for inset in (8, 12, 16, 20):
        mark = datetime.now().replace(microsecond=0)
        before = app.window_rect(handle)
        grip_x, grip_y = before[2] - inset, before[3] - inset
        _user32.SetCursorPos(grip_x, grip_y)
        time.sleep(0.25)
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.2)
        for step in range(1, 7):
            _user32.SetCursorPos(grip_x + step * 5, grip_y + step * 3)
            time.sleep(0.08)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(1.2)
        gained = log_since(mark)
        after = app.window_rect(handle)
        which = "resize" if any("gesture (resize)" in line for line in gained) else "move"
        size = gesture_result(gained, "size")
        grew = (after[2] - after[0], after[3] - after[1]) != (
            before[2] - before[0],
            before[3] - before[1],
        )
        print(
            f"  inset {inset:>2}: page started a {which}; "
            f"size {before[2] - before[0]}x{before[3] - before[1]}"
            f" -> {after[2] - after[0]}x{after[3] - after[1]}"
            f"{'  <= the grip RESIZED the card' if grew else ''}"
            f"{(f'  (log: {size[1]})' if size else '')}"
        )
        if which == "resize":
            print(f"  grip centre is inset {inset}px from the corner")
            break

app.set_topmost(handle, False)
