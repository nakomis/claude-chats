import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class ClaudeChatsBackupStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const bucket = new s3.Bucket(this, 'BackupBucket', {
      bucketName:       `claude-chats-backups-${this.account}`,
      removalPolicy:    cdk.RemovalPolicy.RETAIN,
      encryption:       s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // Lambda keeps the 10 most recent backups — deletes everything beyond that.
    const retentionFn = new lambda.Function(this, 'RetentionLambda', {
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(1),
      environment: { BUCKET_NAME: bucket.bucketName },
      code: lambda.Code.fromInline(`
const { S3Client, ListObjectsV2Command, DeleteObjectsCommand } = require('@aws-sdk/client-s3');
const s3 = new S3Client();
const KEEP = 10;

exports.handler = async () => {
  const list = await s3.send(new ListObjectsV2Command({ Bucket: process.env.BUCKET_NAME }));
  const objects = (list.Contents ?? [])
    .sort((a, b) => (b.LastModified?.getTime() ?? 0) - (a.LastModified?.getTime() ?? 0));
  const toDelete = objects.slice(KEEP);
  if (!toDelete.length) {
    console.log(\`Nothing to delete — \${objects.length} backup(s) present\`);
    return;
  }
  await s3.send(new DeleteObjectsCommand({
    Bucket: process.env.BUCKET_NAME,
    Delete: { Objects: toDelete.map(o => ({ Key: o.Key })) },
  }));
  console.log(\`Kept \${Math.min(objects.length, KEEP)}, deleted \${toDelete.length} backup(s)\`);
};
      `),
    });

    bucket.grantRead(retentionFn);
    retentionFn.addToRolePolicy(new iam.PolicyStatement({
      actions:   ['s3:DeleteObject'],
      resources: [bucket.arnForObjects('*')],
    }));

    new events.Rule(this, 'DailyRetentionRule', {
      schedule: events.Schedule.cron({ minute: '0', hour: '2' }),
      targets:  [new targets.LambdaFunction(retentionFn)],
    });

    new cdk.CfnOutput(this, 'BucketName', {
      value:       bucket.bucketName,
      description: 'S3 bucket for daily postgres backups',
      exportName:  'ClaudeChatsBackupBucket',
    });
  }
}
