#!/usr/bin/env python3
"""Build and flash the `userfs` littlefs image (`docs/05-bridge.md` §11).

Host-only tool beside `test_agent.py`: the bridge never imports it, because an
image build is a provisioning step, not a layer. It takes a source directory,
produces an image sized for the `userfs` partition and, only when given a port,
writes it at that partition's offset.

Partition truth is read from `firmware/partitions.csv` and the filesystem
geometry from the firmware's own littlefs recipe
(`managed_components/joltwallet__littlefs/project_include.cmake`), so an image
built here matches the one the component would build. Both are read, never
restated: a second copy of `0x320000` is a lie waiting for the table to move.

Usage:
    bridge/tools/build_userfs.py --build-only --source firmware/userfs \\
        -o firmware/build/userfs.bin
    bridge/tools/build_userfs.py --source firmware/userfs --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

REPO = Path(__file__).resolve().parents[2]
PARTITIONS = REPO / "firmware" / "partitions.csv"
SDKCONFIG = REPO / "firmware" / "sdkconfig"
#: The reviewable seed tree this tool bakes into the default image
#: (`docs/03-config.md` §config decisions item 7).
SEED = REPO / "firmware" / "userfs"
PARTITION = "userfs"

#: `project_include.cmake` passes `--block-size=4096`; littlefs on flash uses the
#: sector size, and the ESP32-S3 NOR sector is 4 KiB.
BLOCK_SIZE = 4096

#: The version `run_qemu.sh` pins and the one this tool is exercised against.
LITTLEFS_PACKAGE = "littlefs-python==0.15.0"

#: `docs/01-hardware.md` §10 uses this rate; the native USB-JTAG link completes
#: at it, where the IDF-bundled stub stalls.
BAUD = 460800


class ToolError(RuntimeError):
    """A refusal with a message the reader can act on."""


def partition(name: str, path: Path = PARTITIONS) -> tuple[int, int]:
    """`(offset, size)` of one row of the partition table, in bytes."""
    if not path.is_file():
        raise ToolError(f"partition table not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        # Name, Type, SubType, Offset, Size, Flags: a row needs at least five.
        if len(cells) < 5 or cells[0].startswith("#"):
            continue
        if cells[0] == name:
            return int(cells[3], 0), int(cells[4], 0)
    raise ToolError(f"{path} has no partition named {name!r}")


def obj_name_len(path: Path = SDKCONFIG) -> int:
    """`CONFIG_LITTLEFS_OBJ_NAME_LEN`, the `--name-max` the firmware builds with.

    Not a constant: the filesystem stores this limit and refuses an image built
    with a different one, so reading it is the only way the two cannot drift.
    """
    if not path.is_file():
        raise ToolError(f"{path} not found; build the firmware once first")
    match = re.search(
        r"^CONFIG_LITTLEFS_OBJ_NAME_LEN=(\d+)$",
        path.read_text(encoding="utf-8"),
        re.M,
    )
    if match is None:
        raise ToolError(f"{path} does not set CONFIG_LITTLEFS_OBJ_NAME_LEN")
    return int(match.group(1))


def _imports_littlefs(python: str) -> bool:
    try:
        probe = subprocess.run(
            [python, "-c", "import littlefs"], capture_output=True, check=False
        )
    except OSError:
        return False
    return probe.returncode == 0


def _candidates(explicit: str | None) -> Iterator[str]:
    if explicit:
        # An explicit interpreter is the answer or the error; substituting another
        # one would report success for a toolchain the reader did not ask for.
        yield explicit
        return
    seen: set[str] = set()
    env = os.environ.get("IDF_PYTHON_ENV_PATH")
    paths = [
        str(Path(env, "bin", "python")) if env else "",
        str(Path.home() / ".espressif" / "python_env" / "idf5.5_py3.13_env" / "bin" / "python"),
    ]
    paths += [
        str(path)
        for path in sorted((Path.home() / ".espressif" / "python_env").glob("*/bin/python"))
    ]
    paths.append(sys.executable)
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            yield path


def find_python(explicit: str | None = None) -> str:
    """An interpreter that can `import littlefs`, or an actionable refusal."""
    candidates = list(_candidates(explicit))
    for candidate in candidates:
        if Path(candidate).exists() and _imports_littlefs(candidate):
            return candidate
    hint = explicit or sys.executable
    raise ToolError(
        "no interpreter can import littlefs. Tried: "
        + (", ".join(candidates) or "nothing")
        + f"\nInstall it with:\n  {hint} -m pip install {LITTLEFS_PACKAGE}"
        + "\nor point at one that has it with --python /path/to/python"
    )


def esptool(python: str) -> str:
    """`esptool.py` beside the chosen interpreter, else the one on PATH."""
    sibling = Path(python).with_name("esptool.py")
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("esptool.py") or shutil.which("esptool")
    if found is None:
        raise ToolError(f"esptool.py not found beside {python} or on PATH")
    return found


def list_image(python: str, image: Path) -> list[str]:
    """Every path in the image, as the filesystem itself reports them."""
    listed = subprocess.run(
        [
            python, "-m", "littlefs", "list", str(image),
            f"--block-size={BLOCK_SIZE}",
            f"--name-max={obj_name_len()}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in listed.stdout.splitlines() if line]


def build_image(
    source: Path,
    output: Path,
    size: int,
    name_max: int,
    python: str | None = None,
) -> Path:
    """Create `output` from `source`, padded to the partition's exact size."""
    if not source.is_dir():
        raise ToolError(f"source directory not found: {source}")
    interpreter = python or find_python()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            interpreter, "-m", "littlefs", "create", str(source), str(output),
            f"--fs-size={size}",
            f"--name-max={name_max}",
            f"--block-size={BLOCK_SIZE}",
        ],
        check=True,
    )
    produced = output.stat().st_size
    if produced != size:
        raise ToolError(
            f"{output} is {produced} bytes after the build; the {PARTITION} "
            f"partition is {size}"
        )
    return output


def flash(image: Path, port: str, baud: int, python: str | None = None) -> None:
    """Write `image` at the partition offset over `port`."""
    interpreter = python or find_python()
    offset, _ = partition(PARTITION)
    subprocess.run(
        [
            esptool(interpreter), "--chip", "esp32s3", "-p", port, "-b", str(baud),
            "write-flash", hex(offset), str(image),
        ],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the userfs littlefs image from a source tree, and flash it at "
            "the partition offset only when --port is given."
        )
    )
    parser.add_argument(
        "--source", type=Path, default=SEED,
        help=f"tree to bake into the image (default: {SEED})",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=REPO / "firmware" / "build" / "userfs.bin",
        help="image to write (default: firmware/build/userfs.bin)",
    )
    parser.add_argument(
        "--port", help="serial port; omitting it builds and never writes flash",
    )
    parser.add_argument(
        "--baud", type=int, default=BAUD, help=f"flashing baud (default: {BAUD})",
    )
    parser.add_argument(
        "--build-only", action="store_true",
        help="build the image and stop; this is also what omitting --port does",
    )
    parser.add_argument(
        "--python",
        help=(
            f"interpreter that can import littlefs ({LITTLEFS_PACKAGE}); "
            "default: probe the ESP-IDF environment, then this interpreter"
        ),
    )
    args = parser.parse_args(argv)
    if args.build_only and args.port:
        parser.error("--build-only and --port contradict each other")

    try:
        offset, size = partition(PARTITION)
        name_max = obj_name_len()
        interpreter = find_python(args.python)
    except ToolError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        image = build_image(args.source, args.output, size, name_max, interpreter)
        print(f"userfs: {image} ({image.stat().st_size} bytes, 0x{offset:x} on flash)")
        for path in list_image(interpreter, image):
            print(f"  {path}")
    except (ToolError, subprocess.CalledProcessError) as exc:
        print(f"image build failed: {exc}", file=sys.stderr)
        return 1

    if args.port is None:
        print("no --port: image built, flash not written")
        return 0
    try:
        flash(image, args.port, args.baud, interpreter)
    except (ToolError, subprocess.CalledProcessError) as exc:
        print(f"flash failed: {exc}", file=sys.stderr)
        return 1
    print(f"flashed {image} at 0x{offset:x} over {args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
