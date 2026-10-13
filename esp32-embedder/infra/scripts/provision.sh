#!/usr/bin/env bash
# CHATS-10: give a Kata board its IoT certificate, and build its prov partition: the settings its
# firmware reads at run time (firmware/full/main/prov.c), so no secret is ever in the build.
# tinycrypt's provision-key.sh. Safe to re-run.
#
#   infra/scripts/provision.sh <board> [--new]        (blue, green or red)
#
# Each run makes sure the board's certificate is active and attached to its Thing
# (kata-{env}-<board>, from ClaudeChatsKataDeviceStack) and to the board policy. --new retires the
# Thing's existing certificates (detach, deactivate, delete) and makes a fresh one; use it if a
# board's private key may have leaked.
#
# Writes, never committed, under firmware/full/certs/kata-{env}-<board>/:
#   device.pem.crt, device.pem.key, certificate-arn
#   prov.bin   the board's prov partition: Wi-Fi, IoT endpoint, Thing name, certificate and private
#              key, plus the credentials-provider endpoint and role alias CHATS-5 will use.
#              firmware/full/flash.sh writes it alongside the app; none of it is in the firmware
#              image, so every board runs the same (published) image.
#
# INSECURE: the private key is made here and flashed in plain text, and the Wi-Fi password sits in
# prov.bin and the board's flash. Anyone holding the board can read both.
#
# Needs AWS_PROFILE (default nakom.is-{env}) and NPM_ENVIRONMENT (default sandbox). The house Wi-Fi
# credentials are read from SSM /Nakomis/Wifi/* via WIFI_PROFILE (default nakom.is-sandbox: they
# live only in the sandbox account, so a prod board reads them from there too).
set -euo pipefail
umask 077

board=
new=false
for arg in "$@"; do
    case "$arg" in
        --new) new=true ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) board=$arg ;;
    esac
done
[ -n "$board" ] || { echo "usage: provision.sh <board> [--new]   (blue, green or red)" >&2; exit 2; }
[[ "$board" =~ ^[a-z0-9]+$ ]] || { echo "board must be a lower-case word, like blue" >&2; exit 2; }
env=${NPM_ENVIRONMENT:-sandbox}
case "$env" in sandbox | prod) ;; *) echo "NPM_ENVIRONMENT must be sandbox or prod" >&2; exit 2 ;; esac
export AWS_PROFILE=${AWS_PROFILE:-nakom.is-$env}
export AWS_REGION=${AWS_REGION:-eu-west-2}
wifi_profile=${WIFI_PROFILE:-nakom.is-sandbox}

here=$(cd "$(dirname "$0")" && pwd)
fw="${here}/../../firmware/full"
thing="kata-${env}-${board}"
policy="kata-board-${env}"
role_alias="kata-${env}"
dir="${fw}/certs/${thing}"
mkdir -p "$dir"

# Fails here if infra/boards.json (and so ClaudeChatsKataDeviceStack) lacks this board.
aws iot describe-thing --thing-name "$thing" > /dev/null

retire() {
    local arn=$1 id=${1##*/}
    aws iot detach-policy --policy-name "$policy" --target "$arn" 2> /dev/null || true
    aws iot detach-thing-principal --thing-name "$thing" --principal "$arn"
    aws iot update-certificate --certificate-id "$id" --new-status INACTIVE
    # Detaching from a Thing is eventually consistent; deletion can lag a few seconds behind.
    for _ in 1 2 3 4 5 6; do aws iot delete-certificate --certificate-id "$id" 2> /dev/null && break; sleep 5; done
    echo "retired certificate ${id}"
}

if $new; then
    for arn in $(aws iot list-thing-principals --thing-name "$thing" --query 'principals' --output text); do
        retire "$arn"
    done
    rm -f "${dir}/device.pem.crt" "${dir}/device.pem.key" "${dir}/certificate-arn"
fi

if [ ! -s "${dir}/device.pem.key" ] || [ ! -s "${dir}/certificate-arn" ]; then
    aws iot create-keys-and-certificate --set-as-active \
        --certificate-pem-outfile "${dir}/device.pem.crt" \
        --private-key-outfile "${dir}/device.pem.key" \
        --query certificateArn --output text > "${dir}/certificate-arn"
    echo "new certificate $(cut -d/ -f2 < "${dir}/certificate-arn") for ${thing}"
fi
cert_arn=$(cat "${dir}/certificate-arn")

# Both attachments are idempotent, so every run repairs a half-finished earlier one.
aws iot attach-policy --policy-name "$policy" --target "$cert_arn"
aws iot attach-thing-principal --thing-name "$thing" --principal "$cert_arn"
status=$(aws iot describe-certificate --certificate-id "${cert_arn##*/}" --query certificateDescription.status --output text)
[ "$status" = ACTIVE ] || { echo "certificate ${cert_arn##*/} is ${status}: rerun with --new" >&2; exit 1; }

endpoint=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)
cred_endpoint=$(aws iot describe-endpoint --endpoint-type iot:CredentialProvider --query endpointAddress --output text)

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
python3 - "$dir" "$endpoint" "$thing" "$cred_endpoint" "$role_alias" <<'PY'
import csv, os, sys
d, endpoint, thing, cred_endpoint, role_alias = sys.argv[1:]
ssid, password = os.environ["KATA_SSID"], os.environ["KATA_PASSWORD"]
if len(ssid.encode()) > 32 or len(password.encode()) > 63:
    sys.exit("Wi-Fi SSID or password too long for ESP-IDF (32 / 63 bytes)")
# Must fit prov.h's fixed sizes.
if len(endpoint) > 95 or len(thing) > 47 or len(cred_endpoint) > 95 or len(role_alias) > 47:
    sys.exit("an endpoint, the Thing name or the role alias is too long for prov.h")
# Namespace and keys as firmware/full/main/prov.c reads them.
rows = [
    ("kata", "namespace", "", ""),
    ("wifi_ssid", "data", "string", ssid),
    ("wifi_pass", "data", "string", password),
    ("endpoint", "data", "string", endpoint),
    ("thing", "data", "string", thing),
    ("cert", "file", "string", os.path.join(d, "device.pem.crt")),
    ("key", "file", "string", os.path.join(d, "device.pem.key")),
    # CHATS-5: the certificate buys AWS credentials here (nothing reads these yet).
    ("cred_ep", "data", "string", cred_endpoint),
    ("role_alias", "data", "string", role_alias),
]
with open(os.path.join(d, "prov.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(("key", "type", "encoding", "value"))
    w.writerows(rows)
PY
python -m esp_idf_nvs_partition_gen generate "${dir}/prov.csv" "${dir}/prov.bin" "$prov_size" > /dev/null
echo "wrote ${dir}/prov.bin for ${thing} (${endpoint}); flash it with firmware/full/flash.sh <port> ${thing}"
