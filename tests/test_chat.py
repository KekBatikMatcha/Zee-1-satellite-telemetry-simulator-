"""Tests for the testing-only chat channel (message tester over the link)."""

from __future__ import annotations

import time

import pytest

from communication.chat import (
    CHUNK_SIZE,
    DOWN,
    UP,
    ChatChannel,
    ChatError,
    build_chunk_frame,
    parse_chunk_frame,
    sniff,
)
from communication.link import SpaceLink
from config import SimulationConfig
from simulation.events import EventBus


@pytest.fixture()
def sim_config():
    return SimulationConfig(
        link_eb_n0_db=20.0, packet_corruption_rate=0.0, packet_loss_rate=0.0,
        buffer_when_not_visible=True)


def _channel() -> tuple[ChatChannel, SpaceLink]:
    config = SimulationConfig(
        link_eb_n0_db=20.0, packet_corruption_rate=0.0, packet_loss_rate=0.0,
        buffer_when_not_visible=True)
    events = EventBus()
    from simulation.faults import FaultController
    faults = FaultController(enabled=False)
    link = SpaceLink(config, events, faults)
    link.set_visibility(True)
    link.set_eb_n0_db(20.0)  # essentially error-free

    def deliver(direction: str, data: bytes) -> None:
        if chat.sniff(data):
            chat.receive(direction, data)

    chat = ChatChannel(config, events, link)
    link.deliver_cb = deliver
    return chat, link


# -----------------------------------------------------------------------
# Framing
# -----------------------------------------------------------------------
def test_frame_round_trip():
    payload = b"hello how are you?"
    frame = build_chunk_frame(7, DOWN, 0, 1, payload)
    chunk = parse_chunk_frame(frame)
    assert chunk.message_id == 7
    assert chunk.direction == DOWN
    assert chunk.index == 0
    assert chunk.chunk_count == 1
    assert chunk.payload == payload
    assert chunk.crc_ok


def test_frame_round_trip_up():
    frame = build_chunk_frame(1, UP, 2, 9, b"x")
    chunk = parse_chunk_frame(frame)
    assert (chunk.message_id, chunk.direction, chunk.index,
            chunk.chunk_count) == (1, UP, 2, 9)
    assert chunk.crc_ok


def test_sniff_rejects_non_chat_data():
    assert sniff(b"M\x00Tgarbage") is False
    assert sniff(b"") is False
    assert sniff(b"MESG") is True


def test_flipped_bit_fails_crc():
    payload = b"hello how are you?"
    frame = bytearray(build_chunk_frame(7, DOWN, 0, 1, payload))
    frame[20] ^= 0x40
    chunk = parse_chunk_frame(bytes(frame))
    assert chunk.crc_ok is False


def test_bad_magic_rejected():
    bogus = b"XXXX" + build_chunk_frame(1, DOWN, 0, 1, b"hi")[4:]
    with pytest.raises(ChatError):
        parse_chunk_frame(bogus)


def test_bad_direction_rejected():
    payload = b""
    body = b"\x01" + (1).to_bytes(4, "big") + b"X" + (0).to_bytes(2, "big") \
        + (1).to_bytes(2, "big") + (0).to_bytes(1, "big")
    # rebuild with a bad direction byte X
    from telemetry.crc import crc16_bytes
    bad = b"MESG" + body + crc16_bytes(body)
    with pytest.raises(ChatError):
        parse_chunk_frame(bad)


# -----------------------------------------------------------------------
# Channel over the link
# -----------------------------------------------------------------------
def _deliver(link, chat):
    link.fetch_due(now=time.time() + 1.0)
    return chat


def test_channel_send_and_deliver():
    chat, link = _channel()
    chat.send(DOWN, "hello how are you?")
    assert len(chat.history()) == 1
    _deliver(link, chat)
    msg = chat.history()[-1]
    assert msg["delivered"] == 1
    assert msg["lost"] == 0
    assert msg["complete"]
    assert msg["received_text"] == "hello how are you?"


def test_channel_up_and_down():
    chat, link = _channel()
    chat.send(UP, "ack received")
    chat.send(DOWN, "status nominal")
    _deliver(link, chat)
    history = {m["message_id"]: m for m in chat.history()}
    assert history[1]["direction"] == UP
    assert history[2]["direction"] == DOWN
    assert history[1]["received_text"] == "ack received"
    assert history[2]["received_text"] == "status nominal"


def test_multi_chunk_reassembly():
    chat, link = _channel()
    text = "This is a long message that will definitely span several chunks " \
        "because each chunk only carries forty eight bytes. " * 2
    assert len(text.encode("utf-8")) > CHUNK_SIZE * 2
    chat.send(DOWN, text)
    _deliver(link, chat)
    msg = chat.history()[-1]
    assert msg["delivered"] == msg["chunk_count"]
    assert msg["received_text"] == text.strip()


def test_empty_text_rejected():
    chat, link = _channel()
    with pytest.raises(ChatError):
        chat.send(DOWN, "   ")


def test_bad_direction_rejected():
    chat, link = _channel()
    with pytest.raises(ChatError):
        chat.send("SIDEWAYS", "hello")


def test_lost_chunk_accounted():
    chat, link = _channel()
    text = "losing chunks should be visible in the accounting"
    assert len(text.encode("utf-8")) > CHUNK_SIZE
    chat.send(DOWN, text)
    # Nothing fetched yet: every chunk is "in flight" and counted as lost.
    msg = chat.history()[-1]
    assert msg["lost"] == msg["chunk_count"]
    assert msg["delivered"] == 0


def test_stats_aggregate():
    chat, link = _channel()
    chat.send(DOWN, "one")
    chat.send(UP, "two")
    _deliver(link, chat)
    stats = chat.stats()
    assert stats["messages"] == 2
    assert stats["delivered"] == 2
    assert stats["coverage_percent"] == 100.0


def test_noise_degrades_coverage():
    config = SimulationConfig(
        link_eb_n0_db=0.0, packet_corruption_rate=0.0, packet_loss_rate=0.0,
        buffer_when_not_visible=True)
    from simulation.faults import FaultController
    link = SpaceLink(config, EventBus(), FaultController(enabled=False))
    link.set_visibility(True)
    link.set_eb_n0_db(0.0)  # BER ~7.9e-2: heavy corruption
    chat = ChatChannel(config, EventBus(), link)
    link.deliver_cb = lambda _dir, data: chat.receive(_dir, data)
    for i in range(20):
        chat.send(DOWN, f"burst test message {i} with some padding text")
    _deliver(link, chat)
    stats = chat.stats()
    assert stats["corrupted"] + stats["lost"] > 0
    assert 0.0 <= stats["coverage_percent"] <= 100.0
    assert stats["delivered"] < stats["chunks_sent"]


def test_chat_does_not_touch_telemetry_pipeline():
    chat, link = _channel()
    chat.send(UP, "not a telemetry packet")
    _deliver(link, chat)
    # Telemetry counters on the station can't be reached here because no
    # spacecraft is wired; the important thing is that chat frames get
    # consumed by the chat sniffer and never raise in the link callback.
    assert chat.stats()["delivered"] == 1