import http from 'node:http';
import { mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

// Controlled synthetic fixture. The table is intentionally NOT auto-created.
const databasePath = resolve(process.env.NOTES_DB_PATH ?? '.local/notes.sqlite');
mkdirSync(dirname(databasePath), { recursive: true });
const db = new DatabaseSync(databasePath);
const port = Number(process.env.PORT ?? 3000);
if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid PORT');

function json(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}
async function body(req) {
  let data = '';
  for await (const chunk of req) {
    data += chunk;
    if (Buffer.byteLength(data) > 8192) throw new Error('Request body too large');
  }
  return JSON.parse(data);
}
const server = http.createServer(async (req, res) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  try {
    if (req.method === 'GET' && path === '/health') {
      return json(res, 200, { status: 'ready' });
    }
    if (req.method === 'POST' && path === '/notes') {
      const input = await body(req);
      if (typeof input.message !== 'string' || !input.message.length || input.message.length > 1000) {
        return json(res, 400, { error: 'message must be 1-1000 characters' });
      }
      const result = db.prepare('INSERT INTO notes(message) VALUES (?)').run(input.message);
      return json(res, 201, { id: Number(result.lastInsertRowid), message: input.message });
    }
    const match = path.match(/^\/notes\/(\d+)$/);
    if (req.method === 'GET' && match) {
      const note = db.prepare('SELECT id, message FROM notes WHERE id = ?').get(Number(match[1]));
      return note ? json(res, 200, note) : json(res, 404, { error: 'not found' });
    }
    return json(res, 404, { error: 'not found' });
  } catch (error) {
    // Error text is intentional evidence for this controlled fixture, not a
    // recommended public-production error response policy.
    console.error(error.message);
    return json(res, 500, { error: error.message });
  }
});
server.listen(port, '0.0.0.0', () => {
  console.log(JSON.stringify({ event: 'ready', port: server.address().port }));
});
function close() { server.close(() => { db.close(); process.exit(0); }); }
process.on('SIGTERM', close);
process.on('SIGINT', close);
