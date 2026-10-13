#!/usr/bin/env bash
# CHATS-5: give a Kata board its own age identity, the key it decrypts conversation-memory jobs with.
# One per board (Martin, 13 Oct), so a lost board is revoked on its own. Safe to re-run.
#
#   infra/scripts/age-identity.sh <board>            make one if the board has none (blue, green, red…)
#   infra/scripts/age-identity.sh <board> --new      replace it (a key that may have leaked)
#   infra/scripts/age-identity.sh <board> --revoke   remove it (a lost or retired board)
#
# Two SSM parameters, in the account the board's Thing (kata-{env}-<board>) lives in:
#   /kata/{env}/boards/<board>/age-identity   SecureString: the private key. provision.sh copies it
#                                              into the board's prov.bin (NVS key age_id); it's never
#                                              in the firmware image, the repo or a command line.
#   /conversation-memory/{env}/age-recipients/kata-{env}-<board>
#                                              String: the public key. The forwarder encrypts every
#                                              job to Cal's /conversation-memory/{env}/age-recipient
#                                              and to each parameter under …/age-recipients/, so a
#                                              new board needs no forwarder config, only a reinstall
#                                              (or whatever refresh the forwarder does).
#
# After making or replacing a key, run provision.sh <board> and flash the new prov.bin. Jobs already
# queued for the old key reach Cal anyway: Kata hands them off as unembedded/decrypt-failed.
#
# INSECURE, deliberately (accepted 13 Oct): the key sits in the board's prov partition unencrypted,
# so anyone holding the board can read it. That's why each board has its own, and why --revoke exists.
#
# Needs age-keygen (brew install age) and NPM_ENVIRONMENT (default sandbox). The profile is
# nakom.is-{env} (KATA_AWS_PROFILE overrides it; a pre-set AWS_PROFILE is ignored), and the script
# stops unless it's that environment's account.
set -euo pipefail
umask 077

board=
mode=ensure
for arg in "$@"; do
    case "$arg" in
        --new) mode=new ;;
        --revoke) mode=revoke ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) board=$arg ;;
    esac
done
[ -n "$board" ] || { echo "usage: age-identity.sh <board> [--new | --revoke]" >&2; exit 2; }
[[ "$board" =~ ^[a-z0-9]+$ ]] || { echo "board must be a lower-case word, like blue" >&2; exit 2; }
env=${NPM_ENVIRONMENT:-sandbox}
# shellcheck source-path=SCRIPTDIR source=aws-env.sh
. "$(dirname "$0")/aws-env.sh"
kata_aws_env
kata_check_account

thing="kata-${env}-${board}"
identity_param="/kata/${env}/boards/${board}/age-identity"
recipient_param="/conversation-memory/${env}/age-recipients/${thing}"

# Deletes an SSM parameter. Absent is fine; any other failure (expired credentials…) stops the script.
delete_param() {
    local err
    if err=$(aws ssm delete-parameter --name "$1" 2>&1); then
        echo "deleted $1"
    elif [[ "$err" == *ParameterNotFound* ]]; then
        echo "no $1"
    else
        echo "$err" >&2
        echo "could not delete $1: stopping" >&2
        exit 1
    fi
}

if [ "$mode" = revoke ]; then
    # 1. Cut the board off from AWS: its certificate is what buys IoT Core and SQS credentials. A
    #    certificate is deactivated (kept for the audit trail, not deleted), then detached.
    if out=$(aws iot list-thing-principals --thing-name "$thing" --query principals --output text 2>&1); then
        for arn in $out; do
            [ "$arn" = None ] && continue
            aws iot update-certificate --certificate-id "${arn##*/}" --new-status INACTIVE
            aws iot detach-policy --policy-name "kata-board-${env}" --target "$arn" 2> /dev/null || true
            aws iot detach-thing-principal --thing-name "$thing" --principal "$arn"
            echo "deactivated and detached certificate ${arn##*/}"
        done
    elif [[ "$out" == *ResourceNotFound* ]]; then
        echo "no Thing ${thing}: no certificate to deactivate"
    else
        echo "$out" >&2
        echo "could not list ${thing}'s certificates: stopping, nothing revoked" >&2
        exit 1
    fi
    # 2. The recipient, so forwarders stop encrypting to the key; then the key itself.
    delete_param "$recipient_param"
    delete_param "$identity_param"
    echo "revoked ${thing}. Still to do:"
    echo "  - reinstall the forwarder on every Mac, so it stops encrypting to the board's key"
    echo "  - credentials the board already holds last up to an hour"
    echo "  - to retire the board for good, remove it from infra/boards.json and redeploy"
    exit 0
fi

command -v age-keygen > /dev/null || { echo "needs age-keygen: brew install age" >&2; exit 1; }
# Fails here if infra/boards.json (and so ClaudeChatsKataDeviceStack) lacks this board.
aws iot describe-thing --thing-name "$thing" > /dev/null

dir=$(mktemp -d)
trap 'rm -P "$dir"/* 2> /dev/null; rmdir "$dir"' EXIT
key="${dir}/identity.age"

have_key=false
if [ "$mode" = ensure ]; then
    if aws ssm get-parameter --name "$identity_param" --with-decryption --query Parameter.Value --output text \
        > "$key" 2> "${dir}/err"; then
        have_key=true
        echo "${identity_param} exists; keeping it"
    elif ! grep -q ParameterNotFound "${dir}/err"; then
        # Anything but "not there" (expired credentials, wrong profile…) must not become a new key.
        cat "${dir}/err" >&2
        exit 1
    fi
fi
if ! $have_key; then
    rm -f "$key"
    age-keygen -o "$key" 2> /dev/null
    # file://, so the key is never on a command line (where ps would show it). Without --overwrite
    # unless asked, so a key made meanwhile by another run is never silently replaced.
    overwrite=()
    [ "$mode" = new ] && overwrite=(--overwrite)
    aws ssm put-parameter --name "$identity_param" --type SecureString ${overwrite[@]+"${overwrite[@]}"} --value "file://${key}" \
        --description "Kata board ${thing}: age identity (private key) for conversation-memory jobs" > /dev/null
    echo "made a new age identity for ${thing} in ${identity_param}"
fi

recipient=$(age-keygen -y "$key")
[[ "$recipient" =~ ^age1[0-9a-z]{58}$ ]] || { echo "age-keygen gave an odd public key: ${recipient}" >&2; exit 1; }
current=$(aws ssm get-parameter --name "$recipient_param" --query Parameter.Value --output text 2> /dev/null || true)
if [ "$current" != "$recipient" ]; then
    aws ssm put-parameter --name "$recipient_param" --type String --overwrite --value "$recipient" \
        --description "Kata board ${thing}: age recipient. The forwarder encrypts each job to this too." > /dev/null
    echo "published ${recipient} as ${recipient_param}"
    echo "next: provision.sh ${board} and flash its prov.bin; then reinstall the forwarder on every Mac"
else
    echo "${recipient_param} is already ${recipient}"
fi
