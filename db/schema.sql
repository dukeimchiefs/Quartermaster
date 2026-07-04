-- Resident Scheduling Assistant — schema
-- SQLite-flavored, written to be portable to Postgres (see db/models.py).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS residents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    pgy                 INTEGER NOT NULL CHECK (pgy IN (1, 2, 3, 4)),
    start_date          DATE NOT NULL,
    end_date            DATE,
    contact             TEXT,
    board_eligibility   BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rotations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    location            TEXT,
    intern_capacity     INTEGER NOT NULL DEFAULT 0,
    senior_capacity     INTEGER NOT NULL DEFAULT 0,
    requires_pgy        INTEGER
);

CREATE TABLE IF NOT EXISTS blocks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    year                INTEGER NOT NULL,
    block_number        INTEGER NOT NULL,
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    UNIQUE (year, block_number)
);

CREATE TABLE IF NOT EXISTS assignments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id         INTEGER NOT NULL REFERENCES residents (id),
    block_id            INTEGER NOT NULL REFERENCES blocks (id),
    rotation_id         INTEGER NOT NULL REFERENCES rotations (id),
    role                TEXT NOT NULL CHECK (role IN ('intern', 'senior')),
    UNIQUE (resident_id, block_id)
);

CREATE TABLE IF NOT EXISTS time_off (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id         INTEGER NOT NULL REFERENCES residents (id),
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    type                TEXT NOT NULL,
    approved            BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS call_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id         INTEGER NOT NULL REFERENCES residents (id),
    date                DATE NOT NULL,
    shift_type          TEXT NOT NULL,
    hours               REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS swaps (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    original_assignment_id  INTEGER NOT NULL REFERENCES assignments (id),
    new_assignment_id       INTEGER REFERENCES assignments (id),
    reason                  TEXT,
    approved_by             TEXT,
    timestamp               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    definition          TEXT NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor               TEXT NOT NULL,
    action              TEXT NOT NULL,
    reason              TEXT,
    details             TEXT
);

CREATE INDEX IF NOT EXISTS idx_assignments_resident ON assignments (resident_id);
CREATE INDEX IF NOT EXISTS idx_assignments_block ON assignments (block_id);
CREATE INDEX IF NOT EXISTS idx_assignments_rotation ON assignments (rotation_id);
CREATE INDEX IF NOT EXISTS idx_time_off_resident ON time_off (resident_id);
CREATE INDEX IF NOT EXISTS idx_call_history_resident ON call_history (resident_id);
CREATE INDEX IF NOT EXISTS idx_swaps_original ON swaps (original_assignment_id);
CREATE INDEX IF NOT EXISTS idx_swaps_new ON swaps (new_assignment_id);
