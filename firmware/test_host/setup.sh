#!/bin/sh
# Host-side prerequisites for `firmware/test_host`. ESP-IDF's host port includes
# `<bsd/sys/cdefs.h>`, which ships in libbsd-dev; this machine has no root access
# for it, so the package's headers and stub library are unpacked under
# IDF_TOOLS_PATH where the host project finds them. Idempotent: re-running is a
# no-op once the headers are in place.
#
# Run with `idf.py` already on PATH (`idfenv` in the shell rc), or set IDF_PATH.
set -eu

tools="${IDF_TOOLS_PATH:-$HOME/.espressif}"
include="$tools/libbsd_include"
lib="$tools/libbsd_lib"

if [ -e "$include/aarch64-linux-gnu/bsd/sys/cdefs.h" ]; then
    echo "libbsd headers already present in $include"
    exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

(cd "$tmp" && apt-get download libbsd-dev)
dpkg -x "$tmp"/libbsd-dev_*.deb "$tmp/root"

mkdir -p "$include" "$lib"
cp -r "$tmp/root/usr/include/." "$include/"
# Debian multiarch triplet differs from `dpkg --print-architecture` (arm64 here).
arch=$(ls "$tmp/root/usr/lib" | head -1)
cp -r "$tmp/root/usr/lib/$arch/." "$lib/"

echo "libbsd headers -> $include, stub library -> $lib"
