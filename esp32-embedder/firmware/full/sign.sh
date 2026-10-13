#!/usr/bin/env bash
# CHATS-10: sign the app image in place (RSA-3072, secure boot v2 scheme) and check the signature.
# tinycrypt's sign.sh. Every image is signed, dev builds flashed over USB included: once Fleet OTA
# lands, a board only accepts an update signed by the key that signed the image it's running.
#
#   firmware/full/sign.sh [build/kata.bin]
#
# The key comes from the login Keychain (service kata-ota-signing, account {env}); if it
# isn't there it's fetched once from SSM (/kata/{env}/ota/signing-key) into the Keychain.
# Make it with infra/scripts/ota-signing-key.sh. It's written to a 0600 temp file only while
# signing, and never passed on a command line. The public half, ota-signing/{env}.pub.pem, is
# committed: an image is checked against it, so one signed by any other key is refused, and an
# image that's already signed needs no Keychain read at all.
set -euo pipefail
umask 077

here=$(cd "$(dirname "$0")" && pwd)
bin=${1:-$here/build/kata.bin}
bin="$(cd "$(dirname "$bin")" && pwd)/$(basename "$bin")"
env=${NPM_ENVIRONMENT:-sandbox}
service=kata-ota-signing
pub="$here/ota-signing/${env}.pub.pem"
[ -s "$pub" ] || { echo "no public key for $env ($pub): see infra/scripts/ota-signing-key.sh" >&2; exit 1; }

IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" > /dev/null

# A build with nothing to do leaves the last, signed image: check it rather than add a second block.
if espsecure.py verify_signature --version 2 --keyfile "$pub" "$bin" > /dev/null 2>&1; then
    echo "$bin is already signed ($env key)"
    exit 0
fi

if ! key_b64=$(security find-generic-password -s "$service" -a "$env" -w 2> /dev/null); then
    echo "signing key not in the Keychain: fetching it from SSM" >&2
    pem=$(AWS_PROFILE=${AWS_PROFILE:-nakom.is-$env} AWS_REGION=${AWS_REGION:-eu-west-2} \
        aws ssm get-parameter --name "/kata/${env}/ota/signing-key" --with-decryption \
        --query Parameter.Value --output text)
    key_b64=$(printf '%s\n' "$pem" | base64 | tr -d '\n')
    # Through security's stdin, not its argv (where ps would show it). Base64 needs no quoting.
    printf 'add-generic-password -s %s -a %s -T /usr/bin/security -j "%s" -w %s\n' "$service" "$env" \
        "Kata firmware signing key ($env); master copy in SSM /kata/${env}/ota/signing-key" \
        "$key_b64" | security -i
fi

dir=$(mktemp -d)
trap 'rm -P "$dir"/* 2>/dev/null; rmdir "$dir"' EXIT
printf '%s' "$key_b64" | base64 -d > "$dir/key.pem"
espsecure.py sign_data --version 2 --keyfile "$dir/key.pem" --output "$dir/signed.bin" "$bin" > /dev/null
# Against the committed public key: the private key used must be this environment's.
espsecure.py verify_signature --version 2 --keyfile "$pub" "$dir/signed.bin" > /dev/null || {
    echo "the Keychain's $service/$env key isn't the one in $pub: not signing" >&2
    exit 1
}
mv "$dir/signed.bin" "$bin"
chmod 644 "$bin"
echo "signed $bin ($env key)"
