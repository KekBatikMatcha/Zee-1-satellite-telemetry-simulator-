"""CRC-16 integrity check for telemetry packets.

What CRC does
-------------
A CRC detects *accidental* corruption (bit flips, dropped/reordered bytes)
introduced by a noisy channel or faulty hardware. It provides integrity /
error DETECTION only.

What CRC does NOT do
---------------------
* It does NOT provide confidentiality (the data is not secret).
* It does NOT provide authentication (anyone can recompute the CRC).
* It does NOT protect against deliberate tampering.

For telemetry (downlink) CRC is the accepted baseline; for telecommands
(uplink) CRC is *augmented* by an HMAC in :mod:`security`.

Polynomial
----------
CRC-16-CCITT (XModem variant): poly 0x1021, init 0xFFFF, no refin/refout,
final XOR 0x0000. Stored big-endian (MSB first). This is a widely used,
safely documented choice.
"""

from __future__ import annotations

import struct

POLY = 0x1021
INIT = 0xFFFF
LOOKUP: list[int] = []


def _build_table() -> list[int]:
    table: list[int] = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ POLY) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


LOOKUP = _build_table()


def crc16(data: bytes) -> int:
    """Return the CRC-16-CCITT checksum of *data*."""
    crc = INIT
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ LOOKUP[((crc >> 8) ^ byte) & 0xFF]
    return crc & 0xFFFF


def crc16_bytes(data: bytes) -> bytes:
    """Return the checksum as two big-endian bytes."""
    return struct.pack(">H", crc16(data))


def crc16_hex(data: bytes) -> str:
    return f"{crc16(data):04X}"