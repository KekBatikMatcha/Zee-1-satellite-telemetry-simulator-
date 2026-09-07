"""Telecommand definitions, validation, and mode-aware authorization.

Terminology
-----------
Telecommand = structured control message sent GROUND -> SATELLITE.
Telemetry   = state/health messages sent SATELLITE -> GROUND.

Commands are never free-form code. Each command has a registry entry that
declares its parameters and the spacecraft modes in which it is allowed, so
authorization is data, not an if/else soup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field, fields
from typing import Any

from satellite.modes import Mode

ORBITAL_BURN_LIMIT_MPS = 200.0
ATTITUDE_OPTIONS = ["NADIR", "SUN_POINTING", "INERTIAL"]


@dataclass
class CommandSpec:
    name: str
    description: str
    parameters: list[str]
    allowed_modes: list[str]
    requires_param_modes: bool = False
    param_schema: dict[str, Any] = field(default_factory=dict)


COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "SET_MODE": CommandSpec(
        name="SET_MODE",
        description="Switch the spacecraft to a new operating mode.",
        parameters=["mode"],
        allowed_modes=[m.value for m in Mode],
        param_schema={"mode": {"type": "string", "enum": [m.value for m in Mode]}},
    ),
    "REQUEST_TELEMETRY": CommandSpec(
        name="REQUEST_TELEMETRY",
        description="Request an immediate housekeeping/telemetry downlink.",
        parameters=[],
        allowed_modes=[m.value for m in Mode] + [Mode.SAFE.value],
        requires_param_modes=False,
    ),
    "START_PAYLOAD": CommandSpec(
        name="START_PAYLOAD",
        description="Activate the mission payload (go to PAYLOAD_OPERATION).",
        parameters=[],
        allowed_modes=[Mode.NORMAL.value, Mode.PAYLOAD_OPERATION.value],
    ),
    "STOP_PAYLOAD": CommandSpec(
        name="STOP_PAYLOAD",
        description="Deactivate the mission payload (return to NORMAL).",
        parameters=[],
        allowed_modes=[Mode.PAYLOAD_OPERATION.value, Mode.NORMAL.value],
    ),
    "ORBITAL_BURN": CommandSpec(
        name="ORBITAL_BURN",
        description="Fire along the velocity vector to raise (+) or lower (-) "
                    "the orbit, shifting future pass times over the station.",
        parameters=["delta_v_mps"],
        allowed_modes=[Mode.NORMAL.value, Mode.PAYLOAD_OPERATION.value,
                       Mode.COMMUNICATION.value],
        param_schema={"delta_v_mps": {
            "type": "number",
            "min": -ORBITAL_BURN_LIMIT_MPS,
            "max": ORBITAL_BURN_LIMIT_MPS,
        }},
    ),
    "ADJUST_ATTITUDE": CommandSpec(
        name="ADJUST_ATTITUDE",
        description="Point the spacecraft boresight: NADIR, SUN_POINTING or "
                    "INERTIAL (affects solar power collection).",
        parameters=["attitude"],
        allowed_modes=[Mode.NORMAL.value, Mode.PAYLOAD_OPERATION.value,
                       Mode.COMMUNICATION.value],
        param_schema={"attitude": {"type": "string", "enum": ATTITUDE_OPTIONS}},
    ),
    "RESET_SIMULATION": CommandSpec(
        name="RESET_SIMULATION",
        description="Reset the simulation (educational-only).",
        parameters=[],
        allowed_modes=[m.value for m in Mode],
    ),
}


@dataclass
class CommandRequest:
    """The logical command object before signing/packetizing."""

    satellite_id: str
    command: str
    parameters: dict[str, Any]
    timestamp: float
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandRequest":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class CommandError(Exception):
    """Base for command validation/authorization failures."""


class StructuralCommandError(CommandError):
    """Malformed command (missing fields, wrong types)."""


class UnknownCommandError(CommandError):
    """Command name not in the registry."""


class CommandNotAuthorizedError(CommandError):
    """Command not allowed in the spacecraft's current mode."""


def _validate_structure(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise StructuralCommandError("command must be a JSON object")
    required = {"satellite_id", "command", "timestamp", "sequence",
                "parameters"}
    missing = required - set(data)
    if missing:
        raise StructuralCommandError(f"missing fields: {sorted(missing)}")
    for key in ("satellite_id", "command"):
        if not isinstance(data[key], str) or not data[key]:
            raise StructuralCommandError(f"'{key}' must be a non-empty string")
    if not isinstance(data.get("parameters"), dict):
        raise StructuralCommandError("'parameters' must be an object")
    if not isinstance(data.get("sequence"), int):
        raise StructuralCommandError("'sequence' must be an integer")
    if not isinstance(data.get("timestamp"), (int, float)):
        raise StructuralCommandError("'timestamp' must be a number")


def validate_command(req: CommandRequest) -> CommandSpec:
    """Structure + registry validation. Returns the spec on success."""
    spec = COMMAND_REGISTRY.get(req.command)
    if spec is None:
        raise UnknownCommandError(
            f"unknown command '{req.command}'; known: {sorted(COMMAND_REGISTRY)}")
    for param in spec.parameters:
        if param not in req.parameters:
            raise StructuralCommandError(
                f"command {req.command} requires parameter '{param}'")
    return spec


def authorize_command(req: CommandRequest, current_mode: str) -> CommandSpec:
    """Authorize a structurally valid command in the given spacecraft mode."""
    spec = validate_command(req)
    if current_mode not in spec.allowed_modes:
        raise CommandNotAuthorizedError(
            f"{req.command} not allowed in mode {current_mode} "
            f"(allowed: {spec.allowed_modes})")
    return spec


def apply_parameter(value: Any, param_schema: dict[str, Any]) -> Any:
    """Validate a single parameter against its declarative schema."""
    if not param_schema:
        return value
    ptype = param_schema.get("type")
    if ptype == "string":
        if not isinstance(value, str):
            raise StructuralCommandError("parameter must be a string")
        enum_vals = param_schema.get("enum")
        if enum_vals and value not in enum_vals:
            raise StructuralCommandError(
                f"parameter must be one of {enum_vals}, got {value!r}")
    elif ptype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StructuralCommandError("parameter must be a number")
        vmin, vmax = param_schema.get("min"), param_schema.get("max")
        if vmin is not None and value < vmin:
            raise StructuralCommandError(f"parameter must be >= {vmin}")
        if vmax is not None and value > vmax:
            raise StructuralCommandError(f"parameter must be <= {vmax}")
    return value


def validate_parameters(req: CommandRequest, spec: CommandSpec) -> None:
    for param, schema in spec.param_schema.items():
        if param in req.parameters:
            apply_parameter(req.parameters[param], schema)
        else:
            raise StructuralCommandError(
                f"command {req.command} requires parameter '{param}'")


def new_command(
    satellite_id: str,
    command: str,
    parameters: dict[str, Any] | None = None,
    sequence: int = 1,
    timestamp: float | None = None,
) -> CommandRequest:
    return CommandRequest(
        satellite_id=satellite_id,
        command=command,
        parameters=parameters or {},
        timestamp=timestamp if timestamp is not None else time.time(),
        sequence=sequence,
    )