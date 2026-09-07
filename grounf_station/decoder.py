"""Ground-station decoder: packet payload -> telemetry record + anomalies.

The decoder is the last stage of the ground pipeline. It converts the packet
payload (JSON, documented in docs/telemetry-protocol.md) back into a typed
:class:`TelemetryRecord`, verifies the source is the expected satellite, and
runs the rule-based anomaly detector. The result is appended to the record's
``anomalies`` list so the dashboard and database both see it.
"""

from __future__ import annotations

from dataclasses import dataclass

from simulation.anomaly import RuleConfig, detect_anomalies
from telemetry.packet import Packet
from telemetry.schema import TelemetryRecord


@dataclass(frozen=True)
class DecodeResult:
    record: TelemetryRecord | None
    ok: bool
    reason: str | None = None


class Decoder:
    def __init__(self, expected_satellite_id: str,
                 rule_config: RuleConfig | None = None,
                 previous: TelemetryRecord | None = None) -> None:
        self.expected_satellite_id = expected_satellite_id
        self.rule_config = rule_config
        self.previous = previous

    def decode(self, packet: Packet) -> DecodeResult:
        # Source check: never accept telemetry claimed from another spacecraft.
        if packet.satellite_id != self.expected_satellite_id:
            return DecodeResult(
                None, False,
                f"source mismatch: frame from '{packet.satellite_id}', "
                f"expected '{self.expected_satellite_id}'")

        from telemetry.packet import PacketError, decode_payload
        try:
            payload = decode_payload(packet.payload)
        except PacketError as exc:
            return DecodeResult(None, False, str(exc))

        record = TelemetryRecord.from_dict(payload)
        if self.rule_config is not None:
            anomalies = detect_anomalies(record, self.previous, self.rule_config)
            record.anomalies = [a.to_dict() for a in anomalies]
        return DecodeResult(record, True, None)