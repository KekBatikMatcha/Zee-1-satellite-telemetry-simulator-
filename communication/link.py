"""Simulated space communication link (downlink + uplink).

Why this exists
---------------
A real space link is lossy, delayed, and rate-limited. Modeling those three
effects (plus corruption) is what makes the ground station's validation and
sequence tracking meaningful. The link is the untrusted boundary between the
space and ground segments.

Model
-----
* Visibility gate: nothing crosses the link when the satellite is below the
  station elevation mask (both directions).
* Packet loss: with probability ``loss_rate`` a packet is discarded on the
  transmit side and counted lost.
* Corruption -- *from physics*: every packet is passed through a BPSK/AWGN
  channel (see rf_channel.py). Bit errors emerge from the noise realization
  at the configured Eb/N0, so a CRC failure is the ground station observing
  real channel noise rather than a coin flip.
* Interference bursts: ``corruption_rate`` (and the PACKET_CORRUPTION fault)
  layer extra bit-flip bursts on top of the AWGN, standing in for jamming or
  co-channel interference.
* Latency + jitter: each packet is scheduled for delivery at
  ``base + U(0, jitter)`` milliseconds after transmission.
* Bandwidth shaping: ``bytes/sec`` limit; packets beyond the instantaneous
  budget are delayed until capacity frees.
"""

from __future__ import annotations

import dataclasses
import heapq
import logging
import random
import time
from collections import deque
from typing import Any, Callable

from communication.rf_channel import (
    RadioChannel,
    burst_errors,
)
from simulation.events import EventBus, Severity

logger = logging.getLogger("communication.link")

DOWNLINK = "downlink"
UPLINK = "uplink"
BOTH = (DOWNLINK, UPLINK)


@dataclasses.dataclass(order=True)
class _Pending:
    due: float
    seq: int
    direction: str = dataclasses.field(compare=False)
    data: bytes = dataclasses.field(compare=False)
    corrupt: bool = dataclasses.field(compare=False)
    tx_wall: float = dataclasses.field(compare=False)


class SpaceLink:
    """Lossy, delayed, rate-limited channel between satellite and ground."""

    def __init__(
        self,
        config: Any,
        event_bus: EventBus,
        faults: Any,
        seed: int | None = None,
    ) -> None:
        self.config = config
        self.events = event_bus
        self.faults = faults
        self.loss_rate = config.packet_loss_rate
        self.corruption_rate = config.packet_corruption_rate
        self.base_latency_ms = config.link_latency_ms
        self.jitter_ms = config.link_jitter_ms
        self.bandwidth_bps = config.link_bandwidth_bps
        self.rng = random.Random(seed if seed is not None else config.simulation_seed)

        # Physical layer: BPSK modulation over AWGN at configured Eb/N0.
        self.rf = RadioChannel(
            eb_n0_db=config.link_eb_n0_db, rng=self.rng)

        self._pending: list[_Pending] = []
        self._seq = 0
        self._visible = True
        self.deliver_cb: Callable[[str, bytes], None] | None = None

        # counters
        self.transmitted = {"downlink": 0, "uplink": 0}
        self.lost = 0
        self.corrupted = 0
        self.delivered = {"downlink": 0, "uplink": 0}
        self.gate_dropped = 0
        self._latency_history: deque[float] = deque(maxlen=200)
        self._rate_window: deque[tuple[float, int]] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_delivery_callback(self, cb: Callable[[str, bytes], None]) -> None:
        self.deliver_cb = cb

    def visible(self) -> bool:
        """Current visibility gate (set each tick by the runner)."""
        return self._visible

    def set_visibility(self, visible: bool) -> None:
        self._visible = visible

    def set_eb_n0_db(self, value: float) -> None:
        """Adjust the physical Eb/N0 link budget live (e.g. from ops UI)."""
        if value is None or value != value:  # NaN guard
            raise ValueError("Eb/N0 must be a finite dB value")
        self.rf.set_eb_n0_db(value)

    def transmit(self, direction: str, data: bytes) -> bool:
        """Inject a packet into the channel. Returns True if accepted.

        Acceptance does not guarantee delivery — loss/visibility decisions
        are made here, matching how a radio either reaches the receiver or
        does not.
        """
        if direction not in BOTH:
            raise ValueError(f"direction must be one of {BOTH}")
        self.transmitted[direction] += 1

        if not self._visible:
            self.gate_dropped += 1
            self._log(Severity.INFO, f"{direction}: dropped, satellite not visible")
            return False

        if self.faults.is_active("COMMS_OUTAGE"):
            self.lost += 1
            self._log(Severity.CRITICAL, f"{direction}: lost (COMMS_OUTAGE)")
            return False

        effective_loss = self.loss_rate + (
            0.5 if self.faults.is_active("PACKET_LOSS") else 0.0)
        if self.rng.random() < effective_loss:
            self.lost += 1
            self._log(Severity.WARNING, f"{direction}: packet lost in channel")
            return False

        corrupt = False
        # Physical corruption: pass the bytes through BPSK + AWGN once. The
        # noise realization decides whether any bit actually flips.
        received = self.rf.transmit_data(data)
        if received != data:
            corrupt = True
            self.corrupted += 1

        # Interference bursts (config knob + PACKET_CORRUPTION fault): extra
        # bit-flips on top of the noise, independent of Eb/N0.
        interference = self.corruption_rate + (
            0.5 if self.faults.is_active("PACKET_CORRUPTION") else 0.0)
        if not corrupt and self.rng.random() < interference:
            received = burst_errors(received, self.rng)
            corrupt = True
            self.corrupted += 1
        elif corrupt and self.rng.random() < interference:
            received = burst_errors(received, self.rng)

        now = time.time()
        latency_ms = self.base_latency_ms + self.rng.uniform(0.0, self.jitter_ms)
        self._latency_history.append(latency_ms)
        due = now + latency_ms / 1000.0
        # Bandwidth shaping: delay delivery if the instantaneous budget is hit.
        due = max(due, self._shaping_due(now, len(received)))

        self._seq += 1
        heapq.heappush(self._pending, _Pending(
            due=due, seq=self._seq, direction=direction, data=received,
            corrupt=corrupt, tx_wall=now))
        return True

    def _shaping_due(self, now: float, size: int) -> float:
        # Drain old samples (~1 s window).
        while self._rate_window and self._rate_window[0][0] < now - 1.0:
            self._rate_window.popleft()
        used = sum(sz for _, sz in self._rate_window)
        if used + size <= self.bandwidth_bps:
            return 0.0
        over = (used + size - self.bandwidth_bps) / self.bandwidth_bps
        return now + over

    def fetch_due(self, now: float | None = None) -> int:
        """Deliver all packets whose latency elapsed. Returns number delivered."""
        now = now if now is not None else time.time()
        delivered = 0
        while self._pending and self._pending[0].due <= now:
            item = heapq.heappop(self._pending)
            data = item.data  # already passed through BPSK/AWGN at transmit
            if self.deliver_cb is not None:
                try:
                    self.deliver_cb(item.direction, data)
                except Exception:  # noqa: BLE001
                    logger.exception("link delivery callback failed")
            self.delivered[item.direction] += 1
            self._rate_window.append((now, len(item.data)))
            delivered += 1
        return delivered

    def _log(self, severity: str, message: str) -> None:
        self.events.emit_event("PACKET_LOST", severity, message,
                               source="COMMUNICATION")

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    def statistics(self) -> dict[str, Any]:
        tx = self.transmitted["downlink"]
        delivered = self.delivered["downlink"]
        avg_latency = (sum(self._latency_history) / len(self._latency_history)
                       if self._latency_history else 0.0)
        now = time.time()
        while self._rate_window and self._rate_window[0][0] < now - 1.0:
            self._rate_window.popleft()
        data_rate = sum(sz for _, sz in self._rate_window)
        stats = {
            "packets_transmitted": tx,
            "packets_received": delivered,
            "packets_lost": self.lost + self.gate_dropped,
            "packets_corrupted": self.corrupted,
            "gate_dropped": self.gate_dropped,
            "avg_latency_ms": round(avg_latency, 1),
            "current_latency_ms": round(
                self._latency_history[-1] if self._latency_history else 0.0, 1),
            "data_rate_bps": data_rate,
            "bandwidth_bps": self.bandwidth_bps,
            "link_quality_percent": round(self.rf.link_quality_percent, 1),
            "pending_queue": len(self._pending),
            "uplink_transmitted": self.transmitted["uplink"],
            "uplink_delivered": self.delivered["uplink"],
        }
        stats.update(self.rf.stats())
        return stats