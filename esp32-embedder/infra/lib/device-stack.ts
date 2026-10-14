import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

export interface DeviceStackProps extends cdk.StackProps {
    deployEnv: 'sandbox' | 'prod';
    /** One IoT Thing per board, e.g. ['blue', 'green', 'red'] (infra/boards.json). */
    boards: string[];
}

// Fleet's name for Kata: the Thing attribute, the topic prefix and the firmware bucket prefix.
const PROJECT = 'kata';

/**
 * CHATS-10: each Kata board is an IoT Thing, kata-{env}-{board}, that may connect only as itself,
 * take Fleet's commands on kata/{thing}/commands, and answer on kata/{thing}/responses and
 * kata/{thing}/status (its own topics only). tinycrypt's key-stack, for Kata.
 *
 * Its attributes tell Fleet to offer signed builds only (ota-signed). No ota-approval: Kata has
 * nobody to approve an update; its own checks (signature, SHA-256, version, self-test) decide.
 *
 * Certificates are made and attached by infra/scripts/provision.sh, not here: the private key
 * must never pass through CloudFormation.
 *
 * For CHATS-5, the same certificate also buys AWS credentials: the IoT credentials provider
 * (credentials.iot.amazonaws.com) exchanges it for the WorkerRole through the role alias
 * kata-{env}, which the device policy lets a board assume. The role can take jobs from
 * conversation-memory's ingest queue and hand them on through its results queue, and nothing else.
 * Both queues live in home-infra's ConversationMemoryIngestStack, in the same account as the
 * Things (prod boards consume prod; sandbox boards are for testing against sandbox), and are found
 * through the SSM parameters it publishes, so that stack must be deployed first (HOME-439).
 *
 * CHATS-13: Kata no longer holds MQTT. With the model's buffers, internal RAM can't hold an MQTT
 * session beside the SQS one, and stopping and restarting MQTT round every SQS phase fragmented it.
 * So Fleet's commands reach a board through SQS: an IoT rule forwards whatever is published on
 * kata/{thing}/commands into that board's queue, {thing}-commands, which the board reads in each SQS
 * phase. Its responses and status heartbeat go out as HTTPS publishes (SigV4, service iotdata) to its
 * own topics, with the WorkerRole credentials it already has, so Fleet and BD-1 see the same topics
 * as before. The MQTT grants in the board policy stay until no board runs firmware that uses them.
 */
export class DeviceStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: DeviceStackProps) {
        super(scope, id, props);
        const { deployEnv, boards } = props;

        const arn = (resource: string) => `arn:${this.partition}:iot:${this.region}:${this.account}:${resource}`;
        const own = (channel: string) => `${PROJECT}/\${iot:Connection.Thing.ThingName}/${channel}`;

        // ---- CHATS-5: credentials for the embedding queue, through the IoT credentials provider ----
        const workerRole = new iam.Role(this, 'WorkerRole', {
            roleName: `${PROJECT}-worker-${deployEnv}`,
            description: `Assumed by Kata boards (${deployEnv}) through the IoT credentials provider, for the embedding queue`,
            assumedBy: new iam.ServicePrincipal('credentials.iot.amazonaws.com'),
            maxSessionDuration: cdk.Duration.hours(1),
        });
        const roleAlias = new iot.CfnRoleAlias(this, 'WorkerRoleAlias', {
            roleAlias: `${PROJECT}-${deployEnv}`,
            roleArn: workerRole.roleArn,
            credentialDurationSeconds: 3600,
        });

        // CHATS-5's custody hand-off: Kata takes a job from the ingest queue, embeds it, sends the
        // result (the ingest body verbatim, plus its vector) to the results queue, and only then
        // deletes the job. Cal stores the result and deletes it from there. The ARNs come from
        // home-infra's SSM parameters, resolved at deploy time: no CloudFormation export crosses
        // the two repos, and the deploy fails clearly if home-infra hasn't published them yet.
        const convmem = `/conversation-memory/${deployEnv}`;
        const ingestQueueArn = ssm.StringParameter.valueForStringParameter(this, `${convmem}/queue-arn`);
        const resultsQueueArn = ssm.StringParameter.valueForStringParameter(this, `${convmem}/results-queue-arn`);
        workerRole.addToPolicy(new iam.PolicyStatement({
            sid: 'TakeJobs',
            // Receive with a 15-minute visibility, extend it while a job waits in the pool, delete it
            // once its result is accepted, or hand it back (visibility 0) when an update arrives
            // mid-pass (CHATS-10's acceptance criterion 4). The batch calls need the same actions.
            actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:ChangeMessageVisibility'],
            resources: [ingestQueueArn],
        }));
        workerRole.addToPolicy(new iam.PolicyStatement({
            sid: 'HandOffResults',
            // SendMessage covers SendMessageBatch. Send only: Kata can't read results back.
            actions: ['sqs:SendMessage'],
            resources: [resultsQueueArn],
        }));
        // CHATS-13: each board publishes its own responses and status over HTTPS, and reads its own
        // commands queue. credentials-iot:ThingName is the Thing the credentials provider issued
        // this session to (the board's certificate's Thing), so a board can't pose as another.
        const thing = '${credentials-iot:ThingName}';
        workerRole.addToPolicy(new iam.PolicyStatement({
            sid: 'PublishOwnTopics',
            actions: ['iot:Publish'],
            resources: [arn(`topic/${PROJECT}/${thing}/responses`), arn(`topic/${PROJECT}/${thing}/status`)],
        }));
        workerRole.addToPolicy(new iam.PolicyStatement({
            sid: 'RetainOwnStatus',
            // The heartbeat is retained, for BD-1's GetRetainedMessage (HOME-441).
            actions: ['iot:RetainPublish'],
            resources: [arn(`topic/${PROJECT}/${thing}/status`)],
        }));
        workerRole.addToPolicy(new iam.PolicyStatement({
            sid: 'TakeOwnCommands',
            actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage'],
            resources: [`arn:${this.partition}:sqs:${this.region}:${this.account}:${thing}-commands`],
        }));
        // Deliberately nothing else: no S3 (claim-check jobs are handed to Cal unembedded), no
        // GetQueueAttributes, no DLQs, and no KMS (both queues use SQS-managed encryption).

        // ---- the boards ----
        // Policy variables pin each certificate to its own Thing: the client id must be the Thing's
        // name, and the certificate must be attached to that Thing.
        const policy = new iot.CfnPolicy(this, 'BoardPolicy', {
            policyName: `${PROJECT}-board-${deployEnv}`,
            policyDocument: {
                Version: '2012-10-17',
                Statement: [
                    {
                        Effect: 'Allow',
                        Action: 'iot:Connect',
                        Resource: arn('client/${iot:Connection.Thing.ThingName}'),
                        Condition: { Bool: { 'iot:Connection.Thing.IsAttached': 'true' } },
                    },
                    {
                        Effect: 'Allow',
                        Action: 'iot:Publish',
                        // Its own topics only, so a board can't pose as another.
                        Resource: [arn(`topic/${own('responses')}`), arn(`topic/${own('status')}`)],
                    },
                    {
                        // CHATS-5 / HOME-441: the status heartbeat is retained, so BD-1 can read a
                        // board's last status (GetRetainedMessage) between heartbeats. Without this,
                        // IoT Core drops the connection of a client that publishes with retain.
                        Effect: 'Allow',
                        Action: 'iot:RetainPublish',
                        Resource: arn(`topic/${own('status')}`),
                    },
                    {
                        Effect: 'Allow',
                        Action: 'iot:Subscribe',
                        Resource: arn(`topicfilter/${own('commands')}`),
                    },
                    {
                        Effect: 'Allow',
                        Action: 'iot:Receive',
                        Resource: arn(`topic/${own('commands')}`),
                    },
                    {
                        // CHATS-5: the certificate exchanged for WorkerRole credentials.
                        Effect: 'Allow',
                        Action: 'iot:AssumeRoleWithCertificate',
                        Resource: roleAlias.attrRoleAliasArn,
                    },
                ],
            },
        });

        // ---- CHATS-13: Fleet's commands, into each board's SQS queue ----
        // A command waits for the board's next SQS phase (seconds when idle, up to a pass when
        // busy), so an hour is plenty; an update's presigned URL lasts ten minutes anyway.
        const commandQueues: sqs.Queue[] = [];
        for (const board of boards) {
            commandQueues.push(new sqs.Queue(this, `Commands-${board}`, {
                queueName: `${PROJECT}-${deployEnv}-${board}-commands`,
                retentionPeriod: cdk.Duration.hours(1),
                encryption: sqs.QueueEncryption.SQS_MANAGED,
                enforceSSL: true,
            }));
        }
        const ruleRole = new iam.Role(this, 'CommandsRuleRole', {
            assumedBy: new iam.ServicePrincipal('iot.amazonaws.com'),
            description: `Lets the ${PROJECT}-${deployEnv} commands rule put Fleet's commands on each board's queue`,
        });
        for (const q of commandQueues) q.grantSendMessages(ruleRole);
        new iot.CfnTopicRule(this, 'CommandsRule', {
            ruleName: `${PROJECT}_${deployEnv}_commands`,
            topicRulePayload: {
                description: `CHATS-13: Fleet's commands for ${PROJECT}-${deployEnv} boards, onto each board's SQS queue`,
                // Only this environment's boards: a queue exists for each of them.
                sql: `SELECT * FROM '${PROJECT}/+/commands' WHERE startswith(topic(2), '${PROJECT}-${deployEnv}-')`,
                awsIotSqlVersion: '2016-03-23',
                actions: [{
                    sqs: {
                        roleArn: ruleRole.roleArn,
                        // The substitution picks the board's own queue (tested in the CHATS-13 spike).
                        queueUrl: `https://sqs.${this.region}.amazonaws.com/${this.account}/\${topic(2)}-commands`,
                        useBase64: false,
                    },
                }],
            },
        });

        for (const board of boards) {
            const thingName = `${PROJECT}-${deployEnv}-${board}`;
            new iot.CfnThing(this, `Thing-${board}`, {
                thingName,
                // Read by Fleet (nakomis-fleet): ota-signed makes it offer only signed builds.
                attributePayload: {
                    attributes: { project: PROJECT, deviceType: 'embedder', capabilities: 'ota,ota-signed' },
                },
            });
            new cdk.CfnOutput(this, `ThingName-${board}`, { value: thingName });
        }
        new cdk.CfnOutput(this, 'BoardPolicyName', { value: policy.policyName! });
        new cdk.CfnOutput(this, 'WorkerRoleAliasName', { value: roleAlias.roleAlias! });
    }
}
