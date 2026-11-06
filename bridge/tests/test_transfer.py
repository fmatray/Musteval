"""`05-bridge.md` §5.8.1: upload and download walks.

The walker takes a `send` callback instead of a `Session`, so what is asserted here
is the traversal itself — order, refusals before the first write, and the frame
shapes each entry produces.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from bridge import transfer
from bridge.protocol import SANDBOX_CHUNK_BYTES, Frame, encode


class Device:
    """A scripted sandbox that records every frame and answers from a tree."""

    def __init__(self, tree: dict[str, object]) -> None:
        self.tree = tree
        self.sent: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    async def send(self, name, args):
        self.sent.append((name, dict(args)))
        if name in self.fail:
            return None
        if name == "sandbox.list":
            directory = str(args["path"])
            entries = self.tree.get(directory)
            if entries is None:
                return None
            return {
                "path": directory,
                "entries": [
                    {"name": n, "dir": isinstance(v, dict), "bytes": 0}
                    for n, v in entries.items()
                ],
            }
        if name == "sandbox.read":
            path = str(args["path"])
            parent, _, leaf = path.rpartition("/")
            directory = self.tree.get(parent + "/")
            body = directory.get(leaf) if isinstance(directory, dict) else None
            if not isinstance(body, str):
                return None
            return {
                "path": args["path"],
                "text": body,
                "bytes": len(body.encode()),
                "truncated": False,
            }
        if name == "sandbox.mkdir":
            return {"path": args["path"], "created": True}
        if name == "sandbox.write":
            return {"path": args["path"], "bytes": 0, "created": args["offset"] == 0}
        raise AssertionError(f"unexpected command {name}")


TREE = {
    "/workspace/": {"notes.md": "hello", "runs": {}},
    "/workspace/runs/": {"a.json": "{}", "deep": {}},
    "/workspace/runs/deep/": {"b.json": "[]"},
}


async def test_collect_walks_depth_first_in_device_order():
    nodes = await transfer.collect(Device(TREE).send, "/workspace")
    assert [n.path for n in nodes] == [
        "/workspace/notes.md",
        "/workspace/runs/a.json",
        "/workspace/runs/deep/b.json",
    ]


async def test_collect_keeps_the_text_of_each_file():
    nodes = await transfer.collect(Device(TREE).send, "/workspace")
    assert {n.path: n.text for n in nodes}["/workspace/runs/deep/b.json"] == "[]"


async def test_a_truncated_read_aborts_the_whole_walk():
    """A prefix presented as a file is worse than no file."""

    class Truncating(Device):
        async def send(self, name, args):
            body = await super().send(name, args)
            if name == "sandbox.read" and body is not None:
                body = dict(body, truncated=True, bytes=9000)
            return body

    with pytest.raises(transfer.TransferError, match="read cap"):
        await transfer.collect(Truncating(TREE).send, "/workspace")


async def test_a_failed_listing_aborts_rather_than_skipping_the_subtree():
    """Silently skipping a directory is indistinguishable from it being empty."""
    device = Device(TREE)
    device.fail.add("sandbox.list")
    with pytest.raises(transfer.TransferError, match="did not list"):
        await transfer.collect(device.send, "/workspace")


async def test_a_failed_read_aborts():
    device = Device(TREE)
    device.fail.add("sandbox.read")
    with pytest.raises(transfer.TransferError, match="did not read"):
        await transfer.collect(device.send, "/workspace")


def test_host_path_maps_below_the_chosen_root():
    assert transfer.host_path_for("/tmp/x", "/workspace/runs/a.json") == (
        "/tmp/x/runs/a.json"
    )


def test_host_path_refuses_a_constructed_escape():
    """The device cannot send `..`, but the mapping must not trust that."""
    with pytest.raises(transfer.TransferError, match="escapes"):
        transfer.host_path_for("/tmp/x", "/workspace/../../etc/passwd")


def test_write_tree_creates_parents_and_returns_what_it_wrote(tmp_path):
    nodes = [transfer.FileNode("/workspace/runs/deep/b.json", "[]")]
    written = transfer.write_tree(str(tmp_path), nodes)
    assert written == [str(tmp_path / "runs" / "deep" / "b.json")]
    assert (tmp_path / "runs" / "deep" / "b.json").read_text() == "[]"


def test_write_tree_preserves_the_exact_bytes(tmp_path):
    """`newline=""` keeps a CRLF file from being rewritten as LF on the host."""
    transfer.write_tree(
        str(tmp_path), [transfer.FileNode("/workspace/a.txt", "one\r\ntwo\n")]
    )
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\n"


async def test_upload_of_one_file_streams_it_in_one_final_frame(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("hello")
    device = Device({})
    written = await transfer.upload(device.send, str(source), "/workspace")
    assert written == ["/workspace/a.txt"]
    name, args = device.sent[-1]
    assert name == "sandbox.write"
    assert args["final"] is True and args["offset"] == 0
    assert base64.b64decode(args["data"]) == b"hello"


async def test_upload_splits_a_large_file_into_offset_frames(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (SANDBOX_CHUNK_BYTES * 2 + 1))
    device = Device({})
    await transfer.upload(device.send, str(source), "/workspace")
    writes = [a for n, a in device.sent if n == "sandbox.write"]
    assert [w["offset"] for w in writes] == [0, SANDBOX_CHUNK_BYTES, 2 * SANDBOX_CHUNK_BYTES]
    assert [w["final"] for w in writes] == [False, False, True]
    # A frame that overran the cap would be refused on the wire, so assert the cap.
    assert max(len(base64.b64decode(w["data"])) for w in writes) <= SANDBOX_CHUNK_BYTES


async def test_upload_of_a_tree_mkdirs_before_writing(tmp_path):
    root = tmp_path / "tree"
    (root / "deep").mkdir(parents=True)
    (root / "one.txt").write_text("1")
    (root / "deep" / "two.txt").write_text("2")
    device = Device({})
    written = await transfer.upload(device.send, str(root), "/workspace/up")
    assert written == ["/workspace/up/one.txt", "/workspace/up/deep/two.txt"]
    mkdirs = [a["path"] for n, a in device.sent if n == "sandbox.mkdir"]
    assert mkdirs == ["/workspace/up/deep"]


async def test_upload_refuses_a_symlink_instead_of_skipping_it(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "real.txt").write_text("x")
    (root / "link.txt").symlink_to(root / "real.txt")
    with pytest.raises(transfer.TransferError, match="symlink"):
        await transfer.upload(Device({}).send, str(root), "/workspace/up")


async def test_upload_refuses_a_symlinked_directory(tmp_path):
    """`os.walk` lists a symlink-to-directory under `dirs` and never descends, so a
    check over `names` alone would skip the whole subtree without a word."""
    root = tmp_path / "tree"
    (root / "real").mkdir(parents=True)
    (root / "real" / "x.txt").write_text("x")
    (root / "linkdir").symlink_to(root / "real")
    device = Device({})
    with pytest.raises(transfer.TransferError, match="symlink"):
        await transfer.upload(device.send, str(root), "/workspace/up")
    assert not [a for n, a in device.sent if n == "sandbox.write"]


async def test_upload_refuses_a_missing_source(tmp_path):
    with pytest.raises(transfer.TransferError, match="no such file"):
        await transfer.upload(Device({}).send, str(tmp_path / "nope"), "/workspace")


async def test_a_refused_chunk_aborts_before_reporting_success(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("hello")
    device = Device({})
    device.fail.add("sandbox.write")
    with pytest.raises(transfer.TransferError, match="did not accept chunk 0"):
        await transfer.upload(device.send, str(source), "/workspace")


async def test_the_downloaded_tree_round_trips_through_encode():
    """The frames the walker produces are frames `protocol.encode` accepts."""
    device = Device(TREE)
    await transfer.collect(device.send, "/workspace")
    for name, args in device.sent:
        frame = Frame(type="command", id="b-1", ts=0, body={"name": name, "args": args})
        assert json.loads(encode(frame).decode())["name"] == name


async def test_a_refused_mkdir_aborts_before_the_files_inside_it(tmp_path):
    root = tmp_path / "tree"
    (root / "deep").mkdir(parents=True)
    (root / "deep" / "two.txt").write_text("2")
    device = Device({})
    device.fail.add("sandbox.mkdir")
    with pytest.raises(transfer.TransferError, match="/workspace/up/deep: the device did not create"):
        await transfer.upload(device.send, str(root), "/workspace/up")
    assert not [a for n, a in device.sent if n == "sandbox.write"]


async def test_upload_refuses_a_special_file(tmp_path):
    """A fifo has no bytes to read; `open` on one would block the walk instead."""
    root = tmp_path / "tree"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(transfer.TransferError, match="not a regular file"):
        await transfer.upload(Device({}).send, str(root), "/workspace/up")


async def test_an_empty_file_is_one_final_frame_that_clears_it(tmp_path):
    """A file with no chunks would leave the previous contents of that path."""
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    device = Device({})
    await transfer.upload(device.send, str(source), "/workspace")
    name, args = device.sent[-1]
    assert name == "sandbox.write"
    assert args["final"] is True and args["offset"] == 0
    assert base64.b64decode(args["data"]) == b""
