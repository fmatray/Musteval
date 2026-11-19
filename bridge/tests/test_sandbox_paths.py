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
    userfs_import_ok,
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


@pytest.mark.parametrize(
    "path",
    [
        "rel/skills/a/SKILL.md",
        "/skills/a\x00b.md",
        # `..` with no segment to pop climbs above `/` rather than above a
        # directory, so there is no canonical form to test against the allowlist.
        "/../skills/a/SKILL.md",
    ],
)
def test_an_import_path_that_cannot_canonicalise_is_refused(path):
    """`04` §4.16: an import path is canonicalised the way the device does it, so a
    relative path, an embedded NUL or a climb above the root has no form the
    allowlist could match. The device refuses these; the bridge must agree."""
    assert userfs_import_ok(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/config.json",
        "/tasks.json",
        "/skills/led-control/SKILL.md",
        "/prompts/brief.md",
        "/prompts/sub/brief.md",  # a nested template is still a template
        "/tools/gpio.json",
    ],
)
def test_an_import_path_the_device_accepts_is_accepted(path):
    """`04` §4.16: the bridge refuses before sending, so a path it refuses is dead
    for a file the device would have taken."""
    assert userfs_import_ok(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/notes.md",  # the sandbox is not userfs
        "/config.json.bak",
        "/skills/led-control.md",  # a loose `.md` under `/skills/` is not a skill
        "/skills/led-control/skill.md",  # the leaf is fixed and case-sensitive
        "/skills/led-control/notes.md",
        "/skills/led-control/SKILL.md.bak",
        "/skills/a/b/SKILL.md",  # one level: the directory is the skill's identity
        "/skills/SKILL.md",  # no name segment
        "/skills/",  # no leaf at all
        "/tools/gpio.md",
        "/prompts/brief.json",
    ],
)
def test_an_import_path_the_device_refuses_is_refused(path):
    """agentskills.io: a skill is `<name>/SKILL.md` under `/skills/`, so anything
    else there imports and never loads."""
    assert userfs_import_ok(path) is False
