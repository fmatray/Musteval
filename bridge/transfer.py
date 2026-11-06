"""Host-side file transfer for the Files screen (`05-bridge.md` §5.8.1).

The walk is here rather than in `ui` because it is the part that can be wrong
without a terminal: which entries are visited, in what order, and what is refused
before the first byte is written. `ui` supplies a `send` callback for the wire and
a `write` callback for the host, so the same code serves a download and a test.
"""

from __future__ import annotations

import os
from base64 import b64encode
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .protocol import SANDBOX_CHUNK_BYTES, SANDBOX_ROOT, sandbox_join

#: One `sandbox.read` reply carries at most this many raw bytes (`04` §4.18). A
#: file above it comes back `truncated`, and a truncated copy is refused rather
#: than written: presenting a prefix as a file is worse than not copying it.
READ_CAP = 8192

#: What `ui` hands the walker: send one command, return its `result` or `None`.
Send = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class TransferError(Exception):
    """A transfer stopped before writing anything the reader would mistake for
    complete. The message names the entry that caused it."""


@dataclass(frozen=True)
class FileNode:
    """One file in a downloaded tree, with the text fetched for it."""

    path: str
    text: str


async def collect(send: Send, path: str) -> list[FileNode]:
    """Every file under `path`, depth-first in the order the device listed.

    Nothing is written while collecting: the caller either has a whole tree or an
    exception. A directory read that fails aborts the walk instead of skipping the
    subtree, because a download missing a directory is indistinguishable from one
    where it was empty.
    """
    listing = await send("sandbox.list", {"path": path.rstrip("/") + "/"})
    if listing is None:
        raise TransferError(f"{path}: the device did not list it")

    files: list[FileNode] = []
    for entry in listing.get("entries") or []:
        name = str(entry.get("name", ""))
        child = sandbox_join(path, name)
        if entry.get("dir"):
            files.extend(await collect(send, child))
            continue
        body = await send("sandbox.read", {"path": child})
        if body is None:
            raise TransferError(f"{child}: the device did not read it")
        if body.get("truncated"):
            raise TransferError(
                f"{child}: {body.get('bytes')} bytes is above the {READ_CAP}-byte "
                "read cap, so a copy would be a prefix"
            )
        files.append(FileNode(child, str(body.get("text", ""))))
    return files


def host_path_for(root: str, sandbox_path: str) -> str:
    """Map `/workspace/a/b.txt` under `root` to a host path, refusing escapes.

    The sandbox path is already canonical, so the only way out of `root` would be a
    symlink inside it; `os.path.realpath` is not used because the tree does not
    exist yet. `..` cannot occur, and the prefix check makes that a guarantee rather
    than a hope.
    """
    relative = sandbox_path[len(SANDBOX_ROOT.rstrip("/")) :].lstrip("/")
    target = os.path.normpath(os.path.join(root, relative))
    if target != root and not target.startswith(root.rstrip("/") + os.sep):
        raise TransferError(f"{sandbox_path}: escapes {root}")
    return target


def write_file(target: str, text: str) -> str:
    """Write one file at an exact host path, creating its parent directory."""
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return target


def write_tree(root: str, nodes: list[FileNode]) -> list[str]:
    """Write collected files under `root`, returning the paths written.

    Parents are created as they are needed; an existing file is overwritten, because
    the reader asked to copy a tree onto a chosen directory and stopping at the
    first collision would leave half of it.
    """
    written: list[str] = []
    for node in nodes:
        written.append(write_file(host_path_for(root, node.path), node.text))
    return written


async def upload(send: Send, host_path: str, target_dir: str) -> list[str]:
    """Copy one host file or tree into `target_dir` on the device.

    Directories are created with `sandbox.mkdir` and files streamed through
    `sandbox.write`, so a large file is many frames and never one over the cap.
    Anything the device cannot represent — a symlink, a socket, a name that is not
    a legal sandbox entry — aborts before that entry is sent, naming it.
    """
    host_path = os.path.abspath(host_path)
    if not os.path.exists(host_path):
        raise TransferError(f"{host_path}: no such file or directory")

    if os.path.isfile(host_path):
        remote = sandbox_join(target_dir, os.path.basename(host_path))
        await _put_file(send, host_path, remote)
        return [remote]

    written: list[str] = []
    for current, dirs, names in os.walk(host_path):
        dirs.sort()
        # `os.walk` files a symlink-to-directory under `dirs` and never descends it,
        # so checking only `names` would drop that subtree without a word.
        for name in dirs:
            link = os.path.join(current, name)
            if os.path.islink(link):
                raise TransferError(f"{link}: a symlink is not a sandbox entry")
        relative = os.path.relpath(current, host_path)
        remote_dir = (
            target_dir
            if relative == "."
            else sandbox_join(target_dir, relative.replace(os.sep, "/"))
        )
        if relative != ".":
            created = await send("sandbox.mkdir", {"path": remote_dir})
            if created is None:
                raise TransferError(f"{remote_dir}: the device did not create it")
        for name in sorted(names):
            source = os.path.join(current, name)
            if os.path.islink(source):
                raise TransferError(f"{source}: a symlink is not a sandbox entry")
            if not os.path.isfile(source):
                raise TransferError(f"{source}: not a regular file")
            remote = sandbox_join(remote_dir, name)
            await _put_file(send, source, remote)
            written.append(remote)
    return written


async def _put_file(send: Send, source: str, remote: str) -> None:
    with open(source, "rb") as handle:
        raw = handle.read()
    chunks = [
        raw[i : i + SANDBOX_CHUNK_BYTES]
        for i in range(0, len(raw), SANDBOX_CHUNK_BYTES)
    ] or [b""]
    for index, chunk in enumerate(chunks):
        result = await send(
            "sandbox.write",
            {
                "path": remote,
                "data": b64encode(chunk).decode("ascii"),
                "encoding": "base64",
                "offset": index * SANDBOX_CHUNK_BYTES,
                "final": index == len(chunks) - 1,
            },
        )
        if result is None:
            raise TransferError(f"{remote}: the device did not accept chunk {index}")
