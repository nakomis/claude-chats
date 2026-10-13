#!/usr/bin/env bash
# Build and sign Kata's firmware (then flash it with flash.sh). tinycrypt's build.sh.
#   firmware/full/build.sh                 build, then sign build/kata.bin (sign.sh)
#   firmware/full/build.sh menuconfig      any other idf.py command, passed through
# Images are signed (CHATS-10), so don't flash with `idf.py flash`: it would write the unsigned one.
set -euo pipefail
cd "$(dirname "$0")"
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" > /dev/null
if [ $# -gt 0 ]; then
    exec idf.py "$@"
fi
idf.py build
./sign.sh build/kata.bin
