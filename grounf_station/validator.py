"""Ground-station sequence validator: missing/duplicate/late detection.

The downlink sequence numbers are the *network* view of the mission: gaps
mean lost packets, repeats mean duplicates/retransmissions. Tracking
sequences turns a stream of frames into link quality data.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass

from ground_station.receiver import ReceiveOutcome
from telemetry.packet import Packet


class SequenceStatus(enum.Enum):
    OK = "OK"
    GAP = "GAP"
    DUPLICATE = "DUPLICATE"
    LATE = "LATE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    sequence_status: SequenceStatus
    missing_count: int
    packet: Packet | None
    reason: str | None = None


class Validator:
    """Tracks the expected next sequence and tallies gaps/duplicates."""

    def __init__(self) -> None:
        self.expected_seq: int | None = None  # next sequence we anticipate
        self.gaps_detected = 0
        self.duplicates = 0
        self.late = 0
        self.validated = 0
        self._seen_recent: deque[int] = deque(maxlen=2048)

    def reset(self) -> None:
        self.__init__()

    def validate(self, outcome: ReceiveOutcome) -> ValidationResult:
        """Validate a receiver outcome. Only ACCEPTED frames are sequenced."""
        packet = outcome.packet

        if outcome.tag != "ACCEPTED" or packet is None:
            return ValidationResult(
                accepted=False,
                sequence_status=SequenceStatus.REJECTED,
                missing_count=0,
                packet=packet,
                reason=outcome.reason or "frame not accepted by receiver",
            )

        seq = packet.sequence_number
        if self.expected_seq is None:
            self.expected_seq = seq
            self.validated += 1
            return ValidationResult(True, SequenceStatus.OK, 0, packet, None)

        if seq < self.expected_seq:
            if seq in self._seen_recent:
                self.duplicates += 1
                self.validated += 1
                return ValidationResult(
                    True, SequenceStatus.DUPLICATE, 0, packet, "duplicate frame")
            self.late += 1
            self.validated += 1
            return ValidationResult(
                True, SequenceStatus.LATE, 0, packet, "late/out-of-order frame")

        missing = seq - self.expected_seq
        if missing > 0:
            self.gaps_detected += missing
        self.expected_seq = seq + 1
        self._seen_recent.append(seq)
        self.validated += 1
        return ValidationResult(
            True,
            SequenceStatus.GAP if missing else SequenceStatus.OK,
            missing,
            packet,
            f"detected {missing} missing packet(s)" if missing else None,
        )
