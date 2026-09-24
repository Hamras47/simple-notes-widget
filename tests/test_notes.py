"""Tests for notes.py — the checklist, no widgets involved.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import notes


class NoteCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.folder.name) / "notes.json"
        self.note = notes.Note(self.path)

    def tearDown(self):
        self.folder.cleanup()


class Adding(NoteCase):
    def test_add_appends(self):
        item = self.note.add("call the accountant")
        self.assertEqual(item["text"], "call the accountant")
        self.assertFalse(item["done"])
        self.assertEqual(len(self.note.items), 1)

    def test_empty_text_is_refused(self):
        self.assertIsNone(self.note.add("   "))
        self.assertIsNone(self.note.add(None))
        self.assertEqual(self.note.items, [])

    def test_whitespace_and_newlines_collapse(self):
        item = self.note.add("  pay   the\nVAT  ")
        self.assertEqual(item["text"], "pay the VAT")

    def test_long_text_is_capped(self):
        item = self.note.add("x" * 900)
        self.assertEqual(len(item["text"]), notes.MAX_TEXT)

    def test_cap_is_enforced(self):
        for index in range(notes.MAX_ITEMS):
            self.assertIsNotNone(self.note.add(f"item {index}"))
        self.assertIsNone(self.note.add("one too many"))
        self.assertEqual(len(self.note.items), notes.MAX_ITEMS)

    def test_add_at_top(self):
        self.note.add("second")
        self.note.add("first", at_top=True)
        self.assertEqual([item["text"] for item in self.note.items], ["first", "second"])


class Toggling(NoteCase):
    def test_toggle_flips_done_and_back(self):
        item = self.note.add("chase the invoice")
        self.assertTrue(self.note.toggle(item["id"])["done"])
        self.assertFalse(self.note.toggle(item["id"])["done"])

    def test_unknown_id_is_ignored(self):
        self.assertIsNone(self.note.toggle(999))

    def test_counts(self):
        first = self.note.add("one")
        self.note.add("two")
        self.note.toggle(first["id"])
        self.assertEqual(self.note.counts(), (1, 2))


class Editing(NoteCase):
    def test_set_text(self):
        item = self.note.add("old")
        self.assertEqual(self.note.set_text(item["id"], "new")["text"], "new")

    def test_emptying_a_line_deletes_it(self):
        item = self.note.add("temporary")
        self.assertIsNone(self.note.set_text(item["id"], "   "))
        self.assertEqual(self.note.items, [])

    def test_remove(self):
        item = self.note.add("gone")
        self.assertTrue(self.note.remove(item["id"]))
        self.assertFalse(self.note.remove(item["id"]))


class Persistence(NoteCase):
    def test_round_trip(self):
        item = self.note.add("buy milk — 2 litres")
        self.note.toggle(item["id"])
        again = notes.Note(self.path)
        self.assertEqual([row["text"] for row in again.items], ["buy milk — 2 litres"])
        self.assertTrue(again.items[0]["done"])

    def test_ids_keep_going_up_after_a_reload(self):
        item = self.note.add("first")
        self.note.remove(item["id"])
        second = notes.Note(self.path).add("second")
        self.assertGreater(second["id"], item["id"])

    def test_broken_file_reads_as_empty(self):
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(notes.Note(self.path).items, [])

    def test_junk_rows_are_dropped(self):
        rows = [{"id": 1, "text": "keep"}, {"id": 2}, "nonsense", {"text": "   "}]
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.assertEqual([row["text"] for row in notes.Note(self.path).items], ["keep"])

    def test_wrapped_shape_is_accepted(self):
        self.path.write_text(json.dumps({"items": [{"id": 4, "text": "legacy"}]}), encoding="utf-8")
        self.assertEqual(notes.Note(self.path).items[0]["text"], "legacy")

    def test_a_bom_does_not_wipe_the_note(self):
        self.path.write_text(json.dumps([{"id": 1, "text": "survives"}]),
                             encoding="utf-8-sig")
        self.assertEqual(len(notes.Note(self.path).items), 1)

    def test_saving_into_a_missing_folder_works(self):
        deep = pathlib.Path(self.folder.name) / "a" / "b" / "notes.json"
        note = notes.Note(deep)
        note.add("hello")
        self.assertTrue(deep.exists())


if __name__ == "__main__":
    unittest.main()
