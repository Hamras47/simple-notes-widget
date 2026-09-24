"""Is the card transparent, or is it an opaque slab?

    .venv\\Scripts\\python.exe tools\\check-glass.py tmp\\shot.png

Works on a capture of the card (tools\\capture-topmost.ps1 or capture-window.ps1,
which leave a margin of desktop around it).  The trick is that a translucent card
tells you about itself: it should be *about as dark as the desktop around it*,
usually a little darker because of the shade on top.  An opaque card is much
lighter than its surroundings — that is exactly the "milky grey" failure, and
comparing the card with its own frame catches it without needing a reference shot
or a controlled backdrop.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from PIL import Image

#: capture-window.ps1 grabs this many physical pixels of desktop around the card.
MARGIN = 28
#: How much lighter than the surrounding desktop the card may be before it is
#: judged opaque (0-255, per channel).
TOLERANCE = 40

if len(sys.argv) != 2:
    print(__doc__)
    raise SystemExit(2)

image = Image.open(Path(sys.argv[1])).convert("RGB")
width, height = image.size
pixels = image.load()


def median_box(box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    x0, y0, x1, y1 = box
    values = [pixels[x, y] for y in range(y0, y1, 3) for x in range(x0, x1, 3)]
    return tuple(int(statistics.median([v[i] for v in values])) for i in range(3))


def brightness(colour: tuple[int, int, int]) -> float:
    return sum(colour) / 3


# Inside the card, along its left padding, so no glyphs are sampled.
card = median_box((MARGIN + 2, MARGIN + 10, MARGIN + 12, height - MARGIN - 10))
# The desktop just outside it, on the same row band.
around = median_box((2, MARGIN + 10, MARGIN - 4, height - MARGIN - 10))

card_luma, around_luma = brightness(card), brightness(around)
gap = card_luma - around_luma

print(f"card        rgb{card}   brightness {card_luma:.0f}")
print(f"around      rgb{around}   brightness {around_luma:.0f}")
print(f"difference  {gap:+.0f}  (opaque if the card is more than {TOLERANCE} lighter)")

verdict = gap <= TOLERANCE
print(f"\n=> {'transparent glass' if verdict else 'OPAQUE — the blur has been dropped'}")
raise SystemExit(0 if verdict else 1)
