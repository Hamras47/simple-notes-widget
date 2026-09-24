"""The note itself: a checklist, kept in notes.json.

A sticky note has no network, no clock and no schedule, so this is deliberately the
smallest of the three widget engines: load, mutate, save.  The host owns the file; the
page owns nothing but the drawing.

An item is a plain dict ``{"id": int, "text": str, "done": bool}``.  Ids only ever go
up, so a page that has been showing the list for a while can never toggle the wrong row
after something above it was deleted.

Pure stdlib, and every method is total: bad input is refused quietly rather than raising
into a widget whose only console is a log file.
"""

from __future__ import annotations

import json
import pathlib

MAX_ITEMS = 200
MAX_TEXT = 400
DEFAULT_TEXT = "New item"


def clean(text: object) -> str:
    """One line, trimmed, length-capped.  Newlines would break the row layout."""
    return " ".join(str(text or "").split())[:MAX_TEXT]


class Note:
    """The checklist, backed by a JSON file that is replaced atomically on save."""

    def __init__(self, path: pathlib.Path | str) -> None:
        self.path = pathlib.Path(path)
        self.items, self.next_id = self._load()

    # ------------------------------------------------------------- loading

    def _load(self) -> tuple[list[dict], int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return [], 1
        stored_next = None
        if isinstance(raw, dict):  # the file also carries the id high-water mark
            stored_next = raw.get("next_id")
            raw = raw.get("items")
        if not isinstance(raw, list):
            return [], 1
        items: list[dict] = []
        next_id = 1
        for row in raw:
            if not isinstance(row, dict):
                continue
            text = clean(row.get("text"))
            if not text:
                continue
            try:
                item_id = int(row.get("id", next_id))
            except (TypeError, ValueError):
                item_id = next_id
            next_id = max(next_id, item_id + 1)
            items.append({"id": item_id, "text": text, "done": bool(row.get("done"))})
        # The counter is kept so ids never repeat, even after the note is emptied: a page
        # left open on an old list would otherwise be able to toggle a brand new row.
        try:
            next_id = max(next_id, int(stored_next))
        except (TypeError, ValueError):
            pass
        return items[:MAX_ITEMS], next_id

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            body = {"next_id": self.next_id, "items": self.items}
            temporary.write_text(json.dumps(body, indent=1), encoding="utf-8")
            temporary.replace(self.path)
            return True
        except OSError:
            return False  # a note that cannot be saved still shows what is on screen

    # ------------------------------------------------------------ mutating

    def _take_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def _find(self, item_id: int) -> dict | None:
        for item in self.items:
            if item["id"] == item_id:
                return item
        return None

    def add(self, text: object = "", *, at_top: bool = False) -> dict | None:
        text = clean(text)
        if not text:
            return None
        if len(self.items) >= MAX_ITEMS:
            return None
        item = {"id": self._take_id(), "text": text, "done": False}
        if at_top:
            self.items.insert(0, item)
        else:
            self.items.append(item)
        self.save()
        return item

    def toggle(self, item_id: int) -> dict | None:
        item = self._find(item_id)
        if item is None:
            return None
        item["done"] = not item["done"]
        self.save()
        return item

    def set_text(self, item_id: int, text: object) -> dict | None:
        item = self._find(item_id)
        if item is None:
            return None
        cleaned = clean(text)
        if not cleaned:
            # An item emptied by editing is a delete, which is what a user expects when
            # they clear a line and press Enter.
            self.items.remove(item)
            self.save()
            return None
        item["text"] = cleaned
        self.save()
        return item

    def remove(self, item_id: int) -> bool:
        item = self._find(item_id)
        if item is None:
            return False
        self.items.remove(item)
        self.save()
        return True

    # -------------------------------------------------------------- reading

    def counts(self) -> tuple[int, int]:
        done = sum(1 for item in self.items if item["done"])
        return done, len(self.items)

    def as_payload(self) -> list[dict]:
        return [dict(item) for item in self.items]
