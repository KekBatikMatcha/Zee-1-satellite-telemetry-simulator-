"""Ground-station orchestration: receive → validate → decode → store.

The ground station is the trusted control-side termination of the space
link. It must never trust the channel. This component wires together the
receiver, sequence validator, decoder, contact tracking, event logging, and
database persistence, and hosts the *uplink gateway* for telecommands.

Responsibilities (mirrors docs/ground-station.md)
--------------------------------------------------
1. Receive frames delivered by the link.
2. Reject malformed/corrupt frames (tallied separately).
3. Detect missing / duplicate / late sequence numbers.
4. Decode payloads into telemetry records (source-verified).
5. Persist telemetry + packet records, emit events.
6. Track ground contact (AOS/LOS) and expose link statistics.
7. Authorise + sign + transmit telecommands to the spacecraft.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from communication.link import DOWNLINK, UPLINK, SpaceLink
from config import SimulationConfig
from ground_station.decoder import Decoder
from ground_station.receiver import Receiver
from ground_station.validator import Validator
from security.commands import (
    CommandRequest,
    StructuralCommandError,
    UnknownCommandError,
    validate_command,
    validate_parameters,
)
from security.commands_auth import sign_command
from security.replay import ReplayGuard
from simulation.anomaly import RuleConfig, worst_severity
from simulation.events import EventBus, Severity
from telemetry.schema import TelemetryRecord
from telemetry import packet as pkt

logger = logging.getLogger("ground_station")


class GroundStation:
    """Downlink receiver + uplink gateway for one ground site."""

    def __init__(
        self,
        config: SimulationConfig,
        events: EventBus,
        link: SpaceLink,
        store: Any,                # database.Store (injected, keeps this web-free)
        secret: str,               # shared command secret
        record_sink: Callable[[TelemetryRecord], None] | None = None,
    ) -> None:
        self.config = config
        self.events = events
        self.link = link
        self.store = store
        self.secret = secret
        self.record_sink = record_sink   # optional telemetry subscriber

        self.receiver = Receiver()
        self.validator = Validator()
        self.rule_config = RuleConfig(config)
        self.decoder = Decoder(
            expected_satellite_id=config.satellite_id,
            rule_config=self.rule_config)

        self.previous_record: TelemetryRecord | None = None
        self.command_seq = 1000
        self.replay_guard = ReplayGuard()

        # Local counters (link reports its own end-to-end numbers).
        self.received = 0
        self.corrupted = 0
        self.rejected = 0
        self.accepted = 0
        self.missing = 0
        self.duplicates = 0
        self.late = 0
        self.previous_contact_state: bool | None = None

    # ------------------------------------------------------------------
    # Downlink
    # ------------------------------------------------------------------
    def on_downlink(self, data: bytes) -> TelemetryRecord | None:
        """Full receive pipeline for one frame arriving from the link."""
        self.received += 1
        outcome = self.receiver.process(data)

        if outcome.tag == "MALFORMED":
            self.rejected += 1
            self.events.emit_event(
                "INVALID_PACKET", Severity.WARNING.value,
                f"malformed frame rejected: {outcome.reason}",
                source="GROUND_STATION")
            return None

        if outcome.tag == "CRC_FAILURE":
            self.corrupted += 1
            self.rejected += 1
            self.events.emit_event(
                "CRC_FAILURE", Severity.HIGH.value,
                f"CRC failure, seq {outcome.packet.sequence_number if outcome.packet else '?'}",
                source="GROUND_STATION")
            self._persist_packet_log(outcome.packet, "CRC_FAILURE")
            return None

        validation = self.validator.validate(outcome)
        if not validation.accepted:
            self.rejected += 1
            return None

        # Sequence accounting (accepted frame).
        if validation.missing_count > 0:
            self.missing += validation.missing_count
            self.events.emit_event(
                "PACKET_MISSING", Severity.WARNING.value,
                f"missing {validation.missing_count} packet(s); last valid was "
                f"{self.validator.expected_seq - validation.missing_count - 1}",
                source="GROUND_STATION")
        if validation.sequence_status.value == "DUPLICATE":
            self.duplicates += 1
        elif validation.sequence_status.value == "LATE":
            self.late += 1

        result = self.decoder.decode(validation.packet)
        if not result.ok or result.record is None:
            self.rejected += 1
            self.events.emit_event(
                "INVALID_PACKET", Severity.WARNING.value,
                f"decode failed: {result.reason}",
                source="GROUND_STATION")
            self._persist_packet_log(validation.packet, "DECODE_FAILED")
            return None

        self.accepted += 1
        record = result.record
        self._persist_packet_log(validation.packet, "ACCEPTED")

        if record.anomalies:
            worst = worst_severity([
                a for a in self._as_anomaly_objects(record.anomalies)])
            for anom in record.anomalies:
                self.events.emit_event(
                    "ANOMALY_DETECTED", anom["severity"],
                    f"[{anom['rule']}] {anom['message']}",
                    source="GROUND_STATION", sim_time=record.mission_time)
            if worst >= 4:
                self.events.emit_event(
                    "SECURITY_ALERT", Severity.HIGH.value,
                    f"critical anomaly on seq {record.sequence_number}",
                    source="GROUND_STATION", sim_time=record.mission_time)

        try:
            self.store.insert_telemetry(record)
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist telemetry record")
        if self.record_sink is not None:
            try:
                self.record_sink(record)
            except Exception:  # noqa: BLE001
                logger.exception("record sink failed")

        self.previous_record = record
        return record

    @staticmethod
    def _as_anomaly_objects(anomalies: list[dict]) -> list[Any]:
        class _A:
            def __init__(self, d: dict):
                self.severity_level = {
                    "NORMAL": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3,
                    "CRITICAL": 4}.get(d.get("severity"), 0)
        return [_A(a) for a in anomalies]

    def _persist_packet_log(self, packet: Any, status: str) -> None:
        try:
            self.store.insert_packet(
                sequence_number=packet.sequence_number,
                packet_type=packet.packet_type.name,
                received_at=time.time(),
                payload_size=len(packet.payload),
                crc_valid=packet.crc_valid,
                status=status,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist packet log")

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------
    def update_contact(self, visibility: Any) -> str | None:
        """Feed satellite visibility; emit AOS/LOS events, return transition."""
        if visibility.visible and (self.previous_contact_state is False or
                                   self.previous_contact_state is None):
            self.previous_contact_state = True
            self.events.emit_event(
                "GROUND_CONTACT_STARTED", Severity.INFO.value,
                f"AOS: elevation {visibility.elevation_deg} deg",
                source="GROUND_STATION")
            return "AOS"
        if (not visibility.visible) and self.previous_contact_state is True:
            self.previous_contact_state = False
            self.events.emit_event(
                "GROUND_CONTACT_LOST", Severity.INFO.value,
                f"LOS: elevation {visibility.elevation_deg} deg",
                source="GROUND_STATION")
            return "LOS"
        self.previous_contact_state = visibility.visible
        return None

    # ------------------------------------------------------------------
    # Uplink (telecommand gateway)
    # ------------------------------------------------------------------
    def send_telecommand(self, command: CommandRequest) -> dict[str, Any]:
        """Validate, authorize-basics, sign, and transmit a command uplink.

        Returns a result dict; the spacecraft performs its own verification
        and mode authorization before execution.
        """
        spec = validate_command(command)          # structural + registry check
        validate_parameters(command, spec)      # declarative parameter schema
        if command.satellite_id != self.config.satellite_id:
            raise ValueError("command addressed to another satellite")

        # Ground-side replay guard (first line of defense).
        if self.replay_guard.is_replay(command.satellite_id, command.sequence):
            self.events.emit_event(
                "REPLAY_ATTACK_DETECTED", Severity.HIGH.value,
                f"ground guard rejected replayed command seq {command.sequence}",
                source="GROUND_STATION")
            raise ValueError("command sequence already used (possible replay)")

        signed = sign_command(command, self.secret)
        payload = pkt.encode_payload(signed)
        frame = pkt.build_packet(
            satellite_id=self.config.satellite_id,
            packet_type=pkt.PacketType.COMMAND,
            category=pkt.TelemetryCategory.HOUSEKEEPING,
            priority=pkt.TelemetryPriority.CRITICAL,
            sequence_number=command.sequence,
            timestamp=command.timestamp,
            payload=payload,
        )

        self.replay_guard.record(command.satellite_id, command.sequence)
        accepted = self.link.transmit(UPLINK, frame)
        self.command_seq += 1
        self.events.emit_event(
            "TELECOMMAND_RECEIVED", Severity.INFO.value,
            f"uplink {command.command} seq {command.sequence} "
            f"{'accepted by link' if accepted else 'blocked by link'}",
            source="GROUND_STATION")
        return {
            "accepted_by_link": accepted,
            "command": command.command,
            "sequence": command.sequence,
        }

    # ------------------------------------------------------------------
    # Aggregated statistics for the API / dashboard
    # ------------------------------------------------------------------
    def statistics(self) -> dict[str, Any]:
        link_stats = self.link.statistics()
        return {
            "packets_received": self.received,
            "packets_accepted": self.accepted,
            "packets_corrupted": self.corrupted,
            "packets_rejected": self.rejected,
            "packets_missing": self.missing,
            "duplicates": self.duplicates,
            "late": self.late,
            "expected_sequence": self.validator.expected_seq,
            "link": link_stats,
        }