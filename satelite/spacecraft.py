"""Zee-1 simulated spacecraft: state, telemetry production, dispatch.

This is the heart of the *space segment*. The spacecraft owns its subsystem
state, operating-mode state machine, orbit, onboard storage, and the
telemetry manager that converts raw state into structured telemetry records.
It does not know about SQLite, HTTP, or the dashboard; it emits telemetry
frames into a link via a small callback (dependency injection), so the
simulation can swap the transport (socket, in-memory, UDP) without touching
the spacecraft code.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from communication.visibility import (
    GroundStationPosition,
    Visibility,
    compute_visibility,
    ground_station_position,
)
from config import SimulationConfig
from satellite.modes import Mode, ModeController, InvalidModeTransition
from satellite.orbit import OrbitModel
from satellite.storage import OnboardStorage
from satellite.subsystems import ADCS, CommSubsystem, EPS, OBC, Payload, Thermal
from simulation.events import EventBus, Severity
from simulation.faults import FaultController
from telemetry.schema import (
    TelemetryCategory,
    TelemetryPriority,
    TelemetryRecord,
    now_unix,
)

logger = logging.getLogger("satellite")

BOOT_DURATION_S = 3.0    # BOOT -> INIT after this much simulated time
INIT_DURATION_S = 5.0    # INIT -> NORMAL after this much simulated time
BATTERY_SAFE_THRESHOLD = 15.0
DOWNLINK_DRAIN_PER_TICK = 30   # max buffered packets dumped per tick on contact
ATTITUDE_OPTIONS = ("NADIR", "SUN_POINTING", "INERTIAL")
INERTIAL_SUNLIGHT_FACTOR = 0.6


@dataclass
class SpacecraftStatus:
    """Snapshot of the whole spacecraft for the API/dashboard."""

    satellite_id: str
    mode: str
    sim_time: float
    mission_time_s: float
    communication_status: str
    visibility: bool
    elevation_deg: float
    payload_status: str
    faults: list[str] = field(default_factory=list)
    onboard_queue: dict[str, Any] = field(default_factory=dict)


class Spacecraft:
    """Orchestrates subsystems, modes, position, storage, and telemetry."""

    def __init__(
        self,
        config: SimulationConfig,
        event_bus: EventBus,
        faults: FaultController,
        transmit_cb: Callable[[bytes], None] | None = None,
    ) -> None:
        self.config = config
        self.events = event_bus
        self.faults = faults
        self.transmit_cb = transmit_cb

        self.sim_time = 0.0
        self.sequence = 1001                      # packet sequence numbering
        self.rng = random.Random(config.simulation_seed)
        self.last_record: TelemetryRecord | None = None

        self.modes = ModeController(Mode.BOOT)
        self.orbit = OrbitModel(
            altitude_km=config.orbit_altitude_km,
            inclination_deg=config.orbit_inclination_deg,
            raan_deg=config.orbit_raan_deg,
            initial_phase_deg=config.orbit_initial_phase_deg,
        )
        self.eps = EPS(battery_percent=85.0)
        self.obc = OBC()
        self.adcs = ADCS()
        self.thermal = Thermal()
        self.payload = Payload()
        self.comm = CommSubsystem()
        self.storage = OnboardStorage(capacity=config.onboard_capacity_packets)

        self.station = ground_station_position(
            latitude=config.ground_station_latitude,
            longitude=config.ground_station_longitude,
            min_elevation=config.min_elevation_deg,
            name=config.ground_station_id,
        )
        self.command_secret = config.satellite_command_secret
        self.attitude_target = "NADIR"
        from security.replay import ReplayGuard
        self.command_guard = ReplayGuard()
        self.command_results: dict[int, dict[str, Any]] = {}
        self._position: dict[str, float] | None = None
        self._visibility: Visibility | None = None
        self._boot_elapsed = 0.0
        self._init_elapsed = 0.0

    # ------------------------------------------------------------------
    # Mode handling
    # ------------------------------------------------------------------
    def request_mode(self, mode: Mode, reason: str) -> dict[str, str]:
        """Public mode-change request (used by telecommands / API)."""
        try:
            change = self.modes.transition(mode, reason, self.sim_time)
        except InvalidModeTransition as exc:
            self.events.emit_event(
                "INVALID_MODE_TRANSITION", Severity.WARNING.value,
                str(exc), source="SATELLITE", sim_time=self.sim_time)
            raise
        self._emit_mode_change(change.old, change.new, reason)
        return {"mode": change.new.value, "reason": reason}

    def _emit_mode_change(self, old: Mode, new: Mode, reason: str) -> None:
        self.events.emit_event(
            "MODE_CHANGED", Severity.INFO.value,
            f"{old.value} -> {new.value}: {reason}",
            source="SATELLITE", sim_time=self.sim_time)

    def _safety_transition(self, reason: str) -> None:
        """Critical-fault path into SAFE mode (logs and emits)."""
        try:
            change = self.modes.to_safe(reason, self.sim_time)
        except InvalidModeTransition:
            return  # e.g. already SAFE or still BOOT
        self._emit_mode_change(change.old, change.new, reason)

    # ------------------------------------------------------------------
    # Simulation tick
    # ------------------------------------------------------------------
    def tick(self, dt_s: float) -> list[TelemetryRecord]:
        """Advance the spacecraft one simulation step, return produced frames."""
        self.sim_time += dt_s
        self._boot_progression(dt_s)

        position = self.orbit.position_at(self.sim_time)
        self._position = position
        self._visibility = compute_visibility(
            self.station,
            position["latitude"],
            position["longitude"],
            position["altitude_km"],
        )
        visible = self._visibility.visible

        self._apply_fault_state()

        payload_active = self.payload.active or self.modes.mode is Mode.PAYLOAD_OPERATION
        self.comm.update(self.modes.mode.value, visible)
        transmitting = self.comm.transmitting

        # Attitude drives how much of the available sunlight the panels catch.
        sunlight = position["sunlight"]
        if self.attitude_target == "SUN_POINTING":
            sunlight = 1.0
        elif self.attitude_target == "INERTIAL":
            sunlight *= INERTIAL_SUNLIGHT_FACTOR

        self.eps.update(dt_s, self.modes.mode.value, sunlight,
                        payload_active, transmitting)
        self.thermal.update(dt_s, self.modes.mode.value, sunlight)
        self.obc.update(dt_s, self.modes.mode.value, self.rng, payload_active)
        self.adcs.update(dt_s, self.rng)
        self.payload.update(self.modes.mode.value, dt_s)

        self._check_thresholds()

        records = self.produce_telemetry()
        self._dispatch(records, visible)
        return records

    def _boot_progression(self, dt_s: float) -> None:
        if self.modes.mode is Mode.BOOT:
            self._boot_elapsed += dt_s
            if self._boot_elapsed >= BOOT_DURATION_S:
                self.modes.transition(Mode.INIT, "boot sequence completed",
                                      self.sim_time)
                self._emit_mode_change(Mode.BOOT, Mode.INIT, "boot sequence completed")
                self.events.emit_event(
                    "SYSTEM_INITIALIZED", Severity.INFO.value,
                    "OBC boot loader complete, entering INIT",
                    source="SATELLITE", sim_time=self.sim_time)
        elif self.modes.mode is Mode.INIT:
            self._init_elapsed += dt_s
            if self._init_elapsed >= INIT_DURATION_S:
                ok = self._subsystem_self_checks()
                target = Mode.NORMAL if ok else Mode.SAFE
                reason = ("subsystem checks passed" if ok
                          else "subsystem check failure during INIT")
                self.modes.transition(target, reason, self.sim_time)
                self._emit_mode_change(Mode.INIT, target, reason)
                if ok:
                    self.events.emit_event(
                        "SYSTEM_INITIALIZED", Severity.INFO.value,
                        "Zee-1 initialized and nominal",
                        source="SATELLITE", sim_time=self.sim_time)

    def _subsystem_self_checks(self) -> bool:
        """Simplified EPS/OBC power-on self test (POST)."""
        checks = [
            self.eps.battery_percent > 5.0,
            self.obc.cpu_usage < 99.0,
            self.thermal.temperature_c < self.config.anomaly_temperature_high,
        ]
        return all(checks)

    def _check_thresholds(self) -> None:
        """Autonomous FDIR-lite: degrade to SAFE on critical thresholds."""
        if self.modes.mode in (Mode.BOOT, Mode.INIT, Mode.SAFE):
            return
        if self.eps.battery_percent < BATTERY_SAFE_THRESHOLD:
            self.events.emit_event(
                "LOW_BATTERY", Severity.CRITICAL.value,
                f"battery {self.eps.battery_percent:.1f}% below "
                f"{BATTERY_SAFE_THRESHOLD}%",
                source="SATELLITE", sim_time=self.sim_time)
            self._safety_transition("LOW_BATTERY: battery critical")
        elif self.thermal.temperature_c > self.config.anomaly_temperature_high:
            self.events.emit_event(
                "HIGH_TEMPERATURE", Severity.CRITICAL.value,
                f"bus temperature {self.thermal.temperature_c:.1f} C above "
                f"{self.config.anomaly_temperature_high} C",
                source="SATELLITE", sim_time=self.sim_time)
            self._safety_transition("HIGH_TEMPERATURE: bus overheating")

    # ------------------------------------------------------------------
    # Fault state (drives subsystem overrides)
    # ------------------------------------------------------------------
    def _apply_fault_state(self) -> None:
        self.eps.extra_load_w = 20.0 if self.faults.is_active("LOW_BATTERY") else 0.0
        self.eps.solar_derating = (
            0.3 if self.faults.is_active("SOLAR_ARRAY_FAILURE") else 1.0)
        self.thermal.target_offset_c = (
            30.0 if self.faults.is_active("HIGH_TEMPERATURE") else 0.0)
        self.obc.extra_cpu_pct = (
            45.0 if self.faults.is_active("HIGH_CPU") else 0.0)
        self.comm.status = (
            "FAULT" if self.faults.is_active("COMMS_OUTAGE") else self.comm.status)
        if self.faults.is_active("STORAGE_FULL"):
            self.storage.capacity = min(self.storage.capacity, 4)
        else:
            self.storage.capacity = self.config.onboard_capacity_packets

    # ------------------------------------------------------------------
    # Telemetry manager
    # ------------------------------------------------------------------
    def _critical_condition(self) -> bool:
        if bool(self.faults.active_faults()):
            return True
        if self.modes.mode is Mode.SAFE:
            return True
        return (self.eps.battery_percent < BATTERY_SAFE_THRESHOLD or
                self.thermal.temperature_c > self.config.anomaly_temperature_high)

    def produce_telemetry(self) -> list[TelemetryRecord]:
        """Build telemetry records with sequence numbers and priorities."""
        assert self._position is not None
        pos = self._position
        vis = self._visibility
        assert vis is not None

        priority = (TelemetryPriority.CRITICAL if self._critical_condition()
                    else TelemetryPriority.NORMAL)
        sensor_noise = self.faults.is_active("SENSOR_ANOMALY")
        bat_pct = self.eps.battery_percent
        temp_c = self.thermal.temperature_c
        if sensor_noise:
            bat_pct = max(0.0, min(100.0, bat_pct + self.rng.gauss(0.0, 12.0)))
            temp_c = temp_c + self.rng.gauss(0.0, 8.0)

        record = self._build_record(
            category=TelemetryCategory.HOUSEKEEPING,
            priority=priority,
            battery_pct=bat_pct,
            temp_c=temp_c,
        )
        self.last_record = record
        frames = [record]

        if self.payload.active:
            payload_record = self._build_record(
                category=TelemetryCategory.PAYLOAD,
                priority=TelemetryPriority.LOW,
                battery_pct=bat_pct,
                temp_c=temp_c,
            )
            payload_record.payload_status = self.payload.status
            payload_record.storage_usage = self.obc.storage_usage
            frames.append(payload_record)
        return frames

    def _build_record(
        self,
        category: TelemetryCategory,
        priority: TelemetryPriority,
        battery_pct: float,
        temp_c: float,
    ) -> TelemetryRecord:
        vis = self._visibility
        record = TelemetryRecord(
            satellite_id=self.config.satellite_id,
            mission_time=self.sim_time,
            timestamp=now_unix(),
            sequence_number=self.sequence,
            category=category.name,
            priority=priority.name,
            operating_mode=self.modes.mode.value,
            battery_percentage=round(battery_pct, 2),
            battery_voltage=round(self.eps.battery_voltage, 2),
            solar_power=round(self.eps.solar_power, 2),
            power_consumption=round(self.eps.power_consumption, 2),
            temperature_c=round(temp_c, 2),
            cpu_usage=round(self.obc.cpu_usage, 2),
            memory_usage=round(self.obc.memory_usage, 2),
            storage_usage=round(self.obc.storage_usage, 2),
            latitude=self._position["latitude"],
            longitude=self._position["longitude"],
            altitude_km=self._position["altitude_km"],
            velocity_kms=self._position["velocity_kms"],
            attitude_roll=round(self.adcs.roll, 2),
            attitude_pitch=round(self.adcs.pitch, 2),
            attitude_yaw=round(self.adcs.yaw, 2),
            communication_status=self.comm.status,
            visibility=vis.state,
            onboard_queue_count=len(self.storage),
            payload_status=self.payload.status,
        )
        self.sequence += 1
        return record

    # ------------------------------------------------------------------
    # Dispatch (downlink vs. store-and-forward)
    # ------------------------------------------------------------------
    def _dispatch(self, records: list[TelemetryRecord], visible: bool) -> None:
        for record in records:
            if visible and self.transmit_cb is not None:
                self.transmit_cb(self.packetize(record))
            elif self.config.buffer_when_not_visible:
                self.storage.enqueue(
                    TelemetryPriority[record.priority],
                    record.sequence_number, record)

        # On contact, dump buffered telemetry (store-and-forward).
        if visible and self.config.downlink_buffer_on_contact:
            for record in self.storage.drain(DOWNLINK_DRAIN_PER_TICK):
                if self.transmit_cb is not None:
                    self.transmit_cb(self.packetize(record))

    def packetize(self, record: TelemetryRecord) -> bytes:
        """Wrap a record into a framed link packet (see telemetry.packet)."""
        from telemetry.packet import build_packet, encode_payload
        from telemetry.schema import PacketType, TelemetryCategory, TelemetryPriority
        return build_packet(
            satellite_id=self.config.satellite_id,
            packet_type=PacketType.TELEMETRY,
            category=TelemetryCategory[record.category],
            priority=TelemetryPriority[record.priority],
            sequence_number=record.sequence_number,
            timestamp=record.mission_time,
            payload=encode_payload(record),
        )

    # ------------------------------------------------------------------
    # Uplink (telecommand execution with full security gate)
    # ------------------------------------------------------------------
    def on_uplink(self, data: bytes) -> dict[str, Any]:
        """An uplink frame arrived from the ground; verify and maybe execute."""
        from telemetry.packet import (
            PacketError,
            PacketType,
            decode_payload,
            parse_packet,
        )
        try:
            packet = parse_packet(data, verify_crc=True)
        except PacketError as exc:
            self.events.emit_event(
                "INVALID_PACKET", Severity.WARNING.value,
                f"corrupt/invalid uplink frame: {exc}",
                source="SECURITY", sim_time=self.sim_time)
            return {"accepted": False, "reason": str(exc)}

        if packet.packet_type is not PacketType.COMMAND:
            self.events.emit_event(
                "INVALID_PACKET", Severity.WARNING.value,
                f"uplink frame is not a command (type {packet.packet_type.name})",
                source="SECURITY", sim_time=self.sim_time)
            return {"accepted": False, "reason": "not a command frame"}

        try:
            payload = decode_payload(packet.payload)
        except PacketError as exc:
            return {"accepted": False, "reason": str(exc)}
        return self.handle_command(payload)

    def handle_command(self, signed: dict[str, Any]) -> dict[str, Any]:
        """The full ground-to-space security gate, in documented order:
        1 structure, 2 source, 3 authentication, 4 replay, 5 authorization,
        6 execution. Returns an ack/reject dict emitted as a security event.

        The order matters: reject closed-early, so a forged command never
        passes further than the check it fails.
        """
        from security.commands import (
            CommandRequest,
            CommandNotAuthorizedError,
            StructuralCommandError,
            UnknownCommandError,
            authorize_command,
            validate_command,
        )
        from security.commands_auth import verify_signature

        # 1. Structure
        try:
            cmd = CommandRequest.from_dict(signed)
            validate_command(cmd)
        except StructuralCommandError as exc:
            self._security_event("INVALID_COMMAND", Severity.HIGH.value,
                                 f"malformed command: {exc}")
            return {"accepted": False, "reason": f"structural: {exc}"}
        except UnknownCommandError as exc:
            self._security_event("INVALID_COMMAND", Severity.HIGH.value, str(exc))
            return {"accepted": False, "reason": str(exc)}

        # 2. Source
        if cmd.satellite_id != self.config.satellite_id:
            self._security_event(
                "UNAUTHORIZED_COMMAND", Severity.HIGH.value,
                f"command for '{cmd.satellite_id}', this is "
                f"'{self.config.satellite_id}'")
            return {"accepted": False, "reason": "wrong spacecraft"}

        # 3. Authentication (HMAC-SHA256)
        ok, reason = verify_signature(signed, self.command_secret)
        if not ok:
            self._security_event(
                "AUTHENTICATION_FAILURE", Severity.HIGH.value,
                f"seq {cmd.sequence} {cmd.command}: {reason}")
            return {"accepted": False, "reason": f"auth: {reason}"}

        # 4. Replay protection
        if self.command_guard.is_replay(cmd.satellite_id, cmd.sequence):
            self.command_guard.observe_replay(cmd.satellite_id, cmd.sequence)
            self._security_event(
                "REPLAY_ATTACK_DETECTED", Severity.CRITICAL.value,
                f"replayed command seq {cmd.sequence} ({cmd.command}) rejected")
            return {"accepted": False, "reason": "replay rejected"}

        # 5. Authorization (mode-aware)
        try:
            authorize_command(cmd, self.modes.mode.value)
        except CommandNotAuthorizedError as exc:
            self._security_event(
                "UNAUTHORIZED_COMMAND", Severity.HIGH.value,
                f"seq {cmd.sequence} {cmd.command}: {exc}")
            # Record the rejection so ops can see *why* a command did not
            # run instead of it staying "pending" forever.
            result = {"accepted": False, "executed": False,
                      "reason": f"not authorized: {exc}"}
            self.command_results[cmd.sequence] = result
            return result

        # Mark sequence as consumed before execution.
        self.command_guard.record(cmd.satellite_id, cmd.sequence)

        # 6. Execute
        result = self._execute_command(cmd)
        self.command_results[cmd.sequence] = result
        self._security_event(
            "TELECOMMAND_ACCEPTED" if result["executed"] else "TELECOMMAND_REJECTED",
            Severity.INFO.value if result["executed"] else Severity.WARNING.value,
            f"seq {cmd.sequence} {cmd.command} -> "
            f"{'executed' if result['executed'] else 'rejected'}: {result.get('message','')}")
        result["accepted"] = True
        result["sequence"] = cmd.sequence
        result["command"] = cmd.command
        return result

    def _security_event(self, event_type: str, severity: str, message: str) -> None:
        self.events.emit_event(event_type, severity, message,
                               source="SECURITY", sim_time=self.sim_time)

    def _execute_command(self, cmd: Any) -> dict[str, Any]:
        from satellite.modes import Mode, InvalidModeTransition
        mode_name = cmd.parameters.get("mode")
        try:
            if cmd.command == "SET_MODE":
                target = Mode(mode_name)
                if target is self.modes.mode:
                    return {"executed": False, "message": "already in mode"}
                change = self.modes.transition(target, "telecommand", self.sim_time)
                self._emit_mode_change(change.old, change.new, "telecommand")
                return {"executed": True, "message": f"mode -> {target.value}"}
            if cmd.command == "REQUEST_TELEMETRY":
                records = self.produce_telemetry()
                visible = self._visibility.visible if self._visibility else False
                self._dispatch(records, visible)
                return {"executed": True,
                        "message": f"downlinked {len(records)} frame(s)"}
            if cmd.command == "START_PAYLOAD":
                return self._command_transition(Mode.PAYLOAD_OPERATION,
                                                "telecommand: start payload")
            if cmd.command == "STOP_PAYLOAD":
                return self._command_transition(Mode.NORMAL,
                                                "telecommand: stop payload")
            if cmd.command == "ORBITAL_BURN":
                dv = cmd.parameters.get("delta_v_mps")
                try:
                    orb = self.orbit.apply_burn(float(dv), self.sim_time)
                except (ValueError, TypeError) as exc:
                    return {"executed": False, "message": str(exc)}
                self.events.emit_event(
                    "MANEUVER_COMPLETED", Severity.INFO.value,
                    f"ORBITAL_BURN {orb['delta_v_mps']} m/s -> "
                    f"altitude {orb['new_altitude_km']:.0f} km, "
                    f"period {orb['new_period_s']:.0f} s, "
                    f"v {orb['new_velocity_kms']:.3f} km/s",
                    source="SATELLITE", sim_time=self.sim_time)
                return {"executed": True,
                        "message": f"orbit -> {orb['new_altitude_km']:.0f} km, "
                                   f"period {orb['new_period_s']:.0f} s"}
            if cmd.command == "ADJUST_ATTITUDE":
                target = cmd.parameters.get("attitude")
                if target not in ATTITUDE_OPTIONS:
                    return {"executed": False,
                            "message": f"unknown attitude '{target}'"}
                if target == self.attitude_target:
                    return {"executed": False, "message": "already in that attitude"}
                self.attitude_target = target
                self.events.emit_event(
                    "ATTITUDE_CHANGED", Severity.INFO.value,
                    f"attitude -> {target}",
                    source="SATELLITE", sim_time=self.sim_time)
                return {"executed": True, "message": f"attitude -> {target}"}
            if cmd.command == "RESET_SIMULATION":
                return {"executed": False,
                        "message": "handled by mission control, not on-board"}
        except InvalidModeTransition as exc:
            self._security_event(
                "INVALID_MODE_TRANSITION", Severity.WARNING.value, str(exc))
            return {"executed": False, "message": str(exc)}
        except ValueError:
            return {"executed": False,
                    "message": f"unknown mode '{mode_name}'"}
        return {"executed": False, "message": f"unsupported command {cmd.command}"}

    def _command_transition(self, target: Mode, reason: str) -> dict[str, Any]:
        from satellite.modes import InvalidModeTransition
        if target is self.modes.mode:
            return {"executed": True, "message": f"already in {target.value}"}
        change = self.modes.transition(target, reason, self.sim_time)
        self._emit_mode_change(change.old, change.new, reason)
        return {"executed": True, "message": f"mode -> {target.value}"}

    # ------------------------------------------------------------------
    # Status snapshots
    # ------------------------------------------------------------------
    def status(self) -> SpacecraftStatus:
        return SpacecraftStatus(
            satellite_id=self.config.satellite_id,
            mode=self.modes.mode.value,
            sim_time=self.sim_time,
            mission_time_s=self.sim_time,
            communication_status=self.comm.status,
            visibility=(self._visibility.visible if self._visibility else False),
            elevation_deg=(self._visibility.elevation_deg if self._visibility else 0.0),
            payload_status=self.payload.status,
            faults=list(self.faults.active_faults()),
            onboard_queue=self.storage.stats(),
        )

    def state_snapshot(self) -> dict[str, Any]:
        """All values needed by /api/satellite/status and the dashboard."""
        pos = self._position or self.orbit.position_at(self.sim_time)
        vis = self._visibility
        return {
            "satellite_id": self.config.satellite_id,
            "sim_time": round(self.sim_time, 2),
            "mission_time_s": round(self.sim_time, 2),
            "operating_mode": self.modes.mode.value,
            "temperature_c": round(self.thermal.temperature_c, 2),
            "battery_percentage": round(self.eps.battery_percent, 2),
            "battery_voltage": round(self.eps.battery_voltage, 2),
            "solar_power": round(self.eps.solar_power, 2),
            "power_consumption": round(self.eps.power_consumption, 2),
            "cpu_usage": round(self.obc.cpu_usage, 2),
            "memory_usage": round(self.obc.memory_usage, 2),
            "storage_usage": round(self.obc.storage_usage, 2),
            "latitude": pos["latitude"],
            "longitude": pos["longitude"],
            "altitude_km": pos["altitude_km"],
            "velocity_kms": pos["velocity_kms"],
            "orbit_altitude_km": round(self.orbit.altitude_km, 2),
            "orbit_period_s": round(self.orbit.period_s, 1),
            "attitude_target": self.attitude_target,
            "attitude_roll": round(self.adcs.roll, 2),
            "attitude_pitch": round(self.adcs.pitch, 2),
            "attitude_yaw": round(self.adcs.yaw, 2),
            "communication_status": self.comm.status,
            "payload_status": self.payload.status,
            "visibility": vis.state if vis else "NOT_VISIBLE",
            "elevation_deg": vis.elevation_deg if vis else 0.0,
            "active_faults": list(self.faults.active_faults()),
            "onboard_storage": self.storage.stats(),
        }

    def reset(self) -> None:
        """Re-create the spacecraft from scratch (used by reset endpoints)."""
        faults = self.faults
        events = self.events
        cb = self.transmit_cb
        self.__init__(self.config, events, faults, cb)