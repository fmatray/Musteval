"""Sandbox path arithmetic (`05` §5.8, `06-security.md` §6.1).

The device refuses anything outside `/workspace/`, and that refusal is the
authority. This module mirrors the rule so neither `commands.py` nor a screen can
even build such a request, and so a test can prove the mirror before the firmware
exists.

It lives at L0 because both L2 (`commands`, which validates a `/sandbox` path
before sending) and L3 (`ui`, which builds a listing path while browsing) need it,
and L2 may not import L3.

The one place the two could disagree is normalisation, so it is done once here,
exactly as `06-security.md` §6.1 describes: segments are split, `.` is dropped,
`..` pops, a repeated separator is one separator, and the result must carry the
literal `/workspace/` prefix with at least one further segment.
"""

from __future__ import annotations

ROOT = "/workspace/"


class SandboxPathError(ValueError):
    """A path that does not resolve strictly inside the agent sandbox."""


def normalise(path: str) -> str:
    """Return the canonical absolute form, or raise `SandboxPathError`.

    A relative path is refused rather than anchored, because a silently anchored
    path is a path the user did not type.
    """
    if not path.startswith("/"):
        raise SandboxPathError(f"not absolute: {path}")
    if "\x00" in path:
        raise SandboxPathError(f"NUL in path: {path!r}")
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise SandboxPathError(f"escapes the sandbox: {path}")
            parts.pop()
            continue
        parts.append(segment)
    if not parts or parts[0] != "workspace":
        raise SandboxPathError(f"outside {ROOT}: {path}")
    return "/" + "/".join(parts)


def is_dir(path: str) -> bool:
    """`True` when the canonical path names a directory (a trailing `/` or the root)."""
    return path == ROOT.rstrip("/") or path.endswith("/")


def join(directory: str, name: str) -> str:
    """Append one entry name to a directory path, refusing a name that is a path."""
    if "/" in name or name in ("", ".", ".."):
        raise SandboxPathError(f"not a single entry name: {name!r}")
    return normalise(directory.rstrip("/") + "/" + name)


def parent(path: str) -> str:
    """One directory up, stopping at the sandbox root instead of escaping it."""
    canonical = normalise(path)
    if canonical == ROOT.rstrip("/"):
        return ROOT
    head, _, _ = canonical.rpartition("/")
    return head + "/" if head.count("/") else ROOT


def display(path: str) -> str:
    """The path as the screen shows it: canonical, with directories carrying `/`."""
    canonical = normalise(path)
    if canonical == ROOT.rstrip("/"):
        return ROOT
    return canonical + "/" if is_dir(path) else canonical
