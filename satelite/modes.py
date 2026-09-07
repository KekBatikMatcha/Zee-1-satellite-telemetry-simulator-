"""Zee-1 operating modes and the validated mode state machine.

Why this exists
---------------
A real spacecraft runs a small set of well-defined operating modes and a
state machine that only permits documented transitions. Cybersecurity + fault
safety share a common principle here: never let a component reach a state it
was not designed for. This module encodes the spacecraft's mode policy.

Modes
-----
BOOT                  Initial power-on: limited telemetry, subsystem startup.
INIT                  Post-boot subsystem checks, configuration load.
SAFE                  Degraded: payload disabled, power reduced, comms kept.
NORMAL                Standard operations (nominal housekeeping).
PAYLOAD_OPERATION     Mission payload active: higher CPU/power consumption.
COMMUNICATION         Dense downlink/uplink activity window.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Mode(enum.Enum):
    BOOT = "BOOT"
    INIT = "INIT"
    SAFE = "SAFE"
    NORMAL = "NORMAL"
    PAYLOAD_OPERATION = "PAYLOAD_OPERATION"
    COMMUNICATION = "COMMUNICATION"


# ---------------------------------------------------------------------------
# Transition policy
# ---------------------------------------------------------------------------

#: Legal transitions labelled with a human-readable reason.
VALID_TRANSITIONS: dict[Mode, list[tuple[Mode, str]]] = {
    Mode.BOOT: [
        (Mode.INIT, "boot sequence completed"),
    ],
    Mode.INIT: [
        (Mode.NORMAL, "subsystem checks passed"),
        (Mode.SAFE, "critical fault detected during initialization"),
    ],
    Mode.SAFE: [
        (Mode.NORMAL, "recovery / fault cleared, commanded back to nominal"),
        (Mode.INIT, "re-initialization after recovery"),
    ],
    Mode.NORMAL: [
        (Mode.PAYLOAD_OPERATION, "payload activation commanded"),
        (Mode.COMMUNICATION, "scheduled pass / comm window"),
        (Mode.SAFE, "critical fault detected"),
    ],
    Mode.PAYLOAD_OPERATION: [
        (Mode.NORMAL, "payload deactivation commanded"),
        (Mode.SAFE, "critical fault detected"),
    ],
    Mode.COMMUNICATION: [
        (Mode.NORMAL, "pass complete / comm window closed"),
        (Mode.SAFE, "critical fault detected"),
    ],
}

#: Any mode may degrade to SAFE on a critical fault.
CRITICAL_FAULT_ORIGINS: set[Mode] = {
    Mode.INIT, Mode.NORMAL, Mode.PAYLOAD_OPERATION, Mode.COMMUNICATION,
}


class InvalidModeTransition(Exception):
    """Raised when a requested mode change is not in the transition table."""


@dataclass(frozen=True)
class ModeChange:
    old: Mode
    new: Mode
    reason: str
    sim_time: float

    def __str__(self) -> str:  # pragmatic: keep payload small and readable
        return f"{self.old.value} -> {self.new.value} ({self.reason})"


class ModeController:
    """Enforces the mode state machine and records a change history."""

    def __init__(self, initial: Mode = Mode.BOOT) -> None:
        self._mode = initial
        self._history: list[ModeChange] = []
        if initial is not Mode.BOOT:
            self._history.append(
                ModeChange(Mode.BOOT, initial, "initialized directly", 0.0))

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def history(self) -> list[ModeChange]:
        return list(self._history)

    def can_transition(self, target: Mode) -> bool:
        """True if a transition from the current mode to *target* is legal."""
        if target is self._mode:
            return True
        return any(t for t, _ in VALID_TRANSITIONS.get(self._mode, []) if t is target)

    def transition(self, target: Mode, reason: str, sim_time: float) -> ModeChange:
        """Apply a mode change or raise :class:`InvalidModeTransition`."""
        if target is self._mode:
            raise InvalidModeTransition(
                f"already in {target.value}")
        legal = VALID_TRANSITIONS.get(self._mode, [])
        if not any(t is target for t, _ in legal):
            raise InvalidModeTransition(
                f"invalid transition {self._mode.value} -> {target.value}")
        change = ModeChange(
            old=self._mode, new=target, reason=reason, sim_time=sim_time)
        self._mode = target
        self._history.append(change)
        return change

    def to_safe(self, reason: str, sim_time: float) -> ModeChange:
        """The dedicated critical-fault path into SAFE mode."""
        if self._mode is Mode.SAFE:
            raise InvalidModeTransition("already in SAFE")
        if self._mode is Mode.BOOT:
            # During BOOT a critical fault keeps us in boot, then boots to SAFE.
            raise InvalidModeTransition(
                "BOOT cannot drop to SAFE; boot completes to INIT first")
        old = self._mode
        self._mode = Mode.SAFE
        change = ModeChange(
            old=old, new=Mode.SAFE, reason=reason, sim_time=sim_time)
        self._history.append(change)
        return change