"""Deterministic, documented telemetry value models.

Design decision
---------------
Telemetry must look like a *physical system*, not independent random samples.
Each value therefore follows a small documented model with state, inertia,
and inputs. This gives the ground station, anomaly detector, and fault
injector real signal to work with. Every model is an educational
approximation, deliberately simplified, and each has a stated assumption.

All functions are pure: they take the previous state (and a seeded random
generator where noise is wanted) and return the next state. That makes them
trivially testable and reproducible.
"""

from __future__ import annotations

import math
import random
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Orbital constants (WGS84-ish)
# ---------------------------------------------------------------------------
EARTH_RADIUS_KM = 6371.0
EARTH_MU_M3_S2 = 3.986004418e14  # Earth gravitational parameter
ECLIPSE_HALF_ANGLE_DEG = 35.0    # angular half-width of eclipsed arc (rounded)


def orbit_period_seconds(altitude_km: float) -> float:
    """Circular-orbit period from altitude: T = 2*pi*sqrt(a^3/mu)."""
    a = (EARTH_RADIUS_KM + altitude_km) * 1000.0
    return 2.0 * math.pi * math.sqrt(a**3 / EARTH_MU_M3_S2)


def orbit_velocity_mps(altitude_km: float) -> float:
    """Circular-orbit speed: v = sqrt(mu / a)."""
    a = (EARTH_RADIUS_KM + altitude_km) * 1000.0
    return math.sqrt(EARTH_MU_M3_S2 / a)


def orbit_phase_rad(sim_time: float, altitude_km: float,
                    initial_phase_deg: float = 0.0) -> float:
    """Mean anomaly (orbital phase) in radians at a given simulated time."""
    period = orbit_period_seconds(altitude_km)
    return (2.0 * math.pi * sim_time / period +
            math.radians(initial_phase_deg)) % (2.0 * math.pi)


def sunlight_factor(phase_rad: float,
                    eclipse_half_angle_rad: float = math.radians(ECLIPSE_HALF_ANGLE_DEG),
                    eclipse_center_rad: float = math.pi) -> float:
    """Fraction of sunlight (0..1) at an orbital phase.

    Model: a simplified eclipse — every orbit the spacecraft passes through a
    shadowed arc of width 2*half_angle centered at ``eclipse_center_rad``.
    Inside the arc sunlight ramps to 0; elsewhere it is 1. This approximates
    the LEO day/night cycle without a full Sun/Earth geometry model.
    """
    half = eclipse_half_angle_rad
    dist = abs(((phase_rad - eclipse_center_rad + math.pi) % (2 * math.pi)) - math.pi)
    if dist >= half:
        return 1.0
    # Smooth ramp at the edges (penumbra-like) for nicer signal, 15% of arc.
    edge = half * 0.15
    if dist <= (half - edge):
        return 0.0
    return (dist - (half - edge)) / edge


def solar_power_watts(sunlight: float, solar_peak_w: float) -> float:
    """Solar array output scales with illumination fraction."""
    return max(0.0, solar_peak_w * sunlight)


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------

#: Mode -> steady-state spacecraft power draw (watts).
MODE_POWER_W: dict[str, float] = {
    "BOOT": 4.0,
    "INIT": 5.0,
    "SAFE": 2.5,
    "NORMAL": 6.0,
    "PAYLOAD_OPERATION": 12.0,
    "COMMUNICATION": 9.0,
}

BATTERY_CAPACITY_WH = 20.0          # energy capacity of the simulated pack
BATTERY_V_FULL = 8.4                 # 2S Li-ion, fully charged
BATTERY_V_EMPTY = 6.4                # cut-off voltage
CHARGE_EFFICIENCY = 0.92             # losses while charging
DISCHARGE_EFFICIENCY = 1.0
SOLAR_ARRAY_PEAK_W = 45.0            # max panel output in full sunlight


def battery_new_percent(
    pct: float,
    net_power_w: float,          # solar - consumption (can be negative)
    dt_s: float,
    capacity_wh: float = BATTERY_CAPACITY_WH,
    charge_efficiency: float = CHARGE_EFFICIENCY,
    discharge_efficiency: float = DISCHARGE_EFFICIENCY,
) -> float:
    """Integrate battery state of charge.

    dE (Wh) = P_net (W) * dt (s) / 3600, scaled by charge/discharge
    efficiency. Positive net power charges the pack, negative discharges it.
    Clamped to [0, 100]; no energy teleportation.

    Assumption: a single aggregated battery bus; no cell balancing, pack
    temperature dependence, or Peukert effects.
    """
    dt_h = dt_s / 3600.0
    efficiency = charge_efficiency if net_power_w >= 0 else discharge_efficiency
    energy_delta_wh = net_power_w * dt_h * efficiency
    new_pct = pct + energy_delta_wh / capacity_wh * 100.0
    return max(0.0, min(100.0, new_pct))


def battery_voltage_volts(
    pct: float,
    load_w: float,
    r_int_ohm: float = 0.08,
) -> float:
    """Cell-OCV-inspired voltage with internal-resistance drop.

    OCV(pct) = V_empty + (V_empty_percent_gain) * sqrt(pct) + linear ramp.
    The square-root term reproduces the steeper voltage fall near empty that
    real lithium cells show. Load current I ~= load/bus_voltage lowers the
    terminal voltage by I * R_int.
    """
    p = pct / 100.0
    ocv = BATTERY_V_EMPTY + 0.4 * math.sqrt(p) + 1.6 * p
    bus_voltage = ocv                                  # internal bus ~ battery
    current = load_w / bus_voltage if bus_voltage > 0 else 0.0
    return max(BATTERY_V_EMPTY - 0.2, ocv - current * r_int_ohm)


# ---------------------------------------------------------------------------
# Thermal
# ---------------------------------------------------------------------------

#: Mode -> equilibrium (battery/bus) temperature in degrees Celsius.
MODE_EQUILIBRIUM_TEMP_C: dict[str, float] = {
    "BOOT": 17.0,
    "INIT": 18.0,
    "SAFE": 15.0,
    "NORMAL": 22.0,
    "PAYLOAD_OPERATION": 33.0,
    "COMMUNICATION": 26.0,
}
THERMAL_TIME_CONSTANT_S = 900.0     # slow response: mass + radiator time constant
SUNLIGHT_THERMAL_GAIN_K = 2.0       # extra equilibrium temp in full sunlight
ECLIPSE_THERMAL_OFFSET_K = -0.5     # small cooling in eclipse


def thermal_new_temperature(
    current_c: float,
    mode: str,
    sunlight: float,
    dt_s: float,
    tau_s: float = THERMAL_TIME_CONSTANT_S,
    target_offset_c: float = 0.0,
) -> float:
    """First-order thermal settling toward a mode-dependent equilibrium.

    temp(t+dt) = temp + (dt/tau) * (equilibrium - temp)
    Equilibrium shifts up in sunlight, slightly down in eclipse, and can be
    raised by a fault offset (``target_offset_c``). The [0,1] sunlight factor
    interpolates between sunlit and eclipsed equilibria.
    """
    eq = MODE_EQUILIBRIUM_TEMP_C.get(mode, 22.0) + target_offset_c
    equilibrium = eq + SUNLIGHT_THERMAL_GAIN_K * sunlight + ECLIPSE_THERMAL_OFFSET_K
    alpha = min(1.0, dt_s / tau_s)
    return current_c + alpha * (equilibrium - current_c)


# ---------------------------------------------------------------------------
# Computing load
# ---------------------------------------------------------------------------

#: Mode -> steady-state CPU utilisation (%).
MODE_CPU_PERCENT: dict[str, float] = {
    "BOOT": 40.0,
    "INIT": 55.0,
    "SAFE": 18.0,
    "NORMAL": 30.0,
    "PAYLOAD_OPERATION": 82.0,
    "COMMUNICATION": 48.0,
}
CPU_TIME_CONSTANT_S = 8.0          # fast transient response
MEMORY_TIME_CONSTANT_S = 30.0
PAYLOAD_MEMORY_BUFFER_PERCENT = 8.0   # extra RSS consumed by payload buffers


def smoothed_load(
    current: float,
    target: float,
    dt_s: float,
    tau_s: float,
    rng: random.Random,
    noise_std: float = 2.0,
    minimum: float = 0.0,
    maximum: float = 100.0,
) -> float:
    """Exponential smoothing toward a target plus bounded noise.

    Models short-term load transients (process wake-ups, scheduling) without
    producing arbitrary jumps. This is a load *model*, not a kernel metric.
    """
    alpha = min(1.0, dt_s / tau_s)
    smooth = current + alpha * (target - current)
    noisy = smooth + rng.gauss(0.0, noise_std)
    return max(minimum, min(maximum, noisy))


def memory_usage_percent(
    current: float,
    mode: str,
    dt_s: float,
    rng: random.Random,
) -> float:
    """System memory model: baseline + mode buffers + payload growth.

    PAYLOAD_OPERATION grows memory as telemetry/payload buffers accumulate,
    whatever the OS-level "process" story would be.
    """
    baseline = 40.0
    if mode in ("PAYLOAD_OPERATION", "COMMUNICATION"):
        target = baseline + PAYLOAD_MEMORY_BUFFER_PERCENT
    elif mode == "SAFE":
        target = 30.0
    elif mode == "BOOT":
        target = 35.0
    else:
        target = baseline
    return smoothed_load(current, target, dt_s, MEMORY_TIME_CONSTANT_S, rng,
                         noise_std=0.8, minimum=5.0, maximum=98.0)


def payload_storage_growth(
    storage_pct: float,
    payload_active: bool,
    dt_s: float,
    grow_per_minute: float = 0.25,
) -> float:
    """Storage utilisation grows while the payload is actively producing data."""
    if not payload_active:
        return storage_pct
    if storage_pct >= 99.0:
        return storage_pct
    return min(99.9, storage_pct + grow_per_minute * (dt_s / 60.0))


# ---------------------------------------------------------------------------
# Attitude (ADCS)
# ---------------------------------------------------------------------------

def attitude_step(
    current_deg: float,
    rng: random.Random,
    dt_s: float,
    drift_std_deg_s: float = 0.02,
    max_abs_deg: float = 15.0,
) -> float:
    """Bounded random-walk attitude drift + small restoring tendency.

    Models a passively stabilised satellite slowly drifting and being pulled
    back by gravity-gradient torque — not a real quaternion ADCS solution.
    """
    pull = -0.01 * current_deg * dt_s                    # weak restoring torque
    noise = rng.gauss(0.0, drift_std_deg_s) * math.sqrt(dt_s)
    value = current_deg + pull + noise
    return max(-max_abs_deg, min(max_abs_deg, value))


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def gauss(rng: random.Random, std: float, mean: float = 0.0) -> float:
    return rng.gauss(mean, std)


class TelemetryModel(Protocol):
    """Type hint for any model function (import-check only)."""
    def __call__(self, **kwargs: Any) -> Any:  # pragma: no cover
        ...