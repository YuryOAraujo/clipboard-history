from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clipboard.history import ClipboardItem, HistoryStore


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "history"
        self.history = HistoryStore(self.directory)

    def test_text_and_images_survive_restart_in_order(self):
        text = self.history.add_text("hello\n世界")
        image = self.history.add_image(b"png fixture")
        restarted = HistoryStore(self.directory)
        self.assertEqual(restarted.items, [image, text])
        self.assertEqual(restarted.image_path(image).read_bytes(), b"png fixture")

    def test_delete_and_clear_survive_restart_and_remove_image_files(self):
        text = self.history.add_text("keep")
        image = self.history.add_image(b"image")
        self.history.remove(image.id)
        self.assertFalse(self.history.image_path(image).exists())
        self.assertEqual(HistoryStore(self.directory).items, [text])
        self.history.add_image(b"another image")
        self.history.clear()
        self.assertEqual(HistoryStore(self.directory).items, [])
        self.assertEqual(list(self.history.image_directory.iterdir()), [])

    def test_recopy_moves_existing_text_and_image_to_top(self):
        first = self.history.add_text("first")
        second = self.history.add_text("second")
        image = self.history.add_image(b"image")
        self.history.add_text("first")
        self.assertEqual(self.history.items, [first, image, second])
        self.history.add_image(b"image")
        self.assertEqual(self.history.items, [image, first, second])
        self.assertEqual(len(list(self.history.image_directory.iterdir())), 1)

    def test_item_limit_evicts_image_file(self):
        history = HistoryStore(self.directory, max_items=2)
        image = history.add_image(b"old image")
        old = history.add_text("older")
        newest = history.add_text("newest")
        self.assertEqual(history.items, [newest, old])
        self.assertFalse(history.image_path(image).exists())

    def test_separate_byte_budgets_and_oversized_items(self):
        history = HistoryStore(self.directory, max_image_bytes=5, max_text_bytes=5)
        old_image = history.add_image(b"1234")
        old_text = history.add_text("éé")  # Four UTF-8 bytes.
        new_image = history.add_image(b"5678")
        new_text = history.add_text("abc")
        self.assertEqual(history.items, [new_text, new_image])
        self.assertNotIn(old_text, history.items)
        self.assertFalse(history.image_path(old_image).exists())
        self.assertIsNone(history.add_text("ééé"))
        self.assertIsNone(history.add_image(b"123456"))
        self.assertEqual(history.items, [new_text, new_image])

    def test_startup_cleans_orphans_but_keeps_referenced_images(self):
        image = self.history.add_image(b"keep")
        orphan = self.history.image_directory / "orphan.png"
        temporary = self.history.image_directory / ".clipboard-interrupted.tmp"
        orphan.write_bytes(b"old")
        temporary.write_bytes(b"incomplete")
        history = HistoryStore(self.directory)
        self.assertEqual(history.items, [image])
        self.assertEqual(list(history.image_directory.iterdir()), [history.image_path(image)])

    def test_startup_prunes_missing_images_and_applies_new_limits(self):
        missing = self.history.add_image(b"missing")
        self.history.image_path(missing).unlink()
        first = self.history.add_text("first")
        last = self.history.add_text("last")
        self.assertEqual(HistoryStore(self.directory, max_items=1).items, [last])
        self.assertNotEqual(first, last)

    def test_invalid_manifest_records_cannot_escape_image_folder(self):
        good = self.history.add_text("valid")
        outside = Path(self.temporary.name) / "outside.png"
        outside.write_bytes(b"untouched")
        records = [None, {"id": "../../outside", "kind": "image"},
                   {"id": "a" * 64, "kind": "text", "text": "wrong hash"},
                   {"id": "b" * 64, "kind": "text", "text": "\ud800"},
                   asdict(good), asdict(good)]
        self.history.manifest.write_text(json.dumps({"version": 1, "items": records}))
        self.assertEqual(HistoryStore(self.directory).items, [good])
        self.assertEqual(outside.read_bytes(), b"untouched")
        with self.assertRaises(ValueError):
            self.history.image_path(ClipboardItem("../../outside", "image"))

    def test_corrupt_manifest_preserves_files_for_recovery(self):
        image = self.history.add_image(b"keep")
        self.history.manifest.write_bytes(b"{truncated")
        with self.assertLogs("clipboard.history", level="WARNING"):
            restarted = HistoryStore(self.directory)
        self.assertEqual(restarted.items, [])
        self.assertEqual(restarted.manifest.read_bytes(), b"{truncated")
        self.assertTrue(restarted.image_path(image).exists())

    def test_failed_save_keeps_previous_manifest_and_model(self):
        old = self.history.add_text("saved")
        manifest = self.history.manifest.read_bytes()
        with patch("clipboard.history.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.history.add_text("unsaved")
        self.assertEqual(self.history.items, [old])
        self.assertEqual(self.history.manifest.read_bytes(), manifest)
        self.assertEqual(list(self.directory.glob(".clipboard-*.tmp")), [])

    def test_failed_delete_keeps_referenced_image(self):
        image = self.history.add_image(b"keep")
        with patch("clipboard.history.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.history.remove(image.id)
        self.assertEqual(self.history.items, [image])
        self.assertTrue(self.history.image_path(image).exists())

    def test_data_is_private_and_image_symlinks_are_not_followed(self):
        image = self.history.add_image(b"image")
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"untouched")
        self.history.image_path(image).unlink()
        self.history.image_path(image).symlink_to(outside)
        restarted = HistoryStore(self.directory)
        self.assertEqual(restarted.items, [])
        self.assertEqual(outside.read_bytes(), b"untouched")
        for path in (restarted.directory, restarted.image_directory):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(restarted.manifest.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
