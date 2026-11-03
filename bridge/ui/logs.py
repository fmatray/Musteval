"""Logs screen state: level/tag filtering, the bounded buffer and level colouring.

Implements `05-bridge.md` §5.4 (tag and level filtering, the buffer follow mode
scrolls) and the Logs rows of §6.2.
"""

from __future__ import annotations

import re

from rich.text import Text

from .render import sanitize

LEVELS = ("debug", "info", "warn", "error")

#: The bracketed token a firmware line carries, e.g. `12:00:02 [warn] llm: timeout`.
_LEVEL = re.compile(r"\[([A-Za-z]+)\]")

#: A line that names no level is ordinary output, so it counts as info.
_DEFAULT_LEVEL = "info"

_STYLES = {"error": "red", "warn": "yellow", "info": "green", "debug": "dim"}


def _level_of(line: str) -> str:
    match = _LEVEL.search(line)
    return match.group(1).lower() if match else _DEFAULT_LEVEL


def _rank(level: str) -> int:
    """Unknown tokens (e.g. `[trace]`) rank as info: they pass info, not warn."""
    return LEVELS.index(level) if level in LEVELS else LEVELS.index(_DEFAULT_LEVEL)


def _normalise(min_level: str) -> str:
    level = min_level.strip().lower()
    if level not in LEVELS:
        raise ValueError(f"unknown log level: {min_level!r}")
    return level


class LogFilter:
    """Which buffered lines the Logs screen shows."""

    def __init__(self, tag: str = "", min_level: str = "debug") -> None:
        self.tag = tag
        self.min_level = _normalise(min_level)

    def matches(self, line: str) -> bool:
        if _rank(_level_of(line)) < LEVELS.index(self.min_level):
            return False
        return not self.tag or self.tag.lower() in line.lower()

    def with_tag(self, tag: str) -> LogFilter:
        return LogFilter(tag, self.min_level)

    def with_level(self, min_level: str) -> LogFilter:
        return LogFilter(self.tag, min_level)

    @property
    def active(self) -> bool:
        """True when the filter hides anything, i.e. it is not the default."""
        return bool(self.tag) or self.min_level != LEVELS[0]


class LogBuffer:
    """Every line received, bounded; the filter is applied when reading."""

    MAX_LINES = 2000

    def __init__(self, max_lines: int = MAX_LINES) -> None:
        self.max_lines = max_lines
        self._lines: list[str] = []
        self._filter = LogFilter()

    def add(self, line: str) -> bool:
        """Keep `line` even when hidden, and say whether the filter passes it."""
        self._lines.append(line)
        del self._lines[: max(0, len(self._lines) - self.max_lines)]
        return self._filter.matches(line)

    def clear(self) -> None:
        self._lines.clear()

    def set_filter(self, log_filter: LogFilter) -> None:
        self._filter = log_filter

    @property
    def log_filter(self) -> LogFilter:
        return self._filter

    def visible(self) -> list[str]:
        return [line for line in self._lines if self._filter.matches(line)]

    def __len__(self) -> int:
        return len(self._lines)


def render_log_line(line: str) -> Text:
    """`05-bridge.md` §5.4 colouring; an unknown level stays unstyled."""
    text = Text(sanitize(line))
    style = _STYLES.get(_level_of(line))
    if style:
        text.stylize(style)
    return text
