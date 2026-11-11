"""Composer prompt history: bounded, cursor-browsing, with a preserved draft.

The ring is persisted as NDJSON at `history_path()` when a path is given; the
file holds only the lines the user typed, no conversation content, so recall
survives a restart without the bridge storing a transcript.
"""

from __future__ import annotations

import json
import os

MAX_ENTRIES = 100


def history_path() -> str:
    """`~/.config/musteval/history.ndjson`, honouring `XDG_CONFIG_HOME`.

    Deliberately duplicates `providers.custom_path()`; sharing it would mean a
    new module for two lines.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "musteval", "history.ndjson")


class PromptHistory:
    """Recent composer lines the Up/Down keys walk, newest last."""

    def __init__(
        self, max_entries: int = MAX_ENTRIES, path: str | None = None
    ) -> None:
        self.max_entries = max_entries
        #: Where the ring lives on disk; `None` keeps it in memory only.
        self.path = path
        self._entries: list[str] = self._load() if path is not None else []
        self._cursor: int | None = None
        #: `None` means no draft captured yet; `""` is a real, empty draft.
        self._draft: str | None = None

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, text: str) -> None:
        """Store `text` verbatim so recall returns exactly what was typed."""
        if not text.strip():
            return
        if self._entries and self._entries[-1] == text:
            self.reset()
            return
        self._entries.append(text)
        del self._entries[: max(0, len(self._entries) - self.max_entries)]
        self._save()
        self.reset()

    def previous(self, current: str) -> str:
        """Step back, saving `current` first so `next` can return to the unfinished line."""
        if not self._entries:
            return current
        if self._cursor is None:
            self._draft = current
            self._cursor = len(self._entries) - 1
        else:
            self._cursor = max(0, self._cursor - 1)
        return self._entries[self._cursor]

    def next(self, current: str) -> str:
        """Step forward; `len(_entries)` is the draft slot past the newest entry."""
        if self._cursor is None or not self._entries:
            return current
        if self._cursor >= len(self._entries) - 1:
            # Past the newest entry is the draft slot, and leaving it ends the walk.
            draft, self._cursor, self._draft = self._draft, None, None
            return current if draft is None else draft
        self._cursor += 1
        return self._entries[self._cursor]

    def reset(self) -> None:
        """Forget cursor and draft; entries stay so a fresh submission starts at the end."""
        self._cursor = None
        self._draft = None

    def _load(self) -> list[str]:
        """Read the newest `max_entries` lines, skipping anything unreadable."""
        try:
            with open(self.path, "rb") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        entries: list[str] = []
        for line in lines:
            try:
                decoded = line.decode("utf-8")
                entry = json.loads(decoded)
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(entry, str) and entry.strip():
                entries.append(entry)
        return entries[max(0, len(entries) - self.max_entries) :]

    def _save(self) -> None:
        """Rewrite the file whole; losing it must never break a submit."""
        if self.path is None:
            return
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                for entry in self._entries:
                    handle.write(json.dumps(entry) + "\n")
        except OSError:
            return
