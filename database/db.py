"""SQLite database bootstrap for the simulator.

Why SQLite first
----------------
Zero-configuration, single-file, transactionally safe, and expressive enough
for an educational single-instance deployment. A PostgreSQL migration is a
documented roadmap item; the store API is the seam that would absorb it.

Design notes
------------
* WAL journaling avoids read/write contention between the live simulation
  writer and the dashboard reader.
* Foreign keys and sensible indexes for the hot paths (sequence numbers,
  timestamps, event ordering).
* Schema is versioned via ``PRAGMA user_version`` so future migrations are
  additive and safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    satellite_id        TEXT    NOT NULL,
    mission_time        REAL    NOT NULL,
    timestamp           REAL    NOT NULL,
    sequence_number     INTEGER NOT NULL,
    category            TEXT,
    priority            TEXT,
    operating_mode      TEXT,
    temperature_c       REAL,
    battery_percentage  REAL,
    battery_voltage     REAL,
    solar_power         REAL,
    power_consumption   REAL,
    cpu_usage           REAL,
    memory_usage        REAL,
    storage_usage       REAL,
    latitude            REAL,
    longitude           REAL,
    altitude_km         REAL,
    velocity_kms        REAL,
    attitude_roll       REAL,
    attitude_pitch      REAL,
    attitude_yaw        REAL,
    communication_status TEXT,
    payload_status      TEXT,
    visibility          TEXT,
    onboard_queue_count INTEGER,
    anomalies           TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_sat_seq
    ON telemetry (satellite_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_telemetry_time
    ON telemetry (timestamp);

CREATE TABLE IF NOT EXISTS packets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_number INTEGER NOT NULL,
    packet_type     TEXT    NOT NULL,
    received_at     REAL    NOT NULL,
    payload_size    INTEGER NOT NULL,
    crc_valid       INTEGER NOT NULL,
    status          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_seq ON packets (sequence_number);
CREATE INDEX IF NOT EXISTS idx_packets_status ON packets (status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL    NOT NULL,
    sim_time   REAL    NOT NULL,
    event_type TEXT    NOT NULL,
    severity   TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    message    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);

CREATE TABLE IF NOT EXISTS security_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL    NOT NULL,
    sim_time   REAL    NOT NULL,
    event_type TEXT    NOT NULL,
    severity   TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    message    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sec_events_time ON security_events (timestamp);

CREATE TABLE IF NOT EXISTS satellite_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT    NOT NULL,          -- JSON snapshot
    updated_at REAL    NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection with WAL + FK + row factory."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and stamp the schema version."""
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    conn.commit()


def wipe_db(conn: sqlite3.Connection) -> None:
    """Drop all tables (used by reset tests / /api/simulation/reset)."""
    for table in ("telemetry", "packets", "events", "security_events",
                  "satellite_state"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
