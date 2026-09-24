"""Where is the widget in the Z-order?

    .venv\\Scripts\\python.exe tools\\zorder.py

Prints the top-level windows from the front, marking the card, then says whether the
card is topmost and whether any ordinary window is in front of it.  "In front of it" is
exactly the bug this exists to catch: after show desktop the card used to keep the slot
it was lifted to, so it sat over the user's windows.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

GW_HWNDNEXT = 2


def main() -> int:
    handle = app.find_window(app.WINDOW_TITLE)
    if not handle:
        print("the widget is not running")
        return 1

    order = []
    in_front_of_card = []
    seen_card = False
    current = int(app._user32.GetTopWindow(None) or 0)
    while current:
        name = app.window_class(current)
        if current == handle:
            seen_card = True
            order.append(f"[CARD {name}]")
        else:
            if not seen_card and app.is_app_window(current):
                in_front_of_card.append(name)
            order.append(name)
        current = int(app._user32.GetWindow(ctypes.c_void_p(current), GW_HWNDNEXT) or 0)

    print("front to back: " + " | ".join(order[:10]) + " ...")
    print(f"card topmost          : {bool(app._user32.GetWindowLongW(handle, -20) & 0x8)}")
    print(f"desktop in front      : {app.desktop_in_front()}")
    print(f"desktop covering card : {app.desktop_covering(handle)}")
    if in_front_of_card:
        print(f"windows in front of the card: {', '.join(in_front_of_card)}")
        print("=> the card is BEHIND those windows (what we want when not always on top)")
    else:
        print("=> nothing ordinary is in front of the card (it is at the front)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
