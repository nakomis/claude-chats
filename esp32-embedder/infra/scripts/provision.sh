#!/usr/bin/env bash
# CHATS-10: build a Kata board's prov partition: the settings its firmware reads at run time
# (firmware/full/main/prov.c), so no secret is ever in the build. tinycrypt's provision-key.sh,
# without the IoT identity for now. Safe to re-run.
#
#   infra/scripts/provision.sh <board>        (blue, green or red)
#
# Writes, never committed, firmware/full/certs/kata-{env}-<board>/prov.bin, which
# firmware/full/flash.sh writes alongside the app. Holds today: the house Wi-Fi SSID and password.
# Device identity (CHATS-10 part 2) adds the IoT endpoint, Thing name, certificate and private key
# here, and the directory is already named after the board's Thing.
#
# INSECURE: the Wi-Fi password sits in prov.bin and in the board's flash, in plain text.
#
# Needs NPM_ENVIRONMENT (default sandbox). The house Wi-Fi credentials are read from SSM
# /Nakomis/Wifi/* via WIFI_PROFILE (default nakom.is-sandbox: they live only in the sandbox account,
# so a prod board reads them from there too).
set -euo pipefail
umask 077

board=${1:?usage: provision.sh <board> (blue, green or red)}
[[ "$board" =~ ^[a-z0-9]+$ ]] || { echo "board must be a lower-case word, like blue" >&2; exit 2; }
env=${NPM_ENVIRONMENT:-sandbox}
case "$env" in sandbox | prod) ;; *) echo "NPM_ENVIRONMENT must be sandbox or prod" >&2; exit 2 ;; esac
wifi_profile=${WIFI_PROFILE:-nakom.is-sandbox}
export AWS_REGION=${AWS_REGION:-eu-west-2}

here=$(cd "$(dirname "$0")" && pwd)
fw="${here}/../../firmware/full"
name="kata-${env}-${board}"
dir="${fw}/certs/${name}"
mkdir -p "$dir"

# Through the environment, not argv, so the password never shows up in `ps`.
export KATA_SSID KATA_PASSWORD
KATA_SSID=$(aws ssm get-parameter --profile "$wifi_profile" --name /Nakomis/Wifi/SSID --query Parameter.Value --output text)
KATA_PASSWORD=$(aws ssm get-parameter --profile "$wifi_profile" --name /Nakomis/Wifi/Password --with-decryption \
    --query Parameter.Value --output text)

IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" > /dev/null 2>&1   # for its Python, which has the NVS partition generator
trap 'rm -P "${dir}/prov.csv" 2> /dev/null || true' EXIT   # it holds the Wi-Fi password
prov_size=$(awk -F', *' '$1 == "prov" { print $5 }' "${fw}/partitions.csv")
python3 - "$dir" <<'PY'
import csv, os, sys
d = sys.argv[1]
ssid, password = os.environ["KATA_SSID"], os.environ["KATA_PASSWORD"]
if len(ssid.encode()) > 32 or len(password.encode()) > 63:
    sys.exit("Wi-Fi SSID or password too long for ESP-IDF (32 / 63 bytes)")
# Namespace and keys as firmware/full/main/prov.c reads them.
rows = [
    ("kata", "namespace", "", ""),
    ("wifi_ssid", "data", "string", ssid),
    ("wifi_pass", "data", "string", password),
]
with open(os.path.join(d, "prov.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(("key", "type", "encoding", "value"))
    w.writerows(rows)
PY
python -m esp_idf_nvs_partition_gen generate "${dir}/prov.csv" "${dir}/prov.bin" "$prov_size" > /dev/null
echo "wrote ${dir}/prov.bin; flash it with firmware/full/flash.sh <port> ${name}"
