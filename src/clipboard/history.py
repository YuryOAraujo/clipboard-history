"""Bounded clipboard history, independent of GTK and its window lifecycle."""

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Literal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClipboardItem:
    id: str
    kind: Literal["text", "image"]
    text: str | None = None


def content_id(kind: str, data: bytes) -> str:
    return hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


class HistoryStore:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        max_items: int = 100,
        max_image_bytes: int = 64 * 1024 * 1024,
        max_text_bytes: int = 4 * 1024 * 1024,
    ):
        if min(max_items, max_image_bytes, max_text_bytes) <= 0:
            raise ValueError("History limits must be positive")
        if directory is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
            if not data_home.is_absolute():
                data_home = Path.home() / ".local" / "share"
            directory = data_home / "clipboard-history"
        self.directory = Path(directory)
        self.image_directory = self.directory / "clipboard-images"
        self.manifest = self.directory / "history.json"
        self.max_items = max_items
        self.max_image_bytes = max_image_bytes
        self.max_text_bytes = max_text_bytes
        for folder in (self.directory, self.image_directory):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
        self.items: list[ClipboardItem] = []
        self._load()

    def image_path(self, item: ClipboardItem) -> Path:
        if item.kind != "image" or not re.fullmatch(r"[0-9a-f]{64}", item.id):
            raise ValueError("Invalid image item")
        return self.image_directory / f"{item.id}.png"

    def add_text(self, text: str) -> ClipboardItem | None:
        data = text.encode("utf-8")
        if not data or len(data) > self.max_text_bytes:
            return None
        return self._add(ClipboardItem(content_id("text", data), "text", text))

    def add_image(self, png: bytes) -> ClipboardItem | None:
        if not png or len(png) > self.max_image_bytes:
            return None
        item = ClipboardItem(content_id("image", png), "image")
        path = self.image_path(item)
        # Replace symlinks rather than writing through them.
        if path.is_symlink() or not path.is_file():
            self._atomic_write(path, png)
        return self._add(item)

    def remove(self, item_id: str) -> None:
        self._commit([item for item in self.items if item.id != item_id])

    def clear(self) -> None:
        self._commit([])

    def _add(self, item: ClipboardItem) -> ClipboardItem:
        if not self.items or self.items[0] != item:
            self._commit([item] + [old for old in self.items if old.id != item.id])
        return item

    def _load(self) -> None:
        try:
            document = json.loads(self.manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._cleanup_images()
            return
        except (ValueError, UnicodeError):
            # Preserve the damaged manifest and images for possible recovery.
            logger.warning("Cannot decode clipboard history: %s", self.manifest)
            return
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("items"), list):
            logger.warning("Unsupported clipboard history: %s", self.manifest)
            return

        items = []
        seen = set()
        for record in document["items"]:
            if not isinstance(record, dict):
                continue
            item_id = record.get("id")
            kind = record.get("kind")
            text = record.get("text")
            if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-f]{64}", item_id) or item_id in seen:
                continue
            if kind == "text":
                if not isinstance(text, str) or not text:
                    continue
                try:
                    if content_id("text", text.encode("utf-8")) != item_id:
                        continue
                except UnicodeError:
                    continue
                item = ClipboardItem(item_id, "text", text)
            elif kind == "image":
                item = ClipboardItem(item_id, "image")
                path = self.image_path(item)
                if path.is_symlink() or not path.is_file():
                    continue
            else:
                continue
            seen.add(item_id)
            items.append(item)
        # Prune missing files and enforce limits before loading any previews.
        self._commit(items)

    def _bounded(self, items: list[ClipboardItem]) -> list[ClipboardItem]:
        kept = []
        image_bytes = text_bytes = 0
        for item in items:
            if len(kept) >= self.max_items:
                break
            if item.kind == "image":
                try:
                    size = self.image_path(item).stat().st_size
                except FileNotFoundError:
                    continue
                if image_bytes + size > self.max_image_bytes:
                    continue
                image_bytes += size
            else:
                size = len((item.text or "").encode("utf-8"))
                if text_bytes + size > self.max_text_bytes:
                    continue
                text_bytes += size
            kept.append(item)
        return kept

    def _commit(self, items: list[ClipboardItem]) -> None:
        items = self._bounded(items)
        document = {"version": 1, "items": [asdict(item) for item in items]}
        self._atomic_write(self.manifest, json.dumps(document, ensure_ascii=False).encode("utf-8"))
        self.items = items
        self._cleanup_images()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".clipboard-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _cleanup_images(self) -> None:
        referenced = {self.image_path(item) for item in self.items if item.kind == "image"}
        for path in self.image_directory.iterdir():
            if path not in referenced and (path.is_file() or path.is_symlink()):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Cannot remove unused clipboard image: %s", path, exc_info=True)
