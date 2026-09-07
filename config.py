"""Central configuration for the Satellite Telemetry Simulator.

All tunable values live here. They are read from environment variables
(with `.env` support) and fall back to safe, documented defaults.

Design notes
------------
* No configuration values are scattered through the codebase.
* Secrets are NEVER hard-coded here. `SATELLITE_COMMAND_SECRET` and
  `API_TOKEN` come from the environment; a warning is emitted when the
  development defaults are used.
* Values are validated at import time so misconfiguration fails fast and
  loudly instead of producing silent nonsense later.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("simulator.config")

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = ROOT_DIR / ".env"


# --------------------------------------------------------------------------
# Minimal .env loader (stdlib only — avoids a python-dotenv dependency).
# --------------------------------------------------------------------------

def load_dotenv(path: Optional[Path] = None) -> None:
    """Load ``KEY=VALUE`` lines from *path* into the environment if not set.

    Handles comments, blank lines, and optional surrounding quotes. Values in
    the real environment always win over the file (no overwrite).
    """
    env_file = path or DEFAULT_ENV_PATH
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(key: str, default: bool) -> bool:
    """Parse a boolean from the environment with a sane default."""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(key: str, default: float, minimum: Optional[float] = None,
              maximum: Optional[float] = None) -> float:
    val = float(os.getenv(key, str(default)))
    if minimum is not None and val < minimum:
        raise ValueError(f"{key}: expected >= {minimum}, got {val}")
    if maximum is not None and val > maximum:
        raise ValueError(f"{key}: expected <= {maximum}, got {val}")
    return val


def env_int(key: str, default: int, minimum: Optional[int] = None,
            maximum: Optional[int] = None) -> int:
    val = int(os.getenv(key, str(default)))
    if minimum is not None and val < minimum:
        raise ValueError(f"{key}: expected >= {minimum}, got {val}")
    if maximum is not None and val > maximum:
        raise ValueError(f"{key}: expected <= {maximum}, got {val}")
    return val


# --------------------------------------------------------------------------
# Defaults (mirrored in .env.example)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SimulationConfig:
    # --- Identity / mission -------------------------------------------------
    satellite_id: str = "Zee-1"

    # --- Simulation timing --------------------------------------------------
    sim_tick_ms: int = 1000          # wall-clock interval between simulation ticks
    simulation_speed: float = 1.0    # simulated seconds per real second
    simulation_seed: int = 42        # reproducibility of stochastic models

    # --- Telemetry ----------------------------------------------------------
    telemetry_rate_hz: float = 1.0   # telemetry frames produced per simulated second

    # --- Space link ---------------------------------------------------------
    packet_loss_rate: float = 0.02
    # Extra interference bursts on top of the physical AWGN channel (see
    # communication/rf_channel.py). 0.0 = corruption comes from the noise
    # realization alone.
    packet_corruption_rate: float = 0.0
    link_latency_ms: float = 300.0
    link_jitter_ms: float = 50.0
    link_bandwidth_bps: int = 9600   # typical UHF CubeSat data rate
    link_eb_n0_db: float = 10.0      # BPSK/AWGN link budget (BER ~5e-6)
    onboard_capacity_packets: int = 1024

    # --- Ground station -----------------------------------------------------
    ground_station_id: str = "KUCHING-GS"
    ground_station_latitude: float = 1.5533
    ground_station_longitude: float = 110.3592
    min_elevation_deg: float = 10.0

    # --- Orbit (simplified circular LEO) ------------------------------------
    orbit_altitude_km: float = 525.0
    orbit_inclination_deg: float = 51.6
    orbit_raan_deg: float = 110.0     # ascending node over Kuching (~110E)
    orbit_initial_phase_deg: float = 2.0

    # --- Database -----------------------------------------------------------
    database_name: str = "telemetry.db"       # inside data/, see database_url

    # --- Security -----------------------------------------------------------
    satellite_command_secret: str = "dev-secret-change-me"     # OVERRIDE in env
    api_token: str = "dev-dashboard-token-change-me"           # OVERRIDE in env
    allow_unauthenticated_dashboard: bool = False

    # --- Beacon/contact policy -----------------------------------------------
    buffer_when_not_visible: bool = True
    downlink_buffer_on_contact: bool = True   # flush onboard queue on contact

    # --- Anomaly thresholds (rule-based detector) ---------------------------
    anomaly_temperature_high: float = 70.0
    anomaly_battery_low: float = 15.0
    anomaly_battery_critical: float = 5.0
    anomaly_battery_drop_rate: float = 5.0    # % drop per simulated minute
    anomaly_cpu_high: float = 95.0

    # --- Fault injection -----------------------------------------------------
    faults_enabled: bool = True

    # --- Logging ------------------------------------------------------------
    log_level: str = "INFO"

    # --- Derived helpers -----------------------------------------------------
    @property
    def database_path(self) -> Path:
        return ROOT_DIR / "data" / self.database_name

    @property
    def database_url(self) -> str:
        # sqlite:/// absolute path works for both SQLAlchemy and sqlite3.
        return f"sqlite:///{self.database_path.as_posix()}"

    def as_dict(self) -> dict[str, Any]:
        """Public configuration snapshot (no secrets)."""
        secrets = {
            "satellite_command_secret",
            "api_token",
        }
        return {
            k: ("<redacted>" if k in secrets else v)
            for k, v in self.__dict__.items()
        }


def _build_config() -> SimulationConfig:
    load_dotenv()

    # Validate timing sanity.
    tick_ms = env_int("SIM_TICK_MS", 1000, minimum=50)
    speed = env_float("SIMULATION_SPEED", 1.0, minimum=0.0)
    rate_hz = env_float("TELEMETRY_RATE_HZ", 1.0, minimum=0.1, maximum=10.0)

    config = SimulationConfig(
        satellite_id=os.getenv("SATELLITE_ID", "Zee-1"),
        sim_tick_ms=tick_ms,
        simulation_speed=speed,
        simulation_seed=env_int("SIMULATION_SEED", 42, minimum=0),
        telemetry_rate_hz=rate_hz,
        packet_loss_rate=env_float(
            "PACKET_LOSS_RATE", 0.02, minimum=0.0, maximum=1.0),
        packet_corruption_rate=env_float(
            "PACKET_CORRUPTION_RATE", 0.0, minimum=0.0, maximum=1.0),
        link_latency_ms=env_float("LINK_LATENCY_MS", 300.0, minimum=0.0),
        link_jitter_ms=env_float("LINK_JITTER_MS", 50.0, minimum=0.0),
        link_bandwidth_bps=env_int("LINK_BANDWIDTH_BPS", 9600, minimum=1),
        link_eb_n0_db=env_float("LINK_EB_N0_DB", 10.0, minimum=-10.0,
                                maximum=40.0),
        onboard_capacity_packets=env_int(
            "ONBOARD_CAPACITY_PACKETS", 1024, minimum=1),
        ground_station_id=os.getenv("GROUND_STATION_ID", "KUCHING-GS"),
        ground_station_latitude=env_float(
            "GROUND_STATION_LATITUDE", 1.5533, minimum=-90.0, maximum=90.0),
        ground_station_longitude=env_float(
            "GROUND_STATION_LONGITUDE", 110.3592, minimum=-180.0, maximum=180.0),
        min_elevation_deg=env_float("MIN_ELEVATION", 10.0,
                                    minimum=0.0, maximum=90.0),
        orbit_altitude_km=env_float("ORBIT_ALTITUDE_KM", 525.0,
                                    minimum=150.0, maximum=2000.0),
        orbit_inclination_deg=env_float("ORBIT_INCLINATION_DEG", 51.6,
                                        minimum=0.0, maximum=90.0),
        orbit_raan_deg=env_float("ORBIT_RAAN_DEG", 110.0,
                                 minimum=0.0, maximum=360.0),
        orbit_initial_phase_deg=env_float("ORBIT_INITIAL_PHASE_DEG", 2.0,
                                          minimum=0.0, maximum=360.0),
        database_name=os.getenv("DATABASE_NAME", "telemetry.db"),
        satellite_command_secret=os.getenv(
            "SATELLITE_COMMAND_SECRET", "dev-secret-change-me"),
        api_token=os.getenv("API_TOKEN", "dev-dashboard-token-change-me"),
        allow_unauthenticated_dashboard=env_bool(
            "ALLOW_UNAUTHENTICATED_DASHBOARD", False),
        buffer_when_not_visible=env_bool("BUFFER_WHEN_NOT_VISIBLE", True),
        downlink_buffer_on_contact=env_bool("DOWNLINK_BUFFER_ON_CONTACT", True),
        anomaly_temperature_high=env_float("ANOMALY_TEMPERATURE_HIGH", 70.0),
        anomaly_battery_low=env_float("ANOMALY_BATTERY_LOW", 15.0),
        anomaly_battery_critical=env_float("ANOMALY_BATTERY_CRITICAL", 5.0),
        anomaly_battery_drop_rate=env_float("ANOMALY_BATTERY_DROP_RATE", 5.0),
        anomaly_cpu_high=env_float("ANOMALY_CPU_HIGH", 95.0),
        faults_enabled=env_bool("FAULTS_ENABLED", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )

    _warn_if_dev_secrets(config)
    return config


def _warn_if_dev_secrets(config: SimulationConfig) -> None:
    if config.satellite_command_secret == "dev-secret-change-me":
        logger.warning(
            "SATELLITE_COMMAND_SECRET is using the development default. "
            "Set a real secret in the environment or .env for anything "
            "beyond local learning.")
    if config.api_token == "dev-dashboard-token-change-me":
        logger.warning(
            "API_TOKEN is using the development default. The simulator is "
            "intended for localhost only.")


@dataclass
class _ConfigState:
    instance: Optional[SimulationConfig] = None
    loaders: list[Callable[[], SimulationConfig]] = field(default_factory=list)


_state = _ConfigState()


def get_config() -> SimulationConfig:
    """Return the process-wide configuration singleton."""
    if _state.instance is None:
        _state.instance = _build_config()
    return _state.instance


def reload_config() -> SimulationConfig:
    """Re-read configuration (used by tests and /api/simulation/start)."""
    _state.instance = _build_config()
    return _state.instance