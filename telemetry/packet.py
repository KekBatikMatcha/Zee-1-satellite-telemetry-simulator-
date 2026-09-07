"""Zee-1 telemetry link packet: binary framing, build, parse, validate.

Why a real packet and not a JSON blob?
--------------------------------------
A spacecraft TT&C link is a byte stream with strict framing. A frame must be
self-describing: synchronised, versioned, addressed to a spacecraft ID, typed,
sequenced, timed, length-prefixed, and covered by an integrity check. Sending
raw JSON over a socket hides all of that. This module implements the wire
format documented in ``docs/telemetry-protocol.md``. The JSON *payload* inside
the frame is a pragmatic choice for a decodable educational format; a CCSDS
Space Packet or AX.25 upgrade is a documented future step.

Wire format (all multi-byte fields big-endian)
----------------------------------------------
+----------------+--------+--------------------------------------------------+
| Field          | Bytes  | Notes                                            |
+----------------+--------+--------------------------------------------------+
| SYNC           | 4      | 0xDE 0xAD 0xBE 0xEF (educational ASM word)       |
| VERSION        | 1      | protocol version                                 |
| SATELLITE_ID   | 16     | ASCII, space-padded                              |
| PACKET_TYPE    | 1      | see PacketType enum                              |
| CATEGORY       | 1      | see TelemetryCategory enum                       |
| PRIORITY       | 1      | see TelemetryPriority enum                       |
| SEQUENCE_NBR   | 4      | uint32                                           |
| TIMESTAMP      | 8      | float64, simulated mission seconds               |
| PAYLOAD_LENGTH | 2      | uint16                                           |
| PAYLOAD        | var    |                               |               |
| CRC16          | 2      | over fields from VERSION through PAYLOAD         |
+----------------+--------+--------------------------------------------------+

Exported constants keep the layout readable and single-sourced so tests and
documentation can reason about offsets.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

from telemetry.crc import crc16, crc16_bytes
from telemetry.schema import (
    PacketType,
    TelemetryCategory,
    TelemetryPriority,
    TelemetryRecord,
)

# ---------------------------------------------------------------------------
# Fixed layout (see table above)
# ---------------------------------------------------------------------------
SYNC = b"\xde\xad\xbe\xef"
SYNC_SIZE = 4
VERSION = 0x01
SATELLITE_ID_FIELD = 16
HEADER_FIXED = (1 + SATELLITE_ID_FIELD + 1 + 1 + 1 + 4 + 8 + 2)  # bytes
PACKET_HEADER = SYNC_SIZE + HEADER_FIXED  # everything before payload
CRC_SIZE = 2

MAX_PAYLOAD = 0xFFFF
MAX_PACKET = PACKET_HEADER + MAX_PAYLOAD + CRC_SIZE


class PacketError(Exception):
    """Structural problem with the packet (framing, length, version...)."""


class CrcMismatchError(PacketError):
    """CRC reached the receiver different from the one computed on arrival."""


@dataclass(frozen=True)
class Packet:
    """Decoded packet header + raw payload + integrity metadata."""

    satellite_id: str
    packet_type: PacketType
    category: TelemetryCategory
    priority: TelemetryPriority
    sequence_number: int
    timestamp: float
    payload: bytes
    crc: int
    crc_valid: bool
    size: int


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def encode_satellite_id(satellite_id: str) -> bytes:
    if len(satellite_id) > SATELLITE_ID_FIELD:
        raise PacketError(
            f"satellite_id too long ({len(satellite_id)} > {SATELLITE_ID_FIELD})")
    return satellite_id.encode("ascii").ljust(SATELLITE_ID_FIELD, b" ")


def decode_satellite_id(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").strip()


def build_packet(
    satellite_id: str,
    packet_type: PacketType,
    category: TelemetryCategory,
    priority: TelemetryPriority,
    sequence_number: int,
    timestamp: float,
    payload: bytes,
) -> bytes:
    """Assemble a framed packet (header + payload + CRC)."""
    if not 0 <= sequence_number <= 0xFFFFFFFF:
        raise PacketError("sequence_number out of uint32 range")
    if len(payload) > MAX_PAYLOAD:
        raise PacketError(f"payload too large ({len(payload)} bytes)")

    header_no_sync = bytes([
        VERSION,
    ]) + encode_satellite_id(satellite_id) + bytes([
        packet_type.value,
        category.value,
        priority.value,
    ]) + struct.pack(
        ">IdH",
        sequence_number,
        float(timestamp),
        len(payload),
    )
    body = header_no_sync + payload
    crc_field = crc16_bytes(body)
    return SYNC + body + crc_field


# ---------------------------------------------------------------------------
# Parse / validate
# ---------------------------------------------------------------------------

def parse_packet(data: bytes, verify_crc: bool = True) -> Packet:
    """Parse frame *data*; optionally verify CRC.

    Raises :class:`PacketError` for framing problems and
    :class:`CrcMismatchError` when the CRC does not match (only if
    ``verify_crc`` is true). Ground-station code distinguishes the two so it
    can tally rejected vs. corrupted packets separately.
    """
    if len(data) < PACKET_HEADER + CRC_SIZE:
        raise PacketError(
            f"truncated packet: {len(data)} bytes < min {PACKET_HEADER + CRC_SIZE}")

    if data[:SYNC_SIZE] != SYNC:
        raise PacketError("bad sync word")

    version = data[SYNC_SIZE]
    if version != VERSION:
        raise PacketError(f"unsupported version 0x{version:02x}")

    sat_raw = data[SYNC_SIZE + 1: SYNC_SIZE + 1 + SATELLITE_ID_FIELD]
    ptype_b = data[SYNC_SIZE + 1 + SATELLITE_ID_FIELD]
    cat_b = data[SYNC_SIZE + 2 + SATELLITE_ID_FIELD]
    prio_b = data[SYNC_SIZE + 3 + SATELLITE_ID_FIELD]

    seq = struct.unpack(">I", data[SYNC_SIZE + 4 + SATELLITE_ID_FIELD:
                                   SYNC_SIZE + 8 + SATELLITE_ID_FIELD])[0]
    timestamp = struct.unpack(">d", data[SYNC_SIZE + 8 + SATELLITE_ID_FIELD:
                                         SYNC_SIZE + 16 + SATELLITE_ID_FIELD])[0]
    payload_len = struct.unpack(">H", data[SYNC_SIZE + 16 + SATELLITE_ID_FIELD:
                                           SYNC_SIZE + 18 + SATELLITE_ID_FIELD])[0]

    header_size = SYNC_SIZE + 18 + SATELLITE_ID_FIELD
    expected = header_size + payload_len + CRC_SIZE
    if len(data) != expected:
        raise PacketError(
            f"length mismatch: got {len(data)}, expected {expected}")

    body = data[SYNC_SIZE: header_size + payload_len]
    crc_rx = struct.unpack(">H", data[header_size + payload_len:
                                      expected])[0]
    crc_calc = crc16(body)

    try:
        packet_type = PacketType(ptype_b)
    except ValueError:
        packet_type = PacketType.UNKNOWN
    try:
        category = TelemetryCategory(cat_b)
    except ValueError:
        category = TelemetryCategory.SYSTEM
    try:
        priority = TelemetryPriority(prio_b)
    except ValueError:
        priority = TelemetryPriority.LOW

    if verify_crc and crc_rx != crc_calc:
        raise CrcMismatchError(
            f"CRC mismatch: header said {crc_rx:04X}, computed {crc_calc:04X}")

    return Packet(
        satellite_id=decode_satellite_id(sat_raw),
        packet_type=packet_type,
        category=category,
        priority=priority,
        sequence_number=seq,
        timestamp=timestamp,
        payload=data[header_size: header_size + payload_len],
        crc=crc_rx,
        crc_valid=(crc_rx == crc_calc),
        size=len(data),
    )


def corrupt_payload(data: bytes, bits: int = 1) -> bytes:
    """Flip *bits* bits inside the payload for fault/corruption simulation."""
    if len(data) <= PACKET_HEADER + CRC_SIZE:
        raise PacketError("cannot corrupt: packet too small")
    buf = bytearray(data)
    import random
    rng = random.Random()
    lo = SYNC_SIZE + 1
    hi = len(data) - CRC_SIZE
    for _ in range(bits):
        pos = rng.randrange(lo, hi)
        bit = rng.randrange(8)
        buf[pos] ^= 1 << bit
    return bytes(buf)


# ---------------------------------------------------------------------------
# Payload (de)serialization
# ---------------------------------------------------------------------------

def encode_payload(record: TelemetryRecord | dict[str, Any]) -> bytes:
    if isinstance(record, TelemetryRecord):
        payload = record.to_dict()
    else:
        payload = record
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode_payload(payload: bytes) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PacketError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PacketError("payload JSON root must be an object")
    return data