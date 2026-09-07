"""Fault injection for the Zee-1 simulator (simulation-only).

Why this exists
---------------
Operator and security training needs realistic failure responses. Faults are
safe, sandboxed, and purely simulated: they only mutate this process's in-memory
spacecraft/link state. There is no mechanism to reach any real system.

Each fault declares what it does to the simulated spacecraft and/or link, so
the dashboard and documentation can explain the expected response. Faults are
driven through a single controller; spacecraft and link read the active set on
every tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("simulation.faults")


@dataclass
class FaultDefinition:
    name: str
    description: str
    severity: str          # INFO..CRITICAL
    applies_to: str        # "SATELLITE" | "LINK" | "BOTH"
    effect: str            # human-readable effect summary


FAULT_REGISTRY: dict[str, FaultDefinition] = {
    "LOW_BATTERY": FaultDefinition(
        "LOW_BATTERY",
        "Battery rapidly discharges toward critical; spacecraft degrades to SAFE.",
        "CRITICAL", "SATELLITE",
        "EPS simulator draws +20 W, draining the pack; SAFE mode triggers below 15%.",
    ),
    "HIGH_TEMPERATURE": FaultDefinition(
        "HIGH_TEMPERATURE",
        "Bus thermal equilibrium raised ~30 C; temperature exceeds threshold.",
        "HIGH", "SATELLITE",
        "Thermal model offset +30 C pushes bus temp toward the 70 C alert.",
    ),
    "HIGH_CPU": FaultDefinition(
        "HIGH_CPU",
        "CPU load pinned near full utilisation by a runaway process.",
        "MEDIUM", "SATELLITE",
        "OBC model applies a +45% CPU offset; anomaly detector flags it.",
    ),
    "SENSOR_ANOMALY": FaultDefinition(
        "SENSOR_ANOMALY",
        "Telemetry readings include large spikes (e.g. stuck/flaky sensor).",
        "MEDIUM", "SATELLITE",
        "Battery and temperature readings get strong gaussian spikes at build time.",
    ),
    "SOLAR_ARRAY_FAILURE": FaultDefinition(
        "SOLAR_ARRAY_FAILURE",
        "Solar array output derated to 30% of nominal.",
        "HIGH", "SATELLITE",
        "EPS derates array output; net power falls, battery drains over time.",
    ),
    "STORAGE_FULL": FaultDefinition(
        "STORAGE_FULL",
        "Onboard store-and-forward queue nearly full; oldest low-priority data dropped.",
        "MEDIUM", "SATELLITE",
        "Effective queue capacity reduced to 4 packets; drops begin.",
    ),
    "COMMS_OUTAGE": FaultDefinition(
        "COMMS_OUTAGE",
        "Radio link fault: link degrades (typical link effect).",
        "CRITICAL", "LINK",
        "Link outage flips the satellite radio into FAULT and drops all traffic.",
    ),
    "PACKET_LOSS": FaultDefinition(
        "PACKET_LOSS",
        "Raised packet loss on the space link.",
        "MEDIUM", "LINK",
        "Link simulates error with boosted loss probability.",
    ),
    "PACKET_CORRUPTION": FaultDefinition(
        "PACKET_CORRUPTION",
        "Raised corruption on the space link (CRC failures).",
        "MEDIUM", "LINK",
        "Link simulates error with boosted corruption probability.",
    ),
}


@dataclass
class ActiveFault:
    name: str
    injected_at: float
    duration_s: float | None = None  # None = until cleared


class FaultController:
    """Holds the active fault set. Safe by construction: names must be in
    the registry, and faults never touch anything outside the simulation."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._active: dict[str, ActiveFault] = {}

    def inject(self, name: str, sim_time: float = 0.0,
               duration_s: float | None = None) -> FaultDefinition:
        if name not in FAULT_REGISTRY:
            raise KeyError(f"unknown fault '{name}'; known: "
                           f"{sorted(FAULT_REGISTRY)}")
        if not self.enabled:
            raise PermissionError("fault injection is disabled")
        if name in self._active:
            raise ValueError(f"fault '{name}' is already active")
        self._active[name] = ActiveFault(name, sim_time, duration_s)
        logger.warning("Fault injected: %s", name)
        return FAULT_REGISTRY[name]

    def clear(self, name: str) -> bool:
        removed = self._active.pop(name, None)
        if removed:
            logger.info("Fault cleared: %s", name)
        return removed is not None

    def clear_all(self) -> list[str]:
        names = list(self._active)
        self._active.clear()
        return names

    def is_active(self, name: str) -> bool:
        return name in self._active

    def active_faults(self) -> set[str]:
        return set(self._active)

    def expire(self, sim_time: float) -> list[str]:
        """Remove faults whose duration elapsed (called each tick)."""
        expired = [n for n, f in self._active.items()
                   if f.duration_s is not None and sim_time - f.injected_at >= f.duration_s]
        for name in expired:
            self._active.pop(name)
            logger.info("Fault expired: %s", name)
        return expired

    def list_active(self) -> list[dict[str, Any]]:
        return [asdict(self._active[n]) for n in sorted(self._active)]

    def list_all(self) -> list[dict[str, Any]]:
        return [asdict(d) for d in FAULT_REGISTRY.values()]