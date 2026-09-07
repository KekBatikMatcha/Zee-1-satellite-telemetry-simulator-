"""Telemetry schema: record structure, categories, and transmission priority.

Telemetry (downlink, spacecraft -> ground) carries a snapshot of spacecraft
health/state. Each record is a flat, versioned structure so the ground
decoder, database, and dashboard all agree on field names and types.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field, asdict
from typing import Any


class PacketType(enum.Enum):
    TELEMETRY = 1
    COMMAND = 2
    ACK = 3
    UNKNOWN = 255


class TelemetryCategory(enum.Enum):
    """Logical grouping of telemetry, mirroring spacecraft subsystems."""

    HOUSEKEEPING = 0
    POWER = 1
    THERMAL = 2
    ATTITUDE = 3
    POSITION = 4
    PAYLOAD = 5
    COMMUNICATION = 6
    SYSTEM = 7


class TelemetryPriority(enum.Enum):
    """Downlink scheduling priority (see docs/communication.md)."""

    CRITICAL = 0
    NORMAL = 1
    LOW = 2


@dataclass
class TelemetryRecord:
    """One decoded telemetry frame (the payload inside a packet)."""

    satellite_id: str
    mission_time: float            # simulated seconds since mission start
    timestamp: float               # wall-clock UNIX time at generation
    sequence_number: int
    packet_type: str = "TELEMETRY"
    category: str = "HOUSEKEEPING"  # primary category of the frame
    priority: str = "NORMAL"
    operating_mode: str = "BOOT"

    # POWER
    battery_percentage: float = 0.0
    battery_voltage: float = 0.0
    solar_power: float = 0.0
    power_consumption: float = 0.0

    # THERMAL
    temperature_c: float = 0.0

    # COMPUTING
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    storage_usage: float = 0.0

    # POSITION (simplified orbit)
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_km: float = 0.0
    velocity_kms: float = 0.0

    # ATTITUDE (degrees)
    attitude_roll: float = 0.0
    attitude_pitch: float = 0.0
    attitude_yaw: float = 0.0

    # COMMS / PAYLOAD
    communication_status: str = "IDLE"
    visibility: str = "NOT_VISIBLE"
    onboard_queue_count: int = 0
    payload_status: str = "STANDBY"

    # Optional: fault/anomaly annotations produced by ground post-processing.
    anomalies: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TelemetryRecord":
        """Rebuild a record from a decoded message/db row.

        Unknown keys (forward compatibility) are ignored; missing keys keep
        their defaults so a schema evolution does not break old rows.
        """
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


#: Human-readable names for the dashboard chart labels.
FIELD_LABELS: dict[str, str] = {
    "temperature_c": "Temperature (°C)",
    "battery_percentage": "Battery (%)",
    "solar_power": "Solar power (W)",
    "cpu_usage": "CPU (%)",
    "memory_usage": "Memory (%)",
    "storage_usage": "Storage (%)",
    "altitude_km": "Altitude (km)",
}


def now_unix() -> float:
    return time.time()


def telemetry_record_to_compact(record: TelemetryRecord) -> dict[str, Any]:
    return record.to_dict()
