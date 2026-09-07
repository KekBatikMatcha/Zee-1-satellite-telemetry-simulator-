"""Ground-station receiver: raw bytes -> framed packet outcome.

Receiving is the first of three ground-side stages:

    receiver  ->  validator  ->  decoder

The receiver only determines *did a well-formed frame arrive?* and *is its
CRC intact?* It does not trust content. It distinguishes:

* CRC_FAILURE  - frame structure OK but integrity check failed (corruption).
* MALFORMED    - framing/sync/length problems (junk, partial, interference).
* ACCEPTED     - frame is structurally intact and CRC-valid (still unchecked).
"""

from __future__ import annotations

from dataclasses import dataclass

from telemetry.packet import Packet, PacketError, parse_packet


@dataclass(frozen=True)
class ReceiveOutcome:
    tag: str                # "ACCEPTED" | "CRC_FAILURE" | "MALFORMED"
    packet: Packet | None
    reason: str | None = None


class Receiver:
    """Stages 1-2: framing + CRC verification of a received byte string."""

    def process(self, data: bytes) -> ReceiveOutcome:
        if not isinstance(data, bytes) or not data:
            return ReceiveOutcome("MALFORMED", None, "empty frame")
        try:
            packet = parse_packet(data, verify_crc=False)
        except PacketError as exc:
            return ReceiveOutcome("MALFORMED", None, str(exc))
        if not packet.crc_valid:
            return ReceiveOutcome(
                "CRC_FAILURE", packet,
                f"CRC mismatch: stored {packet.crc:04X}")
        return ReceiveOutcome("ACCEPTED", packet, None)