import { mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
const path = resolve(process.env.NOTES_DB_PATH ?? '.local/notes.sqlite');
mkdirSync(dirname(path), { recursive: true });
const db = new DatabaseSync(path);
db.exec('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL)');
db.close();
console.log('Database schema initialized');
