#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { GithubCiStack } from '../lib/github-ci-stack';

const npmEnvironment = process.env.NPM_ENVIRONMENT;
if (npmEnvironment !== 'sandbox' && npmEnvironment !== 'prod') {
    throw new Error('NPM_ENVIRONMENT must be "sandbox" or "prod". Use `pnpm run deploy-sandbox`.');
}
const deployEnv = npmEnvironment;

// The account comes from the profile the deploy runs under, never a literal.
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION ?? 'eu-west-2' };

const app = new cdk.App();

// Device identity (Kata's IoT Things, CHATS-10 part 2) joins this app later.
new GithubCiStack(app, 'ClaudeChatsGithubCiStack', {
    env,
    deployEnv,
    description: `claude-chats GitHub Actions role: sign and publish Kata's firmware (${deployEnv})`,
});
