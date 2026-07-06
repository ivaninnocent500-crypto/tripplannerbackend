const Database = require('better-sqlite3');
const path = require('path');

// SQLite file lives alongside the source. For production, mount a
// persistent volume at this path (Render/Railway/Fly.io all support
// persistent disks) so inquiries survive deploys/restarts.
const DB_PATH = process.env.DB_PATH || path.join(__dirname, '..', 'data.sqlite');

const db = new Database(DB_PATH);

db.pragma('journal_mode = WAL');

db.exec(`
  CREATE TABLE IF NOT EXISTS inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_booking_id INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    itinerary_title TEXT NOT NULL,
    total_price INTEGER NOT NULL,
    traveler_name TEXT NOT NULL,
    traveler_email TEXT NOT NULL,
    traveler_phone TEXT,
    travel_date TEXT,
    number_of_travelers INTEGER NOT NULL DEFAULT 1,
    special_requests TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE INDEX IF NOT EXISTS idx_inquiries_operator ON inquiries(operator_id);
  CREATE INDEX IF NOT EXISTS idx_inquiries_status ON inquiries(status);
  CREATE INDEX IF NOT EXISTS idx_inquiries_email ON inquiries(traveler_email);
`);

module.exports = db;
