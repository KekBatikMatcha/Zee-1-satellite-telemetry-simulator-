"""Structured mission events shared across the simulator.

Why this exists
---------------
A mission needs an audit trail: mode changes, ground contacts, packet losses,
faults, and security events all belong in one event model so the dashboard
and database treat them uniformly. Logs (files) and events (database rows)
are different things: logs are for operators debugging the simulator, events
are part of the simulated mission record.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Callable


class Severity(str, enum.Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


class EventType(str, enum.Enum):
    SYSTEM_BOOT = "SYSTEM_BOOT"
    SYSTEM_INITIALIZED = "SYSTEM_INITIALIZED"
    MODE_CHANGED = "MODE_CHANGED"
    LOW_BATTERY = "LOW_BATTERY"
    HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
    GROUND_CONTACT_STARTED = "GROUND_CONTACT_STARTED"
    GROUND_CONTACT_LOST = "GROUND_CONTACT_LOST"
    PACKET_LOST = "PACKET_LOST"
    CRC_FAILURE = "CRC_FAILURE"
    PACKET_MISSING = "PACKET_MISSING"
    PACKET_REJECTED = "PACKET_REJECTED"
    TELECOMMAND_RECEIVED = "TELECOMMAND_RECEIVED"
    TELECOMMAND_ACCEPTED = "TELECOMMAND_ACCEPTED"
    TELECOMMAND_REJECTED = "TELECOMMAND_REJECTED"
    SECURITY_ALERT = "SECURITY_ALERT"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    INVALID_COMMAND = "INVALID_COMMAND"
    REPLAY_ATTACK_DETECTED = "REPLAY_ATTACK_DETECTED"
    INVALID_PACKET = "INVALID_PACKET"
    UNAUTHORIZED_COMMAND = "UNAUTHORIZED_COMMAND"
    INVALID_MODE_TRANSITION = "INVALID_MODE_TRANSITION"
    FAULT_INJECTED = "FAULT_INJECTED"
    FAULT_CLEARED = "FAULT_CLEARED"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    TRACKING = "TRACKING"
    CHAT_SENT = "CHAT_SENT"
    CHAT_RECEIVED = "CHAT_RECEIVED"
    CHAT_CORRUPTED = "CHAT_CORRUPTED"
    MANEUVER_COMPLETED = "MANEUVER_COMPLETED"
    ATTITUDE_CHANGED = "ATTITUDE_CHANGED"


@dataclass
class MissionEvent:
    """One atomic mission/security event."""

    event_type: str
    severity: str
    message: str
    source: str = "SIMULATION"
    timestamp: float = field(default_factory=time.time)
    sim_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Collects events in memory and forwards them to registered sinks.

    Sinks (e.g. the database store) subscribe once at startup; observers hold
    an immutable snapshot so the dashboard polling never mutates it.
    """

    def __init__(self) -> None:
        self._events: list[MissionEvent] = []
        self._sinks: list[Callable[[MissionEvent], None]] = []

    def subscribe(self, sink: Callable[[MissionEvent], None]) -> None:
        self._sinks.append(sink)

    def emit(self, event: MissionEvent) -> None:
        self._events.append(event)
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - a sink must not break the sim
                import logging
                logging.getLogger("events").exception(
                    "Event sink failed for %s", event.event_type)

    def emit_event(self, event_type: str, severity: str, message: str,
                   source: str = "SIMULATION", sim_time: float = 0.0,
                   timestamp: float | None = None) -> MissionEvent:
        event = MissionEvent(
            event_type=event_type, severity=severity, message=message,
            source=source, sim_time=sim_time,
            timestamp=timestamp if timestamp is not None else time.time())
        self.emit(event)
        return event

    def history(self, limit: int | None = None) -> list[MissionEvent]:
        events = list(self._events)
        if limit is not None and limit > 0:
            events = events[-limit:]
        return events

    def clear(self) -> None:
        self._events.clear()