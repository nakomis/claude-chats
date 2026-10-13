#!/usr/bin/env bash
# Flash a board over USB: the bootloader, partition table, OTA data and the signed app (build.sh),
# plus the board's prov partition from certs/<name>/prov.bin (infra/scripts/provision.sh; the only
# one there if no name is given). tinycrypt's flash-manual.sh.
#
#   firmware/full/flash.sh /dev/cu.usbmodemXXXX [kata-sandbox-blue]
#
# Never the nvs partition. Writing OTA data boots ota_0, whichever slot an update had moved to.
# As tinycrypt, --before no_reset: put the board in download mode first (hold BOOT, tap RST,
# release BOOT). BEFORE=default_reset lets esptool do that itself over the native USB port instead.
# Either way a watchdog reset afterwards starts the new firmware (a plain hard reset over the
# native USB port leaves the S3 in download mode).
#
# The app must be signed by this environment's key (ota-signing/{env}.pub.pem, env from
# NPM_ENVIRONMENT, default sandbox): one signed by another would boot, then refuse every update.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
port=${1:?"usage: flash.sh <port> [kata-{env}-{board}]"}
cd "$here/build"
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" > /dev/null

env=${NPM_ENVIRONMENT:-sandbox}
espsecure.py verify_signature --version 2 --keyfile "$here/ota-signing/${env}.pub.pem" kata.bin > /dev/null 2>&1 || {
    echo "build/kata.bin isn't signed by the $env key (ota-signing/${env}.pub.pem): run build.sh" >&2
    exit 1
}

prov=()
if [ -n "${2:-}" ]; then
    prov=("$here/certs/$2/prov.bin")
else
    shopt -s nullglob
    prov=("$here"/certs/*/prov.bin)
fi
if [ "${#prov[@]}" -ne 1 ] || [ ! -s "${prov[0]}" ]; then
    echo "need exactly one prov.bin (have ${#prov[@]}): name the board, or run infra/scripts/provision.sh" >&2
    exit 1
fi
offset=$(awk -F', *' '$1 == "prov" { print $4 }' "$here/partitions.csv")
echo "prov: ${prov[0]} at ${offset}"
python -m esptool --chip esp32s3 -p "$port" -b 460800 --before "${BEFORE:-no_reset}" --after watchdog_reset write_flash @flash_args \
    "$offset" "${prov[0]}"
