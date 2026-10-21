#!/usr/bin/env bash
# CHATS-10: give a Kata board its IoT certificate, and build its prov partition: the settings its
# firmware reads at run time (firmware/full/main/prov.c), so no secret is ever in the build.
# tinycrypt's provision-key.sh. Safe to re-run.
#
#   infra/scripts/provision.sh <board> [--new]        (blue, kata or red)
#
# Each run makes sure the board's certificate is active and attached to its Thing
# (kata-{env}-<board>, from ClaudeChatsKataDeviceStack) and to the board policy. --new retires the
# Thing's existing certificates (detach, deactivate, delete) and makes a fresh one; use it if a
# board's private key may have leaked.
#
# Writes, never committed, under firmware/full/certs/kata-{env}-<board>/:
#   device.pem.crt, device.pem.key, certificate-arn
#   prov.bin   the board's prov partition: Wi-Fi, IoT endpoint, Thing name, certificate and private
#              key, the credentials-provider endpoint and role alias, and (CHATS-5) the board's age
#              identity from SSM /kata/{env}/boards/<board>/age-identity (make it first with
#              age-identity.sh <board>; without it the board can't decrypt jobs, and says so),
#              and the ingest and results queues' URLs (/conversation-memory/{env}/queue-url,
#              results-queue-url, from home-infra; without them the worker doesn't start).
#              firmware/full/flash.sh writes it alongside the app; none of it is in the firmware
#              image, so every board runs the same (published) image.
#
# Metrics (CHATS-20): the board pushes its health to the house VictoriaMetrics through vmauth on Cal
# (HOME-437). METRICS_URL (default http://172.29.0.33:8427; "none" for no metrics). vmauth has a user
# per device, named METRICS_USER (default: the Thing; "kata-prod-kata" while the board is still
# provisioned as kata-prod-green), whose password is SSM /vmauth/device-<user>-password (read via
# METRICS_PROFILE, default nakom.is-prod, where vmauth's credentials live). vmauth pins host_name to the
# user, so a leaked password can only write metrics as this board. If the password can't be read, the
# board is provisioned without metrics (a warning, not a failure). Firmware: main/metrics.c.
#
# INSECURE: the private key is made here and flashed in plain text, and the Wi-Fi password and the
# age identity sit in prov.bin and the board's flash. Anyone holding the board can read all three.
#
# Needs NPM_ENVIRONMENT (default sandbox). The profile is nakom.is-{env} (KATA_AWS_PROFILE overrides
# it; a pre-set AWS_PROFILE is ignored), and the script stops unless it's that environment's account. The house Wi-Fi
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
[ -n "$board" ] || { echo "usage: provision.sh <board> [--new]   (blue, kata or red)" >&2; exit 2; }
[[ "$board" =~ ^[a-z0-9]+$ ]] || { echo "board must be a lower-case word, like blue" >&2; exit 2; }
env=${NPM_ENVIRONMENT:-sandbox}
# shellcheck source-path=SCRIPTDIR source=aws-env.sh
. "$(dirname "$0")/aws-env.sh"
kata_aws_env
kata_check_account
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
# CHATS-5: the board's own age identity (age-identity.sh). Only the AGE-SECRET-KEY-1… line, not
# age-keygen's comments. Optional, so a board can still be provisioned for Fleet without it.
export KATA_AGE_ID
age_param="/kata/${env}/boards/${board}/age-identity"
if age_out=$(aws ssm get-parameter --name "$age_param" --with-decryption --query Parameter.Value --output text 2>&1); then
    KATA_AGE_ID=$(printf '%s\n' "$age_out" | grep '^AGE-SECRET-KEY-1' || true)
    [ -n "$KATA_AGE_ID" ] || { echo "${age_param} holds no AGE-SECRET-KEY-1 line" >&2; exit 1; }
elif [[ "$age_out" == *ParameterNotFound* ]]; then
    KATA_AGE_ID=
    echo "no ${age_param}: prov.bin will have no age identity (run age-identity.sh ${board} first)" >&2
else
    echo "$age_out" >&2
    exit 1
fi
unset age_out
# CHATS-5: the queues the worker uses, as home-infra's ConversationMemoryIngestStack publishes them
# (HOME-439). Optional too: without them the worker doesn't start, and the board says so.
queue_param() {
    aws ssm get-parameter --name "/conversation-memory/${env}/$1" --query Parameter.Value --output text 2> /dev/null || true
}
queue_url=$(queue_param queue-url)
results_url=$(queue_param results-queue-url)
[ -n "$queue_url" ] && [ -n "$results_url" ] ||
    echo "no /conversation-memory/${env}/queue-url or results-queue-url: prov.bin will have no queues (deploy home-infra first)" >&2
# CHATS-16: who may ask the board for GET /json (statusd.c): addresses, comma-separated (Leia's, for
# kata.home.nakomis.com). Optional: without it, any peer on the board's own subnet. Only a parameter
# that isn't there means that; any other error (an expired session, AccessDenied) stops here, so a
# failed read never quietly opens the board to the subnet.
if allow_out=$(aws ssm get-parameter --name "/kata/${env}/status-allow" --query Parameter.Value --output text 2>&1); then
    status_allow=$allow_out
elif [[ "$allow_out" == *ParameterNotFound* ]]; then
    status_allow=
    echo "no /kata/${env}/status-allow: the board answers GET /json to anyone on its subnet" >&2
else
    echo "$allow_out" >&2
    exit 1
fi
unset allow_out
# CHATS-20: metrics (see the top). Through the environment, so the password never shows up in `ps`.
export KATA_METRICS_URL=${METRICS_URL:-http://172.29.0.33:8427} KATA_METRICS_USER=${METRICS_USER:-$thing} KATA_METRICS_PASS=
if [ "$KATA_METRICS_URL" = none ]; then
    KATA_METRICS_URL=
elif ! KATA_METRICS_PASS=$(aws ssm get-parameter --profile "${METRICS_PROFILE:-nakom.is-prod}" \
        --name "/vmauth/device-${KATA_METRICS_USER}-password" --with-decryption --query Parameter.Value --output text 2> /dev/null); then
    echo "warning: can't read /vmauth/device-${KATA_METRICS_USER}-password (profile ${METRICS_PROFILE:-nakom.is-prod}): no metrics for ${thing}" >&2
    KATA_METRICS_URL= KATA_METRICS_PASS=
fi

IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" > /dev/null 2>&1   # for its Python, which has the NVS partition generator
trap 'rm -P "${dir}/prov.csv" 2> /dev/null || true' EXIT   # it holds the Wi-Fi password and age key
prov_size=$(awk -F', *' '$1 == "prov" { print $5 }' "${fw}/partitions.csv")
python3 - "$dir" "$endpoint" "$thing" "$cred_endpoint" "$role_alias" "$queue_url" "$results_url" "$status_allow" <<'PY'
import csv, ipaddress, os, re, sys
d, endpoint, thing, cred_endpoint, role_alias, queue_url, results_url, status_allow = sys.argv[1:]
ssid, password = os.environ["KATA_SSID"], os.environ["KATA_PASSWORD"]
age_id = os.environ["KATA_AGE_ID"]
if age_id and (len(age_id) != 74 or not age_id.startswith("AGE-SECRET-KEY-1")):
    sys.exit("the age identity isn't a single AGE-SECRET-KEY-1 line of 74 characters")
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
    # CHATS-5: the certificate buys AWS credentials here, for the worker.
    ("cred_ep", "data", "string", cred_endpoint),
    ("role_alias", "data", "string", role_alias),
]
for url in (queue_url, results_url):
    if url and (len(url) > 159 or not url.startswith("https://sqs.")):
        sys.exit(f"{url}: not an SQS queue URL that fits prov.h")
if queue_url and results_url:
    # CHATS-5: the worker's queues (firmware/full/main/kata_worker.c)
    rows.append(("queue_url", "data", "string", queue_url))
    rows.append(("results_url", "data", "string", results_url))
if age_id:
    # CHATS-5: the board's age identity, to decrypt conversation-memory jobs.
    rows.append(("age_id", "data", "string", age_id))
if status_allow:
    # CHATS-16: as worker/httpreq.c parses it (at most 4 IPv4 addresses, prov.h's 80 bytes). A list
    # the board can't parse makes it refuse everyone, so check it here rather than find out there.
    addrs = [a for a in re.split(r"[ ,]+", status_allow) if a]
    try:
        ok = 0 < len(addrs) <= 4 and all(ipaddress.IPv4Address(a) and str(ipaddress.IPv4Address(a)) == a for a in addrs)
    except ValueError:
        ok = False
    if not ok or len(status_allow) > 79:
        sys.exit(f"/kata/.../status-allow {status_allow!r}: want 1 to 4 IPv4 addresses, comma-separated")
    rows.append(("status_allow", "data", "string", status_allow))
# CHATS-20: metrics to vmauth, sizes as prov.h's.
if os.environ["KATA_METRICS_URL"]:
    murl, muser, mpass = os.environ["KATA_METRICS_URL"], os.environ["KATA_METRICS_USER"], os.environ["KATA_METRICS_PASS"]
    if len(murl.encode()) > 127 or len(muser.encode()) > 47 or len(mpass.encode()) > 64:
        sys.exit("metrics URL, user or password too long for prov.h (127 / 47 / 64 bytes)")
    rows += [("metrics_url", "data", "string", murl), ("metrics_user", "data", "string", muser),
             ("metrics_pass", "data", "string", mpass)]
with open(os.path.join(d, "prov.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(("key", "type", "encoding", "value"))
    w.writerows(rows)
PY
python -m esp_idf_nvs_partition_gen generate "${dir}/prov.csv" "${dir}/prov.bin" "$prov_size" > /dev/null
echo "wrote ${dir}/prov.bin for ${thing} (${endpoint}); flash it with firmware/full/flash.sh <port> ${thing}"
