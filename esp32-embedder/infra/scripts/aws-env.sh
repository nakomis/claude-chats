# shellcheck shell=bash disable=SC2154  # $env is set by the script that sources this
# Sourced, not run: the AWS account a Kata script talks to is decided by NPM_ENVIRONMENT alone.
#
#   . "$(dirname "$0")/aws-env.sh"     # with $env set to sandbox or prod
#   kata_aws_env                       # sets AWS_PROFILE and AWS_REGION
#   kata_check_account                 # before the first AWS call: aborts unless it's $env's account
#
# A pre-set AWS_PROFILE is deliberately ignored: inheriting one once put the prod OTA signing key
# into the sandbox account. To use a different profile for this environment, set KATA_AWS_PROFILE;
# the account check still applies to it, as it does to any AWS_ACCESS_KEY_ID in the environment.

kata_aws_env() {
    case "$env" in
        sandbox) KATA_AWS_ACCOUNT=975050268859 ;;
        prod) KATA_AWS_ACCOUNT=637423226886 ;;
        *) echo "NPM_ENVIRONMENT must be sandbox or prod" >&2; exit 2 ;;
    esac
    export AWS_PROFILE=${KATA_AWS_PROFILE:-nakom.is-$env}
    export AWS_REGION=${AWS_REGION:-eu-west-2}
}

kata_check_account() {
    local actual
    actual=$(aws sts get-caller-identity --query Account --output text) || {
        echo "can't tell which AWS account profile ${AWS_PROFILE} is (logged in?): stopping" >&2
        exit 1
    }
    if [ "$actual" != "$KATA_AWS_ACCOUNT" ]; then
        echo "refusing: NPM_ENVIRONMENT=${env} is account ${KATA_AWS_ACCOUNT}, but profile ${AWS_PROFILE} is ${actual}" >&2
        exit 1
    fi
    echo "AWS: ${env} account ${actual} (profile ${AWS_PROFILE})" >&2
}
