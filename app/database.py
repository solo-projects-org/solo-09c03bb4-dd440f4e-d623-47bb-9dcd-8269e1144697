"""SQLite connection management and schema definition.

FastAPI runs sync endpoints in a worker thread, while the lifespan runs in the
main thread, so connections are thread-local (all pointing at the same file).
WAL permits concurrent readers; writers serialize via ``BEGIN IMMEDIATE`` and
the busy timeout.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every meter reading submission.  Revisions of one source are kept forever
-- for audit; only the highest revision of each source_id is authoritative.
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT    NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision >= 1),
    device_id   TEXT    NOT NULL,
    ts          TEXT    NOT NULL,           -- UTC, ISO-8601
    meter_no    TEXT    NOT NULL,
    cumulative  REAL    NOT NULL CHECK (cumulative >= 0),
    content_hash TEXT   NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(source_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id);

-- Meter-change handover statements, same revision rules as readings.
CREATE TABLE IF NOT EXISTS handovers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT    NOT NULL,
    revision       INTEGER NOT NULL CHECK (revision >= 1),
    device_id      TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    old_meter_no   TEXT    NOT NULL,
    old_final      REAL    NOT NULL CHECK (old_final >= 0),
    new_meter_no   TEXT    NOT NULL,
    new_initial    REAL    NOT NULL CHECK (new_initial >= 0),
    content_hash   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(source_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_handover_device ON handovers(device_id);

-- Computed consumption intervals.
--   status = 'current' : the interval is eligible to supply data
--   status = 'stale'   : superseded after a late submission/correction or an
--                        interrupted rebuild; kept for audit, never served as
--                        current data
-- An undeterminable interval is still a valid 'current' row: energy_kwh is
-- NULL and reason explains why.  We never write 0 to mean "cannot decide".
CREATE TABLE IF NOT EXISTS intervals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        TEXT    NOT NULL,
    start_ts         TEXT    NOT NULL,
    end_ts           TEXT    NOT NULL,
    start_meter_no   TEXT    NOT NULL,
    end_meter_no     TEXT    NOT NULL,
    energy_kwh       REAL,                  -- NULL when undeterminable
    status           TEXT    NOT NULL CHECK (status IN ('current','stale')),
    reason           TEXT,                  -- set exactly when energy_kwh IS NULL
    -- provenance: the source ids / revisions of the readings bounding the interval
    start_source_id  TEXT    NOT NULL,
    start_revision   INTEGER NOT NULL,
    end_source_id    TEXT    NOT NULL,
    end_revision     INTEGER NOT NULL,
    -- handover used to settle a cross-meter interval (if any)
    handover_source_id TEXT,
    handover_revision  INTEGER,
    computed_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    superseded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_intervals_device_status
    ON intervals(device_id, status);
CREATE INDEX IF NOT EXISTS idx_intervals_status ON intervals(status);

-- Rebuild run audit trail.  'failed'/'interrupted' runs never delete stale
-- rows, and no run is ever started on service boot.
CREATE TABLE IF NOT EXISTS rebuild_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT,
    status      TEXT    NOT NULL CHECK (status IN ('running','success','failed','interrupted')),
    detail      TEXT,
    started_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);
"""


def _open_one(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class ConnectionPool:
    """One SQLite connection per thread, all backed by the same file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _open_one(self.db_path)
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def init_schema(self) -> None:
        init_db(self.get())

    def close(self) -> None:
        with self._lock:
            for conn in self._conns:
                conn.close()
            self._conns.clear()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    # A process restart must not resurrect anything: rebuilds left in the
    # 'running' state by a crash are marked interrupted, stale rows stay stale.
    conn.execute(
        "UPDATE rebuild_runs SET status = 'interrupted', "
        "detail = COALESCE(detail,'') || ' [marked on service restart]', "
        "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE status = 'running'"
    )
    conn.commit()
