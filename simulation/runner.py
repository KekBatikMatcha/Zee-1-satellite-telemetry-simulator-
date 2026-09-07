"""Simulation runner: binds spacecraft, link, ground station, and database.

This is the composition root of the simulator. It owns the runtime objects,
wires the directional data flow (spacecraft->link->station, station->link->
spacecraft), and advances the simulation on a background loop so the FastAPI
process stays responsive.

The tick is the smallest scheduling unit: one simulated step per iteration.
A slower real-time rate (e.g. 1 simulated tick per 250 ms wall clock) lets an
operator watch a pass unfold without waiting in real clock time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import field, dataclass
from typing import Any

from communication.chat import ChatChannel
from communication.link import DOWNLINK, UPLINK, SpaceLink
from communication.visibility import compute_visibility
from config import SimulationConfig
from database.db import connect, init_db, wipe_db
from database.store import Store
from ground_station.station import GroundStation
from satellite.spacecraft import Spacecraft
from simulation.events import EventBus, Severity
from simulation.faults import FaultController

logger = logging.getLogger("simulation.runner")


@dataclass
class Simulation:
    """The whole running mission, ready for the API to query/control."""

    config: SimulationConfig
    events: EventBus
    store: Store
    faults: FaultController
    spacecraft: Spacecraft
    link: SpaceLink
    station: GroundStation
    chat: ChatChannel
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event,
                                         repr=False, init=False)
    _step_interval: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._step_interval = self.config.sim_tick_ms / 1000.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="simulation-thread", daemon=True)
        self._thread.start()
        logger.info("Simulation started (tick %.2fs, speed x%.1f)",
                    self._step_interval, self.config.simulation_speed)

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=5.0)
        logger.info("Simulation stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def reset(self) -> None:
        """Full reset: fresh objects + empty tables, simulation stopped."""
        self.stop()
        wipe_db(self.store.conn)
        init_db(self.store.conn)
        events, store, faults = self.events, self.store, self.faults
        spacecraft = Spacecraft(self.config, events, faults)
        self.link = SpaceLink(self.config, events, faults)
        self.chat = ChatChannel(self.config, events, self.link)
        self.spacecraft = spacecraft
        self.station = self._build_station()
        self._wire()
        events.clear()
        self._emit_startup_events()
        logger.info("Simulation reset to initial state")

    # ------------------------------------------------------------------
    # Composition (used by the factory and reset)
    # ------------------------------------------------------------------
    def _build_station(self) -> GroundStation:
        return GroundStation(
            config=self.config,
            events=self.events,
            link=self.link,
            store=self.store,
            secret=self.config.satellite_command_secret,
        )

    def _wire(self) -> None:
        def transmit_to_link(data: bytes) -> None:
            self.link.transmit(DOWNLINK, data)

        def deliver(direction: str, data: bytes) -> None:
            # Testing-only chat frames are routed to the chat tester before
            # the real telemetry/command pipelines.
            if self.chat.sniff(data):
                self.chat.receive(direction, data)
                return
            if direction == DOWNLINK:
                self.station.on_downlink(data)
            elif direction == UPLINK:
                self.spacecraft.on_uplink(data)

        self.spacecraft.transmit_cb = transmit_to_link
        self.link.set_delivery_callback(deliver)

    def _emit_startup_events(self) -> None:
        self.events.emit_event(
            "SYSTEM_BOOT", Severity.INFO.value,
            "Zee-1 powered on, OBC boot loader engaged",
            source="SATELLITE")

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------
    def tick_once(self) -> None:
        """Advance the simulation by one step (blocking; thread-agnostic)."""
        config = self.config
        dt_s = config.sim_tick_ms / 1000.0 * config.simulation_speed

        self.spacecraft.tick(dt_s)
        self.link.fetch_due()
        self.faults.expire(self.spacecraft.sim_time)

        vis = self.spacecraft._visibility
        if vis is not None:
            self.link.set_visibility(vis.visible)
            self.station.update_contact(vis)
            if not vis.visible and config.buffer_when_not_visible:
                pass  # buffering happens inside spacecraft._dispatch

        self.store.upsert_satellite_state(self.spacecraft.state_snapshot())

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 - keep simulator alive
                logger.exception("simulation tick failed")
            elapsed = time.monotonic() - started
            sleep_for = max(0.01, self._step_interval - elapsed)
            self._stop_event.wait(sleep_for)


def create_simulation(config: SimulationConfig) -> Simulation:
    """Factory: build every runtime object and wire the data flow."""
    from database.db import SCHEMA_VERSION
    conn = connect(config.database_path)
    init_db(conn)

    events = EventBus()
    store = Store(conn)
    events.subscribe(store.on_event)

    faults = FaultController(enabled=config.faults_enabled)
    link = SpaceLink(config, events, faults)

    spacecraft = Spacecraft(
        config=config, event_bus=events, faults=faults)
    station = GroundStation(
        config=config, events=events, link=link, store=store,
        secret=config.satellite_command_secret)

    sim = Simulation(
        config=config, events=events, store=store, faults=faults,
        spacecraft=spacecraft, link=link, station=station,
        chat=ChatChannel(config, events, link))
    sim._wire()
    sim._emit_startup_events()
    logger.info("Simulation objects created (db=%s)", config.database_path)
    return sim
