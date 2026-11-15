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

#: `04` §4.20: the most raw bytes one `sandbox.write` may carry, mirroring
#: `fs.put` §4.16. A larger file is staged in several frames.
SANDBOX_CHUNK_BYTES = 2048


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


#: `04` §4.16: the directories `fs.put` may write into, and the one extension each
#: carries. A name without its suffix would be listed as a template or a tool and
#: never parsed, so the directory fixes the extension.
IMPORT_DIRS = (("/skills/", ".md"), ("/prompts/", ".md"), ("/tools/", ".json"))

#: `04` §4.16: the two whole documents an import may replace.
IMPORT_FILES = ("/config.json", "/tasks.json")


def import_path_ok(path: str) -> bool:
    """Whether `fs.put` accepts `path`, mirroring `store_import_path_ok` exactly.

    The device refuses a path outside this list, and that refusal is the authority
    (`04` §4.16). The bridge checks it too so a doomed upload costs no round trip,
    and `05` §7 says so. The two must agree: a path the bridge admits and the
    device refuses is a command that fails after the file has been chosen.
    """
    if "\x00" in path or not path.startswith("/"):
        return False
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return False
            parts.pop()
            continue
        parts.append(segment)
    if not parts:
        return False
    canonical = "/" + "/".join(parts)
    if canonical in IMPORT_FILES:
        return True
    for prefix, suffix in IMPORT_DIRS:
        if not canonical.startswith(prefix):
            continue
        stem = canonical[len(prefix):]
        # A dotfile such as `/prompts/.md` has no stem and would list as a
        # template nobody can ask for, so the name must be longer than its suffix.
        return len(stem) > len(suffix) and stem.endswith(suffix)
    return False


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
