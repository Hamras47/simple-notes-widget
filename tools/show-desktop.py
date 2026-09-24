"""Press Win+D once — show the desktop, or bring the windows back.

    .venv\\Scripts\\python.exe tools\\show-desktop.py

The same shortcut as the three-finger swipe.  Handy for screenshots of the card
over the wallpaper with the real acrylic behind it: press it, capture, press it
again to put the user's windows back.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

app._user32.keybd_event(app.VK_LWIN, 0, 0, None)
app._user32.keybd_event(app.VK_D, 0, 0, None)
app._user32.keybd_event(app.VK_D, 0, app.KEYEVENTF_KEYUP, None)
app._user32.keybd_event(app.VK_LWIN, 0, app.KEYEVENTF_KEYUP, None)
time.sleep(0.2)
print("sent Win+D")
