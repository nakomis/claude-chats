import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface GithubCiStackProps extends cdk.StackProps {
    deployEnv: 'sandbox' | 'prod';
}

// GitHub's OIDC subject carries immutable ids (repo:OWNER@OWNER_ID/REPO@REPO_ID:CONTEXT) as well as
// the older name-only form; both are accepted, with the names pinned either way. The ids survive a
// rename, so whoever later claims the name can't inherit the trust.
const GITHUB_OWNER = 'nakomis';
const GITHUB_OWNER_ID = '1488244';
const GITHUB_REPO = 'claude-chats';
const GITHUB_REPO_ID = '1197869337';
const subjects = (context: string) => [
    `repo:${GITHUB_OWNER}@${GITHUB_OWNER_ID}/${GITHUB_REPO}@${GITHUB_REPO_ID}:${context}`,
    `repo:${GITHUB_OWNER}/${GITHUB_REPO}:${context}`,
];

// Kata, the ESP32-S3 embedder: its firmware prefix in the bucket, and its signing key.
const FIRMWARE_PROJECT = 'kata';

/**
 * CHATS-10: the role GitHub Actions assumes to publish Kata's firmware; tinycrypt's, for this
 * repository. A PR's build goes to sandbox; its merge sends the same build to prod, signed with
 * prod's key. Narrow on purpose: read this environment's signing key (and, in sandbox, the Wi-Fi
 * SSID an image must not contain), put and check firmware under kata/ in this environment's bucket,
 * and call the shared deployment tracker (which allow-lists the role in nakomis-infra). Nothing
 * else; no CDK deploys.
 *
 * Sandbox trusts any ref of this repository (PRs run as `pull_request`; forks get no OIDC token).
 * Prod trusts only `main`, so a branch can never sign with the prod key: only Martin merges to main.
 */
export class GithubCiStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: GithubCiStackProps) {
        super(scope, id, props);
        const { deployEnv } = props;

        const provider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(this, 'GithubOidc',
            `arn:${this.partition}:iam::${this.account}:oidc-provider/token.actions.githubusercontent.com`);
        const bucket = `nakomis-firmware-${deployEnv}`;
        const param = (name: string) => `arn:${this.partition}:ssm:${this.region}:${this.account}:parameter${name}`;

        const role = new iam.Role(this, 'CiRole', {
            roleName: `nakomis-${GITHUB_REPO}-github-ci-${deployEnv}`,
            description: `Assumed by ${GITHUB_REPO} GitHub Actions to publish Kata's firmware (${deployEnv})`,
            maxSessionDuration: cdk.Duration.hours(1),
            assumedBy: new iam.WebIdentityPrincipal(provider.openIdConnectProviderArn, {
                StringEquals: { 'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com' },
                StringLike: {
                    'token.actions.githubusercontent.com:sub': deployEnv === 'prod' ? subjects('ref:refs/heads/main') : subjects('*'),
                },
            }),
        });

        role.addToPolicy(new iam.PolicyStatement({
            actions: ['ssm:GetParameter'],
            resources: [
                param(`/${FIRMWARE_PROJECT}/${deployEnv}/ota/signing-key`),
                ...(deployEnv === 'sandbox' ? [param('/Nakomis/Wifi/SSID')] : []),
            ],
        }));
        // SecureStrings use the account's aws/ssm key: decrypt only through SSM.
        role.addToPolicy(new iam.PolicyStatement({
            actions: ['kms:Decrypt'],
            resources: ['*'],
            conditions: { StringEquals: { 'kms:ViaService': `ssm.${this.region}.amazonaws.com` } },
        }));
        role.addToPolicy(new iam.PolicyStatement({
            actions: ['s3:PutObject', 's3:GetObject'],
            resources: [`arn:${this.partition}:s3:::${bucket}/${FIRMWARE_PROJECT}/*`],
        }));
        // So a missing object answers 404, not 403 (unconditional: S3's implicit check carries no prefix).
        role.addToPolicy(new iam.PolicyStatement({
            actions: ['s3:ListBucket'],
            resources: [`arn:${this.partition}:s3:::${bucket}`],
        }));
        // The deployment tracker (nakomis-infra) lives in another account for sandbox; its resource
        // policy is what admits this role.
        role.addToPolicy(new iam.PolicyStatement({
            actions: ['execute-api:Invoke'],
            resources: [`arn:${this.partition}:execute-api:${this.region}:*:*/*/*/deployments/*`],
        }));

        new cdk.CfnOutput(this, 'CiRoleArn', { value: role.roleArn });
    }
}
