"""Tests for the physical RF channel (BPSK over AWGN) and its link wiring."""

from __future__ import annotations

import random
import time

import pytest

from communication.link import SpaceLink
from communication.rf_channel import (
    RadioChannel,
    ber_bpsk,
    bits_from_bytes,
    bytes_from_bits,
    burst_errors,
    required_eb_n0_db,
)
from config import SimulationConfig
from simulation.events import EventBus


# -----------------------------------------------------------------------
# Bit helpers
# -----------------------------------------------------------------------
def test_bits_bytes_roundtrip():
    data = bytes(range(256))
    assert bytes_from_bits(bits_from_bytes(data)) == data


@pytest.mark.parametrize("db,expected", [
    (0.0, 7.86e-2),
    (4.0, 1.25e-2),
    (6.0, 2.39e-3),
    (8.0, 1.91e-4),
    (10.0, 3.87e-6),
])
def test_theoretical_ber(db, expected):
    assert ber_bpsk(db) == pytest.approx(expected, rel=0.02)


def test_required_eb_n0_reaches_1e5():
    db = required_eb_n0_db(1e-5)
    assert db == pytest.approx(9.6, abs=0.3)
    assert ber_bpsk(db) <= 1e-5


# -----------------------------------------------------------------------
# Monte-Carlo: measured BER matches theory
# -----------------------------------------------------------------------
@pytest.mark.parametrize("db", [4.0, 6.0, 8.0, 10.0])
def test_measured_ber_matches_theory(db):
    rng = random.Random(1234)
    rf = RadioChannel(eb_n0_db=db, rng=rng)
    payload = bytes(range(256)) * 64  # ~131k bits
    rf.transmit_data(payload)
    measured = rf.measured_ber
    assert measured is not None
    # Noise realization, so allow up to 4x theory (a 10 dB run has ~0 errors).
    assert measured <= ber_bpsk(db) * 4.0 + 1e-12


def test_no_errors_at_high_eb_n0():
    rng = random.Random(9)
    rf = RadioChannel(eb_n0_db=30.0, rng=rng)
    payload = b"x" * 200
    assert rf.transmit_data(payload) == payload
    assert rf.measured_ber in (0.0, None)


def test_deterministic_with_seed():
    payload = bytes(range(64))
    results = []
    for _ in range(3):
        rng = random.Random(5)
        rf = RadioChannel(eb_n0_db=5.0, rng=rng)
        results.append(rf.transmit_data(payload))
    assert results[0] == results[1] == results[2]


def test_eb_n0_live_update_changes_errors():
    rng = random.Random(3)
    rf = RadioChannel(eb_n0_db=30.0, rng=rng)
    payload = bytes(range(64))
    assert rf.transmit_data(payload) == payload
    rf.set_eb_n0_db(2.0)
    corrupted = sum(rf.transmit_data(payload) != payload for _ in range(50))
    assert corrupted > 0
    assert rf.link_quality_percent < 30.0


def test_burst_errors_flips_bits():
    rng = random.Random(1)
    data = b"packet-" * 10
    out = burst_errors(data, rng)
    assert out != data
    assert len(out) == len(data)


# -----------------------------------------------------------------------
# Integration: link counts physical corruption, ground CRC denies it
# -----------------------------------------------------------------------
def _make_sim(eb_n0_db: float):
    cfg = SimulationConfig(
        link_eb_n0_db=eb_n0_db,
        packet_corruption_rate=0.0,
        packet_loss_rate=0.0,
        buffer_when_not_visible=True,
        database_name=f"test-rf-{abs(hash(eb_n0_db)) % 1000}.db",
    )
    from database.db import connect, init_db, wipe_db
    from database.store import Store
    from ground_station.station import GroundStation
    from satellite.spacecraft import Spacecraft
    from simulation.faults import FaultController

    conn = connect(cfg.database_path)
    wipe_db(conn)
    init_db(conn)
    events = EventBus()
    store = Store(conn)
    events.subscribe(store.on_event)
    faults = FaultController(enabled=False)
    link = SpaceLink(cfg, events, faults)
    spacecraft = Spacecraft(cfg, events, faults)
    station = GroundStation(cfg, events, link, store, cfg.satellite_command_secret)

    def deliver(direction: str, data: bytes) -> None:
        if direction == "downlink":
            station.on_downlink(data)
        elif direction == "uplink":
            spacecraft.on_uplink(data)

    spacecraft.transmit_cb = lambda data: link.transmit("downlink", data)
    link.set_delivery_callback(deliver)
    link.set_visibility(True)
    return spacecraft, link, station, store, conn


def test_low_eb_n0_produces_crc_rejections():
    spacecraft, link, station, store, conn = _make_sim(2.0)
    try:
        for _ in range(80):
            spacecraft.tick(1.0)
            link.fetch_due(now=time.time() + 1.0)  # past transmit latency
        assert link.corrupted >= 1
        assert station.corrupted >= 1
        assert station.accepted < 80
        assert store.conn.execute(
            "SELECT COUNT(*) FROM telemetry").fetchone()[0] < 80
    finally:
        conn.close()


def test_high_eb_n0_clean_link():
    spacecraft, link, station, store, conn = _make_sim(30.0)
    try:
        for _ in range(10):
            spacecraft.tick(1.0)
            link.fetch_due(now=time.time() + 1.0)
        assert link.corrupted == 0
        assert station.corrupted == 0
        assert station.accepted == 10
    finally:
        conn.close()
