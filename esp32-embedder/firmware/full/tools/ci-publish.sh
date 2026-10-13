#!/usr/bin/env bash
# CHATS-10: sign an unsigned image with one environment's key and publish it for Fleet, from CI.
# tinycrypt's ci-publish.sh, for this project's prefix and key.
#
#   esp32-embedder/firmware/full/tools/ci-publish.sh <sandbox|prod> <unsigned.bin> <x.y.z>
#
# A PR's build goes to sandbox; its merge sends the same unsigned bytes to prod, signed with prod's
# key (the two signed images differ only in the signature block). The CI job has already checked the
# build (version, configuration, per-board strings: tools/image-checks.sh); this signs, checks the
# signature against the committed public key, and uploads
# s3://nakomis-firmware-{env}/kata/<x.y.z>/firmware.bin with signed=true and its sha256.
#
# The signing key comes from SSM (/kata/{env}/ota/signing-key) through the job's OIDC role,
# into a private temporary file, never onto a command line or into the log. A version already
# published from the same unsigned bytes is fine (a re-run; RSA-PSS signatures are salted, so the
# signed bytes differ each time, hence the unsigned-sha256 metadata); from other bytes it's
# refused: a published version is never replaced.
#
# Needs AWS credentials (the CI role), esptool/espsecure and the AWS CLI.
set -euo pipefail
umask 077

usage="usage: ci-publish.sh <sandbox|prod> <unsigned.bin> <x.y.z>"
env=${1:?$usage}
unsigned=${2:?$usage}
version=${3:?$usage}
case "$env" in sandbox | prod) ;; *) echo "env must be sandbox or prod" >&2; exit 2 ;; esac
[[ "$version" =~ ^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$ ]] || { echo "version must be x.y.z" >&2; exit 2; }
here=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=image-checks.sh
. "$here/tools/image-checks.sh"
bucket="nakomis-firmware-${env}"
object="kata/${version}/firmware.bin"

check_image_version "$unsigned" "$version"
# The Wi-Fi SSID lives only in the sandbox account; the prod job publishes bytes the sandbox job
# already checked for it.
if [ "$env" = sandbox ]; then
    ssid=$(aws ssm get-parameter --name /Nakomis/Wifi/SSID --query Parameter.Value --output text)
    echo "::add-mask::${ssid}"
    check_image_strings "$unsigned" "$env" "$ssid"
else
    check_image_strings "$unsigned" "$env"
fi

dir=$(mktemp -d)
trap 'rm -f "$dir"/*; rmdir "$dir"' EXIT
aws ssm get-parameter --name "/kata/${env}/ota/signing-key" --with-decryption \
    --query Parameter.Value --output text > "$dir/key.pem"
espsecure.py sign_data --version 2 --keyfile "$dir/key.pem" --output "$dir/signed.bin" "$unsigned" > /dev/null
rm -f "$dir/key.pem"
check_image_signed "$dir/signed.bin" "$env"
sha=$(sha256sum "$dir/signed.bin" | cut -d' ' -f1)
unsigned_sha=$(sha256sum "$unsigned" | cut -d' ' -f1)

if existing=$(aws s3api head-object --bucket "$bucket" --key "$object" --query 'Metadata."unsigned-sha256"' --output text 2> /dev/null); then
    if [ "$existing" = "$unsigned_sha" ]; then
        echo "kata ${version} is already published to ${env} with these bytes"
        exit 0
    fi
    echo "s3://${bucket}/${object} already exists with other bytes: a published version is never replaced" >&2
    exit 1
fi
# --if-none-match: never replaced, even by a publish racing this one.
aws s3api put-object --bucket "$bucket" --key "$object" --body "$dir/signed.bin" --if-none-match '*' \
    --content-type application/octet-stream --metadata "signed=true,sha256=${sha},unsigned-sha256=${unsigned_sha}" > /dev/null
echo "published kata ${version} (sha256 ${sha}) to s3://${bucket}/${object}"
