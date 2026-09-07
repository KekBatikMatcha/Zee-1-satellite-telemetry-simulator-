"""Testing-only chat channel: send text between satellite and ground.

Why this exists
---------------
A quick way to *feel* the link. You type a message ("hello how are you?"),
choose a direction (satellite -> ground, or ground -> satellite) and the text
is split into chunks, framed like real space packets, and pushed through the
exact same link used for telemetry/telecommands -- so it inherits the
physical RF channel (BPSK/AWGN noise), packet loss, latency, visibility gate
and bandwidth shaping.

At the far end the chunks are reassembled and you can see:
* how many chunks were delivered intact,
* how many arrived with the CRC broken (noise destroyed them),
* how many were lost outright (drops / below the elevation mask),
* the text that *actually* arrived, with lost/corrupt pieces marked.

Because it is only a testing aid it lives entirely in memory and does not
touch the real telemetry/command pipelines. Its frames use a distinct sync
word (``M:\x00G``... see below) so it can never be confused with a telemetry
or command frame.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Any

from telemetry.crc import crc16, crc16_bytes
from simulation.events import EventBus, Severity

MAGIC = b"MESG"
VERSION = 0x01
CHUNK_SIZE = 48  # bytes per link frame
MAX_CHUNKS = 0xFFFF

UP = "UP"        # ground -> satellite
DOWN = "DOWN"    # satellite -> ground
DIRECTIONS = (UP, DOWN)
#: direction -> the link's internal channel name
_LINK_DIR = {UP: "uplink", DOWN: "downlink"}
#: direction -> wire byte on the frame
_WIRE_DIR = {UP: b"U", DOWN: b"D"}
#: wire byte -> direction
_FROM_WIRE = {_WIRE_DIR[k][0]: k for k in DIRECTIONS}

# Chunk status values
SENT = "SENT"
DELIVERED = "DELIVERED"
CORRUPTED = "CORRUPTED"
LOST = "LOST"


class ChatError(Exception):
    """Malformed chat frame."""


@dataclass
class ChatChunk:
    index: int
    status: str = SENT
    payload: bytes = b""
    crc_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "status": self.status,
                "crc_ok": self.crc_ok}


@dataclass
class ChatMessage:
    message_id: int
    direction: str
    sent_text: str
    chunk_count: int
    sent_at: float = field(default_factory=time.time)
    chunks: list[ChatChunk] = field(default_factory=list)

    # ------------------------------------------------------------------
    def received_text(self) -> str:
        """What the far end can reconstruct from what actually arrived."""
        parts: list[str] = []
        for ch in self.chunks:
            if ch.status == DELIVERED:
                try:
                    parts.append(ch.payload.decode("utf-8"))
                except UnicodeDecodeError:
                    parts.append("[corrupt]")
            elif ch.status == CORRUPTED:
                raw = ch.payload.decode("utf-8", errors="replace")
                parts.append(f"<{raw}>")
            else:  # LOST / not yet accounted
                parts.append("[--]")
        return "".join(parts)

    def accounted(self) -> tuple[int, int, int]:
        """(delivered, corrupted, lost) given what is known so far."""
        delivered = sum(1 for c in self.chunks if c.status == DELIVERED)
        corrupted = sum(1 for c in self.chunks if c.status == CORRUPTED)
        resolved = delivered + corrupted
        known_lost = sum(1 for c in self.chunks if c.status == LOST)
        lost = known_lost + (self.chunk_count - resolved)
        return delivered, corrupted, max(0, lost)

    def complete(self) -> bool:
        delivered, corrupted, _ = self.accounted()
        return delivered + corrupted >= self.chunk_count

    def to_dict(self) -> dict[str, Any]:
        delivered, corrupted, lost = self.accounted()
        return {
            "message_id": self.message_id,
            "direction": self.direction,
            "label": "satellite -> ground" if self.direction == DOWN
            else "ground -> satellite",
            "sent_text": self.sent_text,
            "chunk_count": self.chunk_count,
            "chunks": [c.to_dict() for c in self.chunks],
            "delivered": delivered,
            "corrupted": corrupted,
            "lost": lost,
            "received_text": self.received_text(),
            "complete": self.complete(),
            "sent_at": self.sent_at,
        }


# ---------------------------------------------------------------------------
# Framing (a chat frame = one chunk)
# ---------------------------------------------------------------------------
def build_chunk_frame(message_id: int, direction: str, index: int,
                      chunk_count: int, payload: bytes) -> bytes:
    if direction not in DIRECTIONS:
        raise ChatError(f"direction must be one of {DIRECTIONS}")
    if chunk_count > MAX_CHUNKS:
        raise ChatError("too many chunks")
    body = struct.pack(
        ">BIBHHB", VERSION, message_id, _WIRE_DIR[direction][0],
        index, chunk_count, len(payload)) + payload
    return MAGIC + body + crc16_bytes(body)


@dataclass
class ParsedChunk:
    message_id: int
    direction: str
    index: int
    chunk_count: int
    payload: bytes
    crc_ok: bool


def sniff(data: bytes) -> bool:
    return data.startswith(MAGIC)


def parse_chunk_frame(data: bytes) -> ParsedChunk:
    if not data.startswith(MAGIC):
        raise ChatError("not a chat frame (bad magic)")
    if len(data) < 15:
        raise ChatError("chat frame too short")
    version = data[4]
    if version != VERSION:
        raise ChatError(f"unsupported chat version 0x{version:02x}")
    version_, message_id, dir_byte, index, chunk_count, plen = struct.unpack(
        ">BIBHHB", data[4:15])
    direction = _FROM_WIRE.get(dir_byte)
    if direction is None:
        raise ChatError(f"bad direction byte 0x{dir_byte:02x}")
    header_end = 15
    if len(data) != header_end + plen + 2:
        raise ChatError(f"chat length mismatch: got {len(data)}, "
                        f"expected {header_end + plen + 2}")
    payload = data[header_end: header_end + plen]
    crc_rx, = struct.unpack(">H", data[header_end + plen: header_end + plen + 2])
    crc_ok = crc_rx == crc16(data[4: header_end + plen])
    return ParsedChunk(message_id, direction, index, chunk_count, payload, crc_ok)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------
class ChatChannel:
    """Frames text into chat packets, pushes them over the real link, and
    reassembles whatever arrives at the far end."""

    def __init__(self, config: Any, events: EventBus, link: Any) -> None:
        self.config = config
        self.events = events
        self.link = link
        self._next_id = 1
        #: message_id -> ChatMessage (both directions; global id space)
        self._messages: dict[int, ChatMessage] = {}

    # -- public ------------------------------------------------------------
    def send(self, direction: str, text: str) -> dict[str, Any]:
        if direction not in DIRECTIONS:
            raise ChatError(f"direction must be one of {DIRECTIONS}")
        text = text.strip()
        if not text:
            raise ChatError("message text is empty")

        payload = text.encode("utf-8")
        chunks = [payload[i:i + CHUNK_SIZE]
                  for i in range(0, len(payload), CHUNK_SIZE)]
        msg = ChatMessage(
            message_id=self._next_id, direction=direction, sent_text=text,
            chunk_count=len(chunks))
        for i, chunk in enumerate(chunks):
            msg.chunks.append(ChatChunk(index=i, payload=chunk))
        self._next_id += 1
        self._messages[msg.message_id] = msg

        for chunk in msg.chunks:
            frame = build_chunk_frame(
                msg.message_id, direction, chunk.index,
                msg.chunk_count, chunk.payload)
            # The real link decides: visibility, loss, RF corruption, shaping.
            self.link.transmit(_LINK_DIR[direction], frame)

        delivered, corrupted, lost = msg.accounted()
        self.events.emit_event(
            "CHAT_SENT", Severity.INFO.value,
            f"chat #{msg.message_id} {msg.direction}: '{text}' "
            f"({msg.chunk_count} chunk(s)) pushed to link",
            source="MESSAGE_TEST")
        return msg.to_dict()

    def sniff(self, data: bytes) -> bool:
        return sniff(data)

    def receive(self, direction: str, data: bytes) -> dict[str, Any] | None:
        """Handle a chat frame arriving from the link on either side."""
        try:
            chunk = parse_chunk_frame(data)
        except ChatError as exc:
            self.events.emit_event(
                "CHAT_CORRUPTED", Severity.WARNING.value,
                f"chat frame rejected: {exc}", source="MESSAGE_TEST")
            return None

        msg = self._messages.get(chunk.message_id)
        if msg is None:
            # A chunk for a message this process did not send (not expected
            # in the tester, but be tolerant).
            msg = ChatMessage(
                message_id=chunk.message_id, direction=chunk.direction,
                sent_text="<unknown sender>", chunk_count=chunk.chunk_count)
            for i in range(chunk.chunk_count):
                msg.chunks.append(ChatChunk(index=i))
            self._messages[msg.message_id] = msg

        if 0 <= chunk.index < len(msg.chunks):
            target = msg.chunks[chunk.index]
            target.payload = chunk.payload
            target.crc_ok = chunk.crc_ok
            target.status = DELIVERED if chunk.crc_ok else CORRUPTED

        delivered, corrupted, lost = msg.accounted()
        severity = (Severity.INFO.value if corrupted == 0
                    else Severity.WARNING.value)
        event_type = "CHAT_RECEIVED" if corrupted == 0 else "CHAT_CORRUPTED"
        if msg.complete():
            self.events.emit_event(
                event_type, severity,
                f"chat #{msg.message_id} {msg.direction} complete: "
                f"delivered {delivered}, corrupt {corrupted}, lost {lost}; "
                f"received: '{msg.received_text()}'",
                source="MESSAGE_TEST")
        return msg.to_dict()

    # -- observability ------------------------------------------------------
    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        items = [m.to_dict() for m in self._messages.values()]
        return items[-limit:]

    def stats(self) -> dict[str, Any]:
        sent = delivered = corrupted = lost = 0
        for m in self._messages.values():
            sent += m.chunk_count
            d, c, l = m.accounted()
            delivered += d
            corrupted += c
            lost += l
        return {
            "messages": len(self._messages),
            "chunks_sent": sent,
            "delivered": delivered,
            "corrupted": corrupted,
            "lost": lost,
            "coverage_percent": round(
                (delivered / sent * 100.0) if sent else 100.0, 1),
        }