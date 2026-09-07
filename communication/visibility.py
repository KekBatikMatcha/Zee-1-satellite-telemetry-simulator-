"""Ground-station visibility / pass model.

Why this exists
---------------
Real LEO missions have short contact windows: a satellite must be within the
ground station's elevation mask and line of sight. Hidden by this model, the
spacecraft cannot transmit (or must buffer), and the ground station shows
NO CONTACT. This is what makes store-and-forward meaningful.

Model
-----
Given the sub-satellite point and the station position, the central angle
gamma between them is computed with the great-circle formula. The elevation
angle of the satellite above the station horizon follows from the geometry of
two concentric spheres (station at radius R, satellite at radius R+h):

    denom = sqrt(1 + (R/r)^2 - 2*(R/r)*cos(gamma))
    sin(elevation) = (cos(gamma) - R/r) / denom

The satellite is VISIBLE when elevation >= the configured elevation mask.
This is an educational approximation of a real pass (no refraction, no
obstacles, spherical Earth).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from telemetry.models import EARTH_RADIUS_KM


@dataclass(frozen=True)
class GroundStationPosition:
    """Static ground antenna location."""

    latitude_deg: float
    longitude_deg: float
    min_elevation_deg: float = 10.0
    name: str = "GROUND-STATION"


@dataclass(frozen=True)
class Visibility:
    """Computed visibility state between satellite and a station."""

    visible: bool
    elevation_deg: float
    distance_km: float
    azimuth_deg: float

    @property
    def state(self) -> str:
        return "VISIBLE" if self.visible else "NOT_VISIBLE"


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def ground_station_position(
    latitude: float,
    longitude: float,
    min_elevation: float,
    name: str = "GROUND-STATION",
) -> GroundStationPosition:
    return GroundStationPosition(
        latitude_deg=latitude,
        longitude_deg=longitude,
        min_elevation_deg=min_elevation,
        name=name,
    )


def compute_visibility(
    station: GroundStationPosition,
    satellite_latitude_deg: float,
    satellite_longitude_deg: float,
    satellite_altitude_km: float,
) -> Visibility:
    """Return visibility of the satellite from *station*."""
    lat_s = math.radians(satellite_latitude_deg)
    lat_g = math.radians(station.latitude_deg)
    dlon = math.radians(_wrap180(satellite_longitude_deg - station.longitude_deg))

    cos_gamma = (math.sin(lat_s) * math.sin(lat_g) +
                 math.cos(lat_s) * math.cos(lat_g) * math.cos(dlon))
    cos_gamma = max(-1.0, min(1.0, cos_gamma))
    gamma = math.acos(cos_gamma)

    r = EARTH_RADIUS_KM + satellite_altitude_km
    ratio = EARTH_RADIUS_KM / r
    denom = math.sqrt(1.0 + ratio * ratio - 2.0 * ratio * cos_gamma)
    sin_el = (cos_gamma - ratio) / denom if denom > 0 else -1.0
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))

    # Distance along the line of sight.
    distance = r * denom

    # Azimuth from station to satellite (great circle initial bearing).
    y = math.sin(dlon) * math.cos(lat_s)
    x = (math.cos(lat_g) * math.sin(lat_s) -
         math.sin(lat_g) * math.cos(lat_s) * math.cos(dlon))
    azimuth = math.degrees(math.atan2(y, x)) % 360.0

    return Visibility(
        visible=elevation >= station.min_elevation_deg,
        elevation_deg=round(elevation, 2),
        distance_km=round(distance, 1),
        azimuth_deg=round(azimuth, 2),
    )


class VisibilityTracker:
    """Detects AOS/LOS transitions so callers can log contact events."""

    def __init__(self) -> None:
        self.previous: bool | None = None

    def update(self, visibility: Visibility) -> str | None:
        """Return 'AOS', 'LOS' or None based on the new state."""
        now = visibility.visible
        transition = None
        if self.previous is None:
            transition = "AOS" if now else None
        elif not self.previous and now:
            transition = "AOS"
        elif self.previous and not now:
            transition = "LOS"
        self.previous = now
        return transition
