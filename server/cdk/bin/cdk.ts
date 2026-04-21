#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { ClaudeChatsBackupStack } from '../lib/claude-chats-backup-stack';

const app = new cdk.App();
new ClaudeChatsBackupStack(app, 'ClaudeChatsBackup');
