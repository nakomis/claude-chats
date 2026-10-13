# shellcheck shell=bash
# What a published firmware image must pass (CHATS-10), shared by tools/ci-publish.sh and the CI
# build job, as tinycrypt's image-checks.sh. Source it; each check prints why and exits on failure.
# Needs esptool and espsecure on PATH (ESP-IDF's export.sh, or pip).

# The image says this version.
check_image_version() { # bin version
    local built
    built=$(python -m esptool --chip esp32s3 image_info --version 2 "$1" 2> /dev/null | awk -F': *' '/^App version/ { print $2 }')
    [ "$built" = "$2" ] || { echo "the image says version '$built', not $2" >&2; exit 1; }
}

# The build's configuration (its sdkconfig.h) is one that may be published: a bad update can be
# rolled back, and the next one's signature is checked.
check_image_config() { # sdkconfig.h
    local option
    for option in CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT; do
        grep -q "^#define ${option} 1$" "$1" || { echo "the build lacks ${option}: not publishing" >&2; exit 1; }
    done
    # A rollback test build (CHATS-10) fails its health check on purpose: never from CI.
    if grep -q "^#define CONFIG_KATA_OTA_FAIL_SELFTEST 1$" "$1"; then
        echo "the build fails its health check on purpose (CONFIG_KATA_OTA_FAIL_SELFTEST): not publishing" >&2
        exit 1
    fi
}

# No secrets or per-board settings (they live in the prov partition, infra/scripts/provision.sh): no
# board or Thing name, no IoT endpoint, and none of the extra needles (the Wi-Fi SSID). Not a bare
# "PRIVATE KEY": mbedTLS's PEM parser has that string in every image.
check_image_strings() { # bin env [needle...]
    local bin=$1 env=$2 needle
    shift 2
    for needle in "kata-${env}-" "-ats.iot." "$@"; do
        [ -n "$needle" ] || continue
        if LC_ALL=C grep -qaF -- "$needle" "$bin"; then
            echo "the image contains a per-board string (\"${needle:0:12}...\"): not publishing" >&2
            exit 1
        fi
    done
}

# Signed by this environment's key, or every board in the field will refuse it (a board only
# accepts updates signed like the image it runs).
check_image_signed() { # bin env
    local pub
    pub="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ota-signing/$2.pub.pem"
    espsecure.py verify_signature --version 2 --keyfile "$pub" "$1" > /dev/null 2>&1 || {
        echo "the image isn't signed by the $2 key (ota-signing/$2.pub.pem): not publishing" >&2
        exit 1
    }
}
