"""Sandbox path confinement (`06-security.md` §6.1, `05` §5.8).

The device is the authority; these tests prove the bridge-side mirror agrees with
it for the cases the firmware suite will also assert, so a screen cannot build a
request the device will refuse.
"""

from __future__ import annotations

import pytest

from bridge.protocol import (
    SANDBOX_ROOT,
    SandboxPathError,
    sandbox_display,
    sandbox_join,
    sandbox_normalise,
    sandbox_parent,
)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("/workspace/", "/workspace"),
        ("/workspace", "/workspace"),
        ("/workspace/a/b.txt", "/workspace/a/b.txt"),
        ("/workspace/./a//b.txt", "/workspace/a/b.txt"),
        ("/workspace/x/../y", "/workspace/y"),
        ("/workspace/a/b/../../c", "/workspace/c"),
    ],
)
def test_a_sandbox_path_normalises_to_its_canonical_form(typed, expected):
    assert sandbox_normalise(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "/workspace/../config.json",
        "/workspace/a/../../config.json",
        "/workspace/..",
        "workspace/x.txt",
        "/fs/config.json",
        "/config.json",
        "//etc/passwd",
        "/workspace/a\x00b",
        # `..` before any segment has nothing to pop, so it escapes by climbing
        # above `/` rather than above `/workspace`. `/workspace/..` is refused one
        # branch later; these are the earlier one.
        "/../workspace/a",
        "/..",
    ],
)
def test_a_path_outside_the_sandbox_is_refused(typed):
    with pytest.raises(SandboxPathError):
        sandbox_normalise(typed)


def test_the_root_is_its_own_parent():
    """`U` at the root must stop, not climb into the user tree."""
    assert sandbox_parent("/workspace/") == SANDBOX_ROOT
    assert sandbox_parent("/workspace/runs") == "/workspace/"
    assert sandbox_parent("/workspace/runs/a.txt") == "/workspace/runs/"


def test_join_refuses_an_entry_name_that_is_a_path():
    assert sandbox_join("/workspace/runs", "a.txt") == "/workspace/runs/a.txt"
    for name in ("", ".", "..", "sub/a.txt", "/etc/passwd"):
        with pytest.raises(SandboxPathError):
            sandbox_join("/workspace/runs", name)


def test_display_marks_a_directory_with_a_trailing_slash():
    assert sandbox_display("/workspace/runs/") == "/workspace/runs/"
    assert sandbox_display("/workspace/runs/a.txt") == "/workspace/runs/a.txt"
    assert sandbox_display(SANDBOX_ROOT) == SANDBOX_ROOT
