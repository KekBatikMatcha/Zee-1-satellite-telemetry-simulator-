"""Rule-based telemetry anomaly detection (ground segment).

Why this exists
---------------
Monitoring a spacecraft means spotting when a telemetry point leaves its
expected envelope before it becomes a threat. This module implements a
transparent rule-based detector with configurable thresholds — deliberately
simple, since it must be explainable in a mission-control setting. A
statistical / ML detector is a documented future upgrade.

Severity mapping (dashboard + events)
-------------------------------------
Normal       -> NORMAL (no finding)
Borderline   -> LOW
Warning      -> MEDIUM
Alarm        -> HIGH
Emergency    -> CRITICAL
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

from telemetry.schema import TelemetryRecord

# A numeric severity used by the dashboard for coloring. 0 = normal.
LEVEL_NORMAL = 0
LEVEL_LOW = 1
LEVEL_MEDIUM = 2
LEVEL_HIGH = 3
LEVEL_CRITICAL = 4

SEVERITY_NAMES = {
    LEVEL_NORMAL: "NORMAL",
    LEVEL_LOW: "LOW",
    LEVEL_MEDIUM: "MEDIUM",
    LEVEL_HIGH: "HIGH",
    LEVEL_CRITICAL: "CRITICAL",
}


@dataclass
class Anomaly:
    rule: str
    severity_level: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self),
                "severity": SEVERITY_NAMES[self.severity_level]}


class RuleConfig:
    """Thresholds; overridable via config (see config.py)."""

    def __init__(self, config: Any) -> None:
        self.temperature_high = config.anomaly_temperature_high
        self.battery_low = config.anomaly_battery_low
        self.battery_critical = config.anomaly_battery_critical
        self.battery_drop_percent_per_min = config.anomaly_battery_drop_rate
        self.cpu_high = config.anomaly_cpu_high
        self.position_jump_factor = 10.0   # multiple of expected displacement


#: diameter-of-orbit sanity guard (km) for the position-jump rule.
MAX_POSITION_JUMP_KM = 500.0


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def detect_anomalies(
    record: TelemetryRecord,
    previous: TelemetryRecord | None,
    rule_config: RuleConfig,
) -> list[Anomaly]:
    """Run every rule against *record* (and *previous* for rate rules)."""
    findings: list[Anomaly] = []

    # Temperature.
    if record.temperature_c > rule_config.temperature_high:
        findings.append(Anomaly(
            "HIGH_TEMPERATURE", LEVEL_CRITICAL,
            f"temperature {record.temperature_c:.1f} C exceeds "
            f"{rule_config.temperature_high} C threshold"))

    # Battery low / critical.
    if record.battery_percentage <= rule_config.battery_critical:
        findings.append(Anomaly(
            "BATTERY_CRITICAL", LEVEL_CRITICAL,
            f"battery {record.battery_percentage:.1f}% is critical"))
    elif record.battery_percentage <= rule_config.battery_low:
        findings.append(Anomaly(
            "BATTERY_LOW", LEVEL_HIGH,
            f"battery {record.battery_percentage:.1f}% below "
            f"{rule_config.battery_low}% threshold"))

    # Battery discharge rate (needs previous sample).
    if previous is not None:
        dt_s = max(0.0, record.mission_time - previous.mission_time)
        if dt_s >= 1.0:
            drop = previous.battery_percentage - record.battery_percentage
            per_min = drop * (60.0 / dt_s)
            if per_min > rule_config.battery_drop_percent_per_min:
                findings.append(Anomaly(
                    "BATTERY_RAPID_DRAIN", LEVEL_HIGH,
                    f"battery draining {per_min:.1f}%/min (limit "
                    f"{rule_config.battery_drop_percent_per_min}%/min)"))

    # CPU.
    if record.cpu_usage > rule_config.cpu_high:
        findings.append(Anomaly(
            "CPU_OVERLOAD", LEVEL_HIGH,
            f"CPU {record.cpu_usage:.1f}% exceeds {rule_config.cpu_high}%"))

    # Position jump (sensor anomaly / spoofing indicator).
    if previous is not None:
        actual_km = _haversine_km(
            previous.latitude, previous.longitude,
            record.latitude, record.longitude)
        dt_s = max(0.0, record.mission_time - previous.mission_time)
        expected_km = record.velocity_kms * dt_s if record.velocity_kms > 0 else 0.0
        limit_km = min(MAX_POSITION_JUMP_KM,
                       max(50.0, expected_km * rule_config.position_jump_factor))
        if actual_km > limit_km:
            findings.append(Anomaly(
                "POSITION_JUMP", LEVEL_MEDIUM,
                f"position moved {actual_km:.0f} km in {dt_s:.0f} s, expected "
                f"~{expected_km:.0f} km"))

    return findings


def worst_severity(findings: list[Anomaly]) -> int:
    return max([f.severity_level for f in findings],
               default=LEVEL_NORMAL)