#!/usr/bin/env node
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { DeviceStack } from '../lib/device-stack';
import { GithubCiStack } from '../lib/github-ci-stack';

const npmEnvironment = process.env.NPM_ENVIRONMENT;
if (npmEnvironment !== 'sandbox' && npmEnvironment !== 'prod') {
    throw new Error('NPM_ENVIRONMENT must be "sandbox" or "prod". Use `pnpm run deploy-sandbox`.');
}
const deployEnv = npmEnvironment;

// The account comes from the profile the deploy runs under, never a literal.
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION ?? 'eu-west-2' };

const app = new cdk.App();

new GithubCiStack(app, 'ClaudeChatsGithubCiStack', {
    env,
    deployEnv,
    description: `claude-chats GitHub Actions role: sign and publish Kata's firmware (${deployEnv})`,
});

// One IoT Thing per Kata board, listed per environment in infra/boards.json (as tinycrypt's
// keys.json). Committed, so every deploy declares every board: a board left out of a deploy would
// have its Thing deleted (and the deploy fails while its certificate is still attached).
const boards: string[] = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'boards.json'), 'utf8'))[deployEnv];
if (!Array.isArray(boards) || boards.some(b => !/^[a-z0-9]+$/.test(b)) || new Set(boards).size !== boards.length) {
    throw new Error(`infra/boards.json: "${deployEnv}" must be a list of distinct lower-case names like blue`);
}

new DeviceStack(app, 'ClaudeChatsKataDeviceStack', {
    env,
    deployEnv,
    boards,
    description: `Kata boards as IoT Things for Fleet, and their credentials for the embedding queue (${deployEnv})`,
});
