"""Zee-1 logical subsystem components.

Each component owns a slice of spacecraft state and an ``update(dt, mode,
...)`` method driven by the simulation. This mirrors how a real OBC flight
software polls its subsystem managers every control cycle. The mathematical
dynamics live in :mod:`telemetry.models`; components are thin stateful
wrappers over those documented models.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import telemetry.models as tm


# ---------------------------------------------------------------------------
# Electrical Power System
# ---------------------------------------------------------------------------

@dataclass
class EPS:
    """Simulated power bus: solar input, consumption, battery dynamics."""

    battery_percent: float = 85.0
    solar_peak_w: float = tm.SOLAR_ARRAY_PEAK_W
    capacity_wh: float = tm.BATTERY_CAPACITY_WH
    extra_load_w: float = 0.0          # fault-induced extra draw (e.g. heater)

    battery_voltage: float = field(init=False, default=7.9)
    solar_power: float = field(init=False, default=0.0)
    power_consumption: float = field(init=False, default=0.0)
    net_power: float = field(init=False, default=0.0)
    solar_derating: float = 1.0      # 0..1 fault factor to cut array output

    def update(
        self,
        dt_s: float,
        mode: str,
        sunlight: float,
        payload_active: bool,
        transmitting: bool,
    ) -> None:
        self.solar_power = tm.solar_power_watts(sunlight, self.solar_peak_w) * self.solar_derating
        consumption = tm.MODE_POWER_W.get(mode, 6.0)
        if payload_active:
            consumption += 5.0        # payload power draw
        if transmitting:
            consumption += 2.5        # radio transmit amplifier draw
        self.power_consumption = consumption + self.extra_load_w
        self.net_power = self.solar_power - self.power_consumption
        self.battery_percent = tm.battery_new_percent(
            self.battery_percent, self.net_power, dt_s,
            capacity_wh=self.capacity_wh)
        self.battery_voltage = tm.battery_voltage_volts(
            self.battery_percent, self.power_consumption)


# ---------------------------------------------------------------------------
# On-Board Computer
# ---------------------------------------------------------------------------

@dataclass
class OBC:
    """CPU / memory / storage load."""

    cpu_usage: float = 30.0
    memory_usage: float = 40.0
    storage_usage: float = 20.0
    extra_cpu_pct: float = 0.0      # fault offset pushed onto CPU load

    def update(
        self,
        dt_s: float,
        mode: str,
        rng: random.Random,
        payload_active: bool,
    ) -> None:
        target_cpu = tm.MODE_CPU_PERCENT.get(mode, 30.0)
        self.cpu_usage = tm.smoothed_load(
            self.cpu_usage, target_cpu, dt_s, tm.CPU_TIME_CONSTANT_S, rng,
            noise_std=3.0)
        self.cpu_usage = min(100.0, self.cpu_usage + self.extra_cpu_pct)
        self.memory_usage = tm.memory_usage_percent(
            self.memory_usage, mode, dt_s, rng)
        self.storage_usage = tm.payload_storage_growth(
            self.storage_usage, payload_active, dt_s)


# ---------------------------------------------------------------------------
# Attitude Determination & Control
# ---------------------------------------------------------------------------

@dataclass
class ADCS:
    """Bounded attitude drift (roll/pitch/yaw in degrees)."""

    roll: float = 1.5
    pitch: float = -0.8
    yaw: float = 2.2

    def update(self, dt_s: float, rng: random.Random) -> None:
        self.roll = tm.attitude_step(self.roll, rng, dt_s)
        self.pitch = tm.attitude_step(self.pitch, rng, dt_s)
        self.yaw = tm.attitude_step(self.yaw, rng, dt_s)


# ---------------------------------------------------------------------------
# Thermal
# ---------------------------------------------------------------------------

@dataclass
class Thermal:
    """Bus temperature with first-order thermal inertia."""

    temperature_c: float = 18.0
    target_offset_c: float = 0.0    # fault offset pushed onto equilibrium

    def update(self, dt_s: float, mode: str, sunlight: float) -> None:
        self.temperature_c = tm.thermal_new_temperature(
            self.temperature_c, mode, sunlight, dt_s,
            target_offset_c=self.target_offset_c)


# ---------------------------------------------------------------------------
# Communications subsystem
# ---------------------------------------------------------------------------

@dataclass
class CommSubsystem:
    """Radio behaviour: transmitting state, uplink/downlink counters."""

    status: str = "IDLE"             # IDLE | TRANSMITTING | LISTENING
    transmitting: bool = False
    packets_sent: int = 0

    def update(self, mode: str, visible: bool) -> None:
        # The radio transmits during COMMUNICATION windows and whenever a
        # store-and-forward dump is in progress (visible + buffered data).
        self.transmitting = bool(mode == "COMMUNICATION" and visible)
        self.status = "TRANSMITTING" if self.transmitting else "IDLE"


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

@dataclass
class Payload:
    """Mission payload: imaging/data-generation experiment."""

    status: str = "STANDBY"          # STANDBY | ACTIVE | FAULT
    active: bool = False
    data_bytes: int = 0
    data_rate_bps: int = 2048        # simulated payload output rate

    def update(self, mode: str, dt_s: float) -> None:
        self.active = mode == "PAYLOAD_OPERATION"
        self.status = "ACTIVE" if self.active else "STANDBY"
        if self.active:
            self.data_bytes += int(self.data_rate_bps * dt_s / 8)
