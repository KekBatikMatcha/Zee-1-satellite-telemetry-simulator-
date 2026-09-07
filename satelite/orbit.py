"""Simplified circular-orbit model for Zee-1.

Why this exists
---------------
The dashboard, ground-station visibility, and link simulation all need a
plausible, continuously-evolving satellite position. This module provides a
deterministic circular-LEO ground track.

Explicit limits
---------------
* Single orbit plane, no J2/precession, no Kepler element set.
* Earth modelled as a sphere (WGS84 radius); no drag.
* Manoeuvres are along-track burns only: they change circular speed and
  altitude, shifting the ground track and future pass times (no plane change).
* Ground-track longitude includes Earth's rotation for realism.

This is an educational orbit approximation and is NOT suitable for real
mission operations or pass planning. A future SGP4/TLE upgrade is documented
in docs/orbit-model.md.
"""

from __future__ import annotations

import math

from telemetry.models import EARTH_RADIUS_KM, EARTH_MU_M3_S2
from telemetry import models as tm

EARTH_ROTATION_RAD_S = 7.2921159e-5  # sidereal rotation rate

ORBIT_ALT_MIN_KM = 300.0     # hard lower altitude limit (safety)
ORBIT_ALT_MAX_KM = 2000.0    # hard upper altitude limit
MAX_DELTA_V_MPS = 200.0      # max single-manoeuvre burn magnitude


class OrbitModel:
    """Propagates a circular, inclined LEO state from simulated time."""

    def __init__(
        self,
        altitude_km: float,
        inclination_deg: float,
        raan_deg: float = 0.0,
        initial_phase_deg: float = 0.0,
    ) -> None:
        if not 0 <= inclination_deg <= 90:
            raise ValueError("inclination must be within [0, 90] degrees")
        self.altitude_km = float(altitude_km)
        self.inclination = math.radians(inclination_deg)
        self.raan = math.radians(raan_deg)
        self.initial_phase = math.radians(initial_phase_deg)

    @property
    def period_s(self) -> float:
        return tm.orbit_period_seconds(self.altitude_km)

    @property
    def velocity_kms(self) -> float:
        return tm.orbit_velocity_mps(self.altitude_km) / 1000.0

    def phase_rad(self, sim_time: float) -> float:
        return tm.orbit_phase_rad(sim_time, self.altitude_km, self.initial_phase)

    def altitude_after_burn(self, delta_v_mps: float) -> float:
        """Resulting mean altitude after an along-track burn of *delta_v_mps*.

        Real orbit maths, simplified deliberately:
          * an instantaneous tangential burn at radius r gives a new
            semi-major axis  a = 1 / (2/r - v_new**2/mu)   (vis-viva),
          * so burning "forward" (+dv, prograde) RISES the orbit -- it
            becomes slower with a longer period, while braking (-dv,
            retrograde) DROPS it into a lower, faster orbit.
        """
        r_cur = (EARTH_RADIUS_KM + self.altitude_km) * 1000.0
        v0 = tm.orbit_velocity_mps(self.altitude_km)
        v1 = v0 + float(delta_v_mps)
        a1 = 1.0 / (2.0 / r_cur - v1 * v1 / EARTH_MU_M3_S2)
        alt1 = a1 / 1000.0 - EARTH_RADIUS_KM
        return max(ORBIT_ALT_MIN_KM, min(ORBIT_ALT_MAX_KM, alt1))

    def apply_burn(self, delta_v_mps: float, sim_time: float) -> dict[str, float]:
        """Apply an along-track manoeuvre at *sim_time*; returns new orbit."""
        if not -MAX_DELTA_V_MPS <= float(delta_v_mps) <= MAX_DELTA_V_MPS:
            raise ValueError(
                f"burn magnitude {delta_v_mps} m/s outside "
                f"[{-MAX_DELTA_V_MPS}, {MAX_DELTA_V_MPS}] m/s")

        old_alt, old_period, old_v = (self.altitude_km, self.period_s,
                                      self.velocity_kms)
        phase_now = self.phase_rad(sim_time)

        self.altitude_km = self.altitude_after_burn(delta_v_mps)
        new_period, new_v = self.period_s, self.velocity_kms

        # Keep the *position* continuous across the burn: re-anchor the
        # initial phase so phase_rad(sim_time) is unchanged, only the future
        # angular speed (and therefore every later pass time) changes.
        self.initial_phase = math.degrees(
            (phase_now - 2.0 * math.pi * sim_time / new_period)
            % (2.0 * math.pi))

        return {
            "delta_v_mps": round(float(delta_v_mps), 2),
            "old_altitude_km": round(old_alt, 2),
            "new_altitude_km": round(self.altitude_km, 2),
            "old_velocity_kms": round(old_v, 4),
            "new_velocity_kms": round(new_v, 4),
            "old_period_s": round(old_period, 1),
            "new_period_s": round(new_period, 1),
        }

    def position_at(self, sim_time: float) -> dict[str, float]:
        """Return lat/lon/alt (Earth-fixed) + velocity + sunlight + phase.

        Steps:
          1. Mean anomaly advances at constant angular rate (circular orbit).
          2. Components are projected into an inertial frame tilted by the
             inclination and rotated by the RAAN.
          3. The sub-satellite longitude is shifted by Earth's rotation so the
             ground track walks westward, as a real LEO pass does.
        """
        phase = self.phase_rad(sim_time)
        radius_km = EARTH_RADIUS_KM + self.altitude_km

        cth, sth = math.cos(phase), math.sin(phase)
        ci, si = math.cos(self.inclination), math.sin(self.inclination)
        cn, sn = math.cos(self.raan), math.sin(self.raan)

        x = cth * cn - sth * sn * ci
        y = cth * sn + sth * cn * ci
        z = sth * si

        lon_inertial = math.degrees(math.atan2(y, x))
        latitude = math.degrees(math.asin(max(-1.0, min(1.0, z))))
        gmst = math.degrees(EARTH_ROTATION_RAD_S * sim_time)
        longitude = (lon_inertial - gmst + 180.0) % 360.0 - 180.0

        return {
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "altitude_km": round(self.altitude_km, 2),
            "velocity_kms": round(self.velocity_kms, 4),
            "phase_rad": phase,
            "sunlight": tm.sunlight_factor(phase),
        }