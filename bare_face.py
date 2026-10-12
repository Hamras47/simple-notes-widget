"""The note, drawn with no background (see bare.py): progress line and the checklist.

Sizes and positions are the card's own (styles.css and the note's inline styles), so
switching between the card and this face changes the background and nothing else.  Each
checkbox is a button, so items can be ticked straight from the wallpaper; editing happens
on the glass card (double-click the note).
"""

from __future__ import annotations

from bare import Drawing, Sketch

WIDTH = 290.0
PAD = 16.0
LIST_TOP = 38.0
ROW = 24.0
MAX_ROWS = 12
GREEN = (94, 231, 155)


def render(payload: dict, ink: str, scale: float) -> Drawing:
    items = list(payload.get("items") or [])
    shown = items[:MAX_ROWS]
    extra = len(items) - len(shown)
    rows = max(1, len(shown)) + (1 if extra > 0 else 0)
    s = Sketch(WIDTH, LIST_TOP + rows * ROW + 8, scale, ink)
    s.pulse_dot(PAD + 4, 24, 4, GREEN)
    s.text(PAD + 16, 28.2, str(payload.get("summary", "")).upper(), 12, "display", rgb=GREEN, spacing=1.8)
    if not shown:
        s.text(PAD + 2, LIST_TOP + 16, "Nothing here yet \u00b7 double-click to add", 11.5, alpha=170)
    text_x = PAD + 2 + 16 + 7
    for index, item in enumerate(shown):
        top = LIST_TOP + index * ROW
        box = (PAD + 3, top + 5, PAD + 17, top + 19)
        done = bool(item.get("done"))
        if done:
            s.rounded(box, 4, fill=(*GREEN, 255))
            s.line([(box[0] + 3.2, top + 12.2), (box[0] + 5.9, top + 15), (box[0] + 10.8, top + 8.8)],
                   (8, 17, 12, 255), 1.6)
        else:
            s.rounded(box, 4, outline=s.colour(None, 115), width=1.0)
        s.button((PAD - 2, top, PAD + 22, top + ROW), item.get("id"))
        text = s.fit(str(item.get("text", "")), 12.5, "text", WIDTH - text_x - PAD)
        width = s.text(text_x, top + 16.5, text, 12.5, alpha=165 if done else 255)
        if done:
            s.line([(text_x, top + 12.3), (text_x + width, top + 12.3)], s.colour(None, 140), 1.0)
    if extra > 0:
        s.text(text_x, LIST_TOP + len(shown) * ROW + 16.5, f"+{extra} more", 11.5, alpha=170)
    return s.finish()
