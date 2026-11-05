#!/usr/bin/env bash
# run_qemu.sh - boot the firmware under qemu-system-xtensa, no board attached.
#
# Builds a QEMU-only image under build/qemu/ from sdkconfig.defaults (dio flash,
# see sdkconfig.qemu.defaults), merges it into an 8 MiB flash image, seeds
# userfs, and runs the machine. The board sdkconfig is never read or written.
#
# Usage: firmware/tools/run_qemu.sh [--no-build] [--keep] [--pty] [qemu args...]
#        QEMU_BIN=/path/to/qemu-system-xtensa firmware/tools/run_qemu.sh
#
# --no-build boots what is already in build/qemu/.
# --keep reuses the existing flash image, so NVS state survives across runs: the
# boot counter reaching safe mode needs several boots of one image (§3.2).
# --pty puts UART0 on a pty linked at build/qemu/uart, for the bridge to open.
#        No argument selects the number; extra arguments go to QEMU. There is no
#        monitor in this mode, so Ctrl-C stops it. Attach before the guest boots or
#        the output is lost — see the note below the invocation.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJ=$(dirname "$HERE")
# idf.py takes its project directory from the CWD, and SDKCONFIG_DEFAULTS names are
# resolved relative to it, so the script must run from the project root.
cd "$PROJ"
BUILD="$PROJ/build/qemu"
IMAGE="$BUILD/qemu_flash.bin"
SEED="$BUILD/userfs.bin"
QEMU_BIN=${QEMU_BIN:-$PROJ/../../qemu/build/qemu-system-xtensa}

# userfs is never formatted on mount (docs/02-firmware.md §store), so a blank
# partition halts boot with E_STORAGE. The real builder is the M4
# bridge/tools/build_userfs.py; this is the minimum tree that mounts.
seed_userfs() {
    local dir="$BUILD/userfs_seed"
    mkdir -p "$dir/prompts" "$dir/skills" "$dir/tools"
    printf '{"wifi":{"ssid":"","password":""}}\n' > "$dir/config.json"
    printf 'You are Musteval.\n' > "$dir/prompts/system.md"

    local py="$BUILD/littlefs_venv/bin/python"
    if [ ! -x "$py" ]; then
        python3 -m venv "$BUILD/littlefs_venv"
        "$py" -m pip install --quiet "littlefs-python==0.15.0"
    fi
    # Geometry from the component's own recipe: fs-size is the partition length
    # (docs/01-hardware.md §4), name-max is CONFIG_LITTLEFS_OBJ_NAME_LEN.
    "$py" -m littlefs create "$dir" "$SEED" \
        --fs-size=0x3C0000 --name-max=64 --block-size=4096 >/dev/null
}

build=1
keep=0
pty=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) build=0 ;;
        --keep)     keep=1 ;;
        --pty)      pty=1 ;;
        *)          break ;;
    esac
    shift
done

# shellcheck disable=SC1091
. "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" >/dev/null 2>&1

if [ "$build" = 1 ]; then
    idf.py -B "$BUILD" \
        -D SDKCONFIG="$BUILD/sdkconfig" \
        -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.qemu.defaults" \
        build
fi

if [ "$keep" = 0 ]; then
    # QEMU picks its flash model by exact drive length (hw/xtensa/esp32s3.c): only 2,
    # 4, 8 and 16 MiB are accepted, so the image is padded to 8 MiB to match the part.
    # flash_args is multi-line: options first, then `offset file` pairs.
    opts=(); pairs=()
    while IFS= read -r line; do
        if [ ${#opts[@]} -eq 0 ]; then
            read -r -a opts <<< "$line"
        else
            pairs+=("$line")
        fi
    done < "$BUILD/flash_args"
    read -r -a pairs <<< "${pairs[*]}"
    ( cd "$BUILD" && esptool.py --chip esp32s3 merge_bin --output "$IMAGE" \
        "${opts[@]}" "${pairs[@]}" >/dev/null )
    truncate -s 8M "$IMAGE"

    seed_userfs
    dd if="$SEED" of="$IMAGE" bs=1 seek=$((0x320000)) conv=notrunc status=none
fi

# A blank efuse image is enough: chip rev reads v0.0, which CONFIG_ESP32S3_REV_MIN
# accepts. `idf.py qemu` writes a v0.3 blob instead; only the revision log differs.
[ -f "$BUILD/qemu_efuse.bin" ] || truncate -s 1024 "$BUILD/qemu_efuse.bin"

# -m 8M, not idf.py qemu's default 32M: the PSRAM mapping shares the MMU vaddr
# window, and 32M leaves none for the flash mapping, so the partition table cannot
# be read. is_octal matches CONFIG_SPIRAM_MODE_OCT.
#
# Default: UART0 on this terminal's stdin/stdout, no pts; exit with Ctrl-A then x.
# --pty allocates one and links it at $UART_LINK, so the path is stable across runs
# instead of a /dev/pts/N number that changes every time. The monitor is off in that
# mode, so the terminal carries nothing and Ctrl-C stops the guest.
#
# A pty is not a log. A reader that opens the slave before the guest writes receives
# the whole boot log; one that attaches later finds the buffer already flushed. Holding
# the slave open in this script does not help: pyserial calls tcflush(TCIFLUSH) on open
# (serialposix.py, _reset_input_buffer), so whatever was buffered is discarded at the
# moment the bridge attaches. Reading the log means the default mode.
UART_LINK="$BUILD/uart"
if [ "$pty" = 1 ]; then
    rm -f "$UART_LINK"
    serial=(-display none -monitor none -chardev "pty,id=serial0,path=$UART_LINK"
            -serial chardev:serial0)
    echo "uart: $UART_LINK (resolved below); Ctrl-C to stop" >&2
else
    serial=(-nographic -serial mon:stdio)
fi

exec "$QEMU_BIN" -M esp32s3 -m 8M \
    -drive "file=$IMAGE,if=mtd,format=raw" \
    -drive "file=$BUILD/qemu_efuse.bin,if=none,format=raw,id=efuse" \
    -global driver=nvram.esp32s3.efuse,property=drive,value=efuse \
    -global driver=timer.esp32s3.timg,property=wdt_disable,value=true \
    -global driver=ssi_psram,property=is_octal,value=true \
    -nic user,model=open_eth "${serial[@]}" "$@"
