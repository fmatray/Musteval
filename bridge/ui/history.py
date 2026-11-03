"""Composer prompt history: bounded, cursor-browsing, with a preserved draft.

Nothing here persists or prints; `05-bridge.md` §1 keeps the bridge a terminal
with no local conversation storage, so this ring dies with the process.
"""

from __future__ import annotations

MAX_ENTRIES = 100


class PromptHistory:
    """Recent composer lines the Up/Down keys walk, newest last."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: list[str] = []
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
