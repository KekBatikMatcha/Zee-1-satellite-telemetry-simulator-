"""Data-access layer (repository) over the SQLite schema.

Keeps SQL out of the API and simulation code. All persistence the simulator
needs goes through this single module, so future storage backends can be
swapped without touching spacecraft / ground station / API code.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from telemetry.schema import TelemetryRecord
from simulation.events import MissionEvent

#: Event types that are recorded into the security_events table too.
SECURITY_EVENT_TYPES = frozenset({
    "AUTHENTICATION_FAILURE",
    "INVALID_COMMAND",
    "REPLAY_ATTACK_DETECTED",
    "INVALID_PACKET",
    "CRC_FAILURE",
    "UNAUTHORIZED_COMMAND",
    "INVALID_MODE_TRANSITION",
    "TELECOMMAND_REJECTED",
    "TELECOMMAND_ACCEPTED",
    "SECURITY_ALERT",
})


class Store:
    """Repository over :mod:`database.db` tables."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def insert_telemetry(self, record: TelemetryRecord) -> int:
        anomalies_json = json.dumps(record.anomalies)
        cur = self.conn.execute(
            """INSERT INTO telemetry (
                satellite_id, mission_time, timestamp, sequence_number,
                category, priority, operating_mode, temperature_c,
                battery_percentage, battery_voltage, solar_power,
                power_consumption, cpu_usage, memory_usage, storage_usage,
                latitude, longitude, altitude_km, velocity_kms,
                attitude_roll, attitude_pitch, attitude_yaw,
                communication_status, payload_status, visibility,
                onboard_queue_count, anomalies)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.satellite_id, record.mission_time, record.timestamp,
                record.sequence_number, record.category, record.priority,
                record.operating_mode, record.temperature_c,
                record.battery_percentage, record.battery_voltage,
                record.solar_power, record.power_consumption,
                record.cpu_usage, record.memory_usage, record.storage_usage,
                record.latitude, record.longitude, record.altitude_km,
                record.velocity_kms, record.attitude_roll,
                record.attitude_pitch, record.attitude_yaw,
                record.communication_status, record.payload_status,
                record.visibility, record.onboard_queue_count,
                anomalies_json,
            ))
        self.conn.commit()
        return cur.lastrowid

    def latest_telemetry(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM telemetry ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def telemetry_history(self, limit: int = 200,
                          since: float | None = None) -> list[dict[str, Any]]:
        rows = []
        if since is not None:
            rows = self.conn.execute(
                "SELECT * FROM telemetry WHERE mission_time >= ? "
                "ORDER BY mission_time ASC LIMIT ?", (since, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM telemetry ORDER BY mission_time ASC LIMIT ?",
                (limit,)).fetchall()
        return [self._decode_anomalies(dict(r)) for r in rows]

    @staticmethod
    def _decode_anomalies(item: dict[str, Any]) -> dict[str, Any]:
        try:
            item["anomalies"] = json.loads(item["anomalies"] or "[]")
        except (TypeError, json.JSONDecodeError):
            item["anomalies"] = []
        return item

    def telemetry_field_series(self, field: str, limit: int = 500) -> list[dict]:
        allowed = {
            "temperature_c", "battery_percentage", "solar_power", "cpu_usage",
            "memory_usage", "storage_usage", "altitude_km", "battery_voltage",
        }
        if field not in allowed:
            raise ValueError(f"unsupported field '{field}'")
        rows = self.conn.execute(
            f"SELECT timestamp, mission_time, {field} FROM telemetry "
            "ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        return [{"timestamp": r[0], "mission_time": r[1], "value": r[2]}
                for r in rows]

    # ------------------------------------------------------------------
    # Packets
    # ------------------------------------------------------------------
    def insert_packet(self, sequence_number: int, packet_type: str,
                      received_at: float, payload_size: int,
                      crc_valid: bool, status: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO packets (sequence_number, packet_type, received_at, "
            "payload_size, crc_valid, status) VALUES (?,?,?,?,?,?)",
            (sequence_number, packet_type, received_at, payload_size,
             int(crc_valid), status))
        self.conn.commit()
        return cur.lastrowid

    def packet_statistics(self) -> dict[str, Any]:
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM packets").fetchone()["n"]
        by_status: dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM packets "
                "GROUP BY status"):
            by_status[row["status"]] = row["n"]
        return {
            "total": total,
            "by_status": by_status,
            "crc_failures": self.conn.execute(
                "SELECT COUNT(*) FROM packets WHERE status = 'CRC_FAILURE'"
            ).fetchone()[0],
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def insert_event(self, event: MissionEvent) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (timestamp, sim_time, event_type, severity, "
            "source, message) VALUES (?,?,?,?,?,?)",
            (event.timestamp, event.sim_time, event.event_type,
             event.severity, event.source, event.message))
        self.conn.commit()
        return cur.lastrowid

    def insert_security_event(self, event: MissionEvent) -> int:
        cur = self.conn.execute(
            "INSERT INTO security_events (timestamp, sim_time, event_type, "
            "severity, source, message) VALUES (?,?,?,?,?,?)",
            (event.timestamp, event.sim_time, event.event_type,
             event.severity, event.source, event.message))
        self.conn.commit()
        return cur.lastrowid

    def on_event(self, event: MissionEvent) -> None:
        """EventBus sink: persist to events, mirror security-relevant ones."""
        try:
            self.insert_event(event)
            if event.event_type in SECURITY_EVENT_TYPES or \
                    event.source == "SECURITY":
                self.insert_security_event(event)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("database").exception("event persistence failed")

    def events_history(self, limit: int = 100,
                       kind: str = "events") -> list[dict[str, Any]]:
        table = "security_events" if kind == "security" else "events"
        rows = self.conn.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Satellite state
    # ------------------------------------------------------------------
    def upsert_satellite_state(self, snapshot: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO satellite_state (id, payload, updated_at) VALUES "
            "(1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (json.dumps(snapshot), time.time()))
        self.conn.commit()

    def latest_satellite_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM satellite_state WHERE id = 1").fetchone()
        return json.loads(row["payload"]) if row else None

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def reset(self) -> None:
        for table in ("telemetry", "packets", "events", "security_events",
                      "satellite_state"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
