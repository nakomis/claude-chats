import * as stompit from 'stompit';
import { Pool } from 'pg';
import axios from 'axios';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const DB_URL       = process.env.CLAUDE_CHATS_DB_URL    ?? 'postgresql://claude:claude@localhost:5433/claude_chats';
const QUEUE_URL    = process.env.CLAUDE_CHATS_QUEUE_URL  ?? 'stomp://localhost:61613';
const QUEUE_NAME   = process.env.CLAUDE_CHATS_QUEUE_NAME ?? 'claude-chats-messages';
const QUEUE_USER   = process.env.CLAUDE_CHATS_QUEUE_USER ?? 'admin';
const QUEUE_PASS   = process.env.CLAUDE_CHATS_QUEUE_PASS ?? 'admin';
const OLLAMA_URL   = process.env.OLLAMA_BASE_URL         ?? 'http://localhost:11434';
const MODEL        = process.env.CLAUDE_CHATS_MODEL      ?? 'mxbai-embed-large';
const TEXT_LIMIT   = 800; // match Python hook truncation for mxbai-embed-large

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface QueueMessage {
  session_id:   string;
  project_path: string;
  git_branch:   string;
  started_at:   string;
  name?:        string | null;
  messages: {
    message_uuid: string;
    role:         string;
    content:      string;
    created_at:   string;
    sequence_num: number;
  }[];
}

// ---------------------------------------------------------------------------
// DB pool
// ---------------------------------------------------------------------------

const pool = new Pool({ connectionString: DB_URL });

// ---------------------------------------------------------------------------
// Embedding
// ---------------------------------------------------------------------------

async function getEmbedding(text: string): Promise<number[] | null> {
  try {
    const { data } = await axios.post(`${OLLAMA_URL}/api/embeddings`, {
      model: MODEL,
      prompt: text.slice(0, TEXT_LIMIT),
    });
    return data.embedding ?? null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Message processing
// ---------------------------------------------------------------------------

async function processPayload(payload: QueueMessage): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    await client.query(
      `INSERT INTO conversations (session_id, project_path, git_branch, started_at)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (session_id) DO NOTHING`,
      [payload.session_id, payload.project_path, payload.git_branch, payload.started_at],
    );

    if (payload.name) {
      await client.query(
        'UPDATE conversations SET name = $1 WHERE session_id = $2',
        [payload.name, payload.session_id],
      );
    }

    const { rows } = await client.query(
      'SELECT id FROM conversations WHERE session_id = $1',
      [payload.session_id],
    );
    const convId: string = rows[0].id;

    for (const msg of payload.messages) {
      if (!msg.content) continue;

      const existing = await client.query(
        'SELECT id FROM messages WHERE message_uuid = $1',
        [msg.message_uuid],
      );
      if (existing.rows.length > 0) continue;

      const embedding = await getEmbedding(msg.content);

      if (embedding) {
        const vec = '[' + embedding.join(',') + ']';
        await client.query(
          `INSERT INTO messages
             (conversation_id, message_uuid, role, content, embedding, created_at, sequence_num)
           VALUES ($1, $2, $3, $4, $5::vector, $6, $7)
           ON CONFLICT (message_uuid) DO NOTHING`,
          [convId, msg.message_uuid, msg.role, msg.content, vec, msg.created_at, msg.sequence_num],
        );
      } else {
        await client.query(
          `INSERT INTO messages
             (conversation_id, message_uuid, role, content, created_at, sequence_num)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (message_uuid) DO NOTHING`,
          [convId, msg.message_uuid, msg.role, msg.content, msg.created_at, msg.sequence_num],
        );
      }
    }

    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

// ---------------------------------------------------------------------------
// STOMP consumer with reconnect
// ---------------------------------------------------------------------------

function parseStompUrl(url: string): { host: string; port: number } {
  const stripped = url.replace(/^stomp:\/\//, '');
  const [host, portStr] = stripped.split(':');
  return { host: host ?? 'localhost', port: parseInt(portStr ?? '61613', 10) };
}

function connect(): void {
  const { host, port } = parseStompUrl(QUEUE_URL);

  const servers: stompit.ConnectFailover.Server[] = [{
    host,
    port,
    connectHeaders: {
      host:         '/',
      login:        QUEUE_USER,
      passcode:     QUEUE_PASS,
      'heart-beat': '5000,5000',
    },
  }];

  const manager = new stompit.ConnectFailover(servers, {
    initialDelay:  1_000,
    maxDelay:      30_000,
    maxReconnects: -1,
  });

  manager.connect((err, client, reconnect) => {
    if (err) {
      console.error('STOMP connection error:', err.message);
      return;
    }

    console.log(`Connected to ActiveMQ at ${host}:${port}`);

    client.on('error', (e: Error) => {
      console.error('STOMP client error:', e.message);
      reconnect();
    });

    client.subscribe(
      { destination: `/queue/${QUEUE_NAME}`, ack: 'client-individual' },
      (subErr, message) => {
        if (subErr) {
          console.error('Subscribe error:', subErr.message);
          reconnect();
          return;
        }

        message.readString('utf-8', async (readErr, body) => {
          if (readErr || !body) {
            client.nack(message);
            return;
          }

          try {
            const payload: QueueMessage = JSON.parse(body);
            await processPayload(payload);
            client.ack(message);
            console.log(
              `[${new Date().toISOString()}] Processed session ${payload.session_id}` +
              ` — ${payload.messages.length} message(s)`,
            );
          } catch (procErr) {
            console.error('Processing error:', procErr);
            client.nack(message);
          }
        });
      },
    );
  });
}

console.log(`claude-chats worker starting (queue: ${QUEUE_URL}, db: ${DB_URL})`);
connect();
