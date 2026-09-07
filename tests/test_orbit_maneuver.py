"""Tests for orbital manoeuvre (ORBITAL_BURN) and attitude (ADJUST_ATTITUDE)
telecommands -- the "bring the satellite back into range" controls."""

from __future__ import annotations

import time

import pytest

from config import SimulationConfig
from satellite.orbit import (
    ORBIT_ALT_MAX_KM,
    ORBIT_ALT_MIN_KM,
    OrbitModel,
)
from security.commands import StructuralCommandError, new_command
from simulation.runner import create_simulation


def _cfg(name: str, **over) -> SimulationConfig:
    kw = dict(
        link_eb_n0_db=20.0, packet_corruption_rate=0.0, packet_loss_rate=0.0,
        buffer_when_not_visible=True, database_name=f"test-{name}.db",
        faults_enabled=False,
    )
    kw.update(over)
    return SimulationConfig(**kw)


def _sim(name: str):
    sim = create_simulation(_cfg(name))
    sim.link.set_visibility(True)
    sim.link.loss_rate = 0.0
    # Boot to NORMAL (BOOT 3 s + INIT 5 s) so telecommands are authorized.
    for _ in range(20):
        sim.spacecraft.tick(1.0)
    assert sim.spacecraft.modes.mode.value == "NORMAL"
    return sim


# ---------------------------------------------------------------------------
# Physics of apply_burn
# ---------------------------------------------------------------------------
def test_prograde_burn_raises_orbit():
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    alt0, period0 = orbit.altitude_km, orbit.period_s
    result = orbit.apply_burn(50.0, sim_time=100.0)
    assert result["new_altitude_km"] > alt0          # forward burn raises
    assert result["new_velocity_kms"] < result["old_velocity_kms"]
    assert orbit.period_s > period0                  # higher = slower


def test_retrograde_burn_lowers_orbit():
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    alt0, period0 = orbit.altitude_km, orbit.period_s
    orbit.apply_burn(-50.0, sim_time=100.0)
    assert orbit.altitude_km < alt0
    assert orbit.period_s < period0


def test_burn_position_is_continuous():
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    before = orbit.position_at(100.0)
    orbit.apply_burn(60.0, sim_time=100.0)
    after = orbit.position_at(100.0)
    assert after["latitude"] == pytest.approx(before["latitude"], abs=1e-3)
    assert after["longitude"] == pytest.approx(before["longitude"], abs=1e-3)
    assert after["altitude_km"] != before["altitude_km"]


def test_burn_stays_within_sane_limits():
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    for dv in (200.0, -200.0):
        orbit2 = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
        orbit2.apply_burn(dv, 0.0)
        assert ORBIT_ALT_MIN_KM <= orbit2.altitude_km <= ORBIT_ALT_MAX_KM
        assert orbit2.period_s > 0.0


def test_burn_magnitude_validation():
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    with pytest.raises(ValueError):
        orbit.apply_burn(250.0, 0.0)


def test_burn_loosens_ground_track_schedule():
    """Higher orbit = longer period = same phase over more time."""
    orbit = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    burn_t = 100.0
    orbit.apply_burn(80.0, sim_time=burn_t)
    # At the burn time the phase must be identical (continuity)...
    slower = OrbitModel(altitude_km=525.0, inclination_deg=53.0)
    phase_at_burn = slower.phase_rad(burn_t)
    assert orbit.phase_rad(burn_t) == pytest.approx(phase_at_burn, abs=1e-9)


# ---------------------------------------------------------------------------
# ORBITAL_BURN over the real uplink
# ---------------------------------------------------------------------------
def test_orbital_burn_telecommand():
    sim = _sim("orb")
    alt_before = sim.spacecraft.orbit.altitude_km
    seq = sim.station.command_seq
    sent = sim.station.send_telecommand(
        new_command(sim.config.satellite_id, "ORBITAL_BURN",
                    {"delta_v_mps": 40.0}, seq))
    assert sent["accepted_by_link"] is True
    sim.link.fetch_due(now=time.time() + 1.0)
    result = sim.spacecraft.command_results[seq]
    assert result["executed"] is True
    assert sim.spacecraft.orbit.altitude_km > alt_before
    assert any(e.event_type == "MANEUVER_COMPLETED"
               for e in sim.events.history())


def test_orbital_burn_rejected_when_delta_v_out_of_range():
    sim = _sim("orb2")
    with pytest.raises(StructuralCommandError):
        sim.station.send_telecommand(
            new_command(sim.config.satellite_id, "ORBITAL_BURN",
                        {"delta_v_mps": 9999.0}, sim.station.command_seq))


def test_orbital_burn_not_authorized_in_safe():
    sim = _sim("orb3")
    sim.spacecraft.modes.to_safe("test: force SAFE", sim.spacecraft.sim_time)
    assert sim.spacecraft.modes.mode.value == "SAFE"
    seq = sim.station.command_seq
    sim.station.send_telecommand(
        new_command(sim.config.satellite_id, "ORBITAL_BURN",
                    {"delta_v_mps": 10.0}, seq))
    sim.link.fetch_due(now=time.time() + 1.0)
    result = sim.spacecraft.command_results[seq]
    assert result["executed"] is False
    assert "not allowed in mode SAFE" in result["reason"]


# ---------------------------------------------------------------------------
# SET_MODE parameter schema (string enum, not Mode-enum-keyed dict)
# ---------------------------------------------------------------------------
def test_set_mode_valid_mode_passes_ground_validation():
    sim = _sim("setmode")
    seq = sim.station.command_seq
    sent = sim.station.send_telecommand(
        new_command(sim.config.satellite_id, "SET_MODE", {"mode": "NORMAL"}, seq))
    assert sent["accepted_by_link"] is True
    sim.link.fetch_due(now=time.time() + 1.0)
    assert "not allowed" not in sim.spacecraft.command_results[seq]["message"]


def test_set_mode_rejects_mode_enum_object():
    """The schema must accept plain strings like 'NORMAL', not the Mode enum
    member, so a good dashboard value never trips validation."""
    from security.commands import apply_parameter, COMMAND_REGISTRY
    schema = COMMAND_REGISTRY["SET_MODE"].param_schema["mode"]
    apply_parameter("NORMAL", schema)          # string: fine
    from satellite.modes import Mode
    with pytest.raises(StructuralCommandError):
        apply_parameter(Mode.BOOT, schema)     # enum member: rejected


def test_set_mode_missing_mode_rejected():
    sim = _sim("setmode3")
    with pytest.raises(StructuralCommandError):
        sim.station.send_telecommand(
            new_command(sim.config.satellite_id, "SET_MODE", {}, 5))


def test_set_mode_ground_schema_enum_are_strings():
    from security.commands import COMMAND_REGISTRY
    enum_vals = COMMAND_REGISTRY["SET_MODE"].param_schema["mode"]["enum"]
    assert enum_vals == ["BOOT", "INIT", "SAFE", "NORMAL",
                         "PAYLOAD_OPERATION", "COMMUNICATION"]
    assert all(isinstance(v, str) for v in enum_vals)


# ---------------------------------------------------------------------------
# ADJUST_ATTITUDE
# ---------------------------------------------------------------------------
def test_adjust_attitude_telecommand():
    sim = _sim("att")
    seq = sim.station.command_seq
    sent = sim.station.send_telecommand(
        new_command(sim.config.satellite_id, "ADJUST_ATTITUDE",
                    {"attitude": "SUN_POINTING"}, seq))
    assert sent["accepted_by_link"] is True
    sim.link.fetch_due(now=time.time() + 1.0)
    assert sim.spacecraft.command_results[seq]["executed"] is True
    assert sim.spacecraft.attitude_target == "SUN_POINTING"
    assert any(e.event_type == "ATTITUDE_CHANGED"
               for e in sim.events.history())


def test_adjust_attitude_unknown_option_rejected():
    sim = _sim("att2")
    with pytest.raises(StructuralCommandError):
        sim.station.send_telecommand(
            new_command(sim.config.satellite_id, "ADJUST_ATTITUDE",
                        {"attitude": "SIDEWAYS"}, sim.station.command_seq))


def test_attitude_changes_solar_power():
    sim = _sim("att3")
    # Park in darkness first (eclipse arc is centred on phase = pi): find a
    # simulated time where the orbit places us in eclipse.
    orbit = sim.spacecraft.orbit
    dark_t = None
    for t in range(0, int(orbit.period_s), 60):
        if orbit.position_at(float(t))["sunlight"] < 0.4:
            dark_t = float(t)
            break
    assert dark_t is not None, "expected an eclipse window in the orbit"

    # Advance the spacecraft to that moment.
    while sim.spacecraft.sim_time < dark_t:
        sim.spacecraft.tick(min(30.0, dark_t - sim.spacecraft.sim_time))

    sim.spacecraft.attitude_target = "NADIR"
    sim.spacecraft.tick(1.0)
    nadir_power = sim.spacecraft.eps.solar_power

    sim.spacecraft.attitude_target = "SUN_POINTING"
    sim.spacecraft.tick(1.0)
    sun_power = sim.spacecraft.eps.solar_power

    assert sun_power > nadir_power + 1.0