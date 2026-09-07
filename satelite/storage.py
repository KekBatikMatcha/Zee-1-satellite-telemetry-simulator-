"""Onboard storage: store-and-forward telemetry queue.

Why this exists
---------------
LEO satellites have short, intermittent contact windows. When Zee-1 is
out of ground-station view, telemetry is buffered onboard and dumped on the
next pass. This module models that queue with a capacity limit and a
priority-based scheduling policy.

Policy
------
* ``capacity`` packets maximum.
* Entries are scheduled CRITICAL first, then NORMAL, then LOW (FIFO within a
  priority class) — "priority then FIFO".
* A full queue drops the LOWEST-priority oldest entry to protect critical data.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from telemetry.schema import TelemetryPriority


@dataclass(order=True)
class _QueuedItem:
    priority: int          # lower value = higher priority (see schema enum)
    seq: int
    record: object = None  # TelemetryRecord (kept untyped to avoid import cycle)


class OnboardStorage:
    """A bounded, priority-aware telemetry buffer."""

    def __init__(self, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._buckets: dict[int, deque[_QueuedItem]] = {
            p: deque() for p in (TelemetryPriority.CRITICAL.value,
                                 TelemetryPriority.NORMAL.value,
                                 TelemetryPriority.LOW.value)
        }
        self.dropped = 0  # packets dropped because storage was full

    def __len__(self) -> int:
        return sum(len(q) for q in self._buckets.values())

    @property
    def used_packets(self) -> int:
        return len(self)

    @property
    def free_packets(self) -> int:
        return max(0, self.capacity - len(self))

    @property
    def usage_percent(self) -> float:
        return (len(self) / self.capacity * 100.0) if self.capacity else 0.0

    def enqueue(self, priority: TelemetryPriority, seq: int, record: object) -> bool:
        """Buffer one record. Drops the oldest LOW-priority item if full."""
        if len(self) >= self.capacity:
            # Drop worst candidate: lowest priority bucket, oldest item.
            for p in (
                TelemetryPriority.LOW.value,
                TelemetryPriority.NORMAL.value,
                TelemetryPriority.CRITICAL.value,
            ):
                if self._buckets[p]:
                    self._buckets[p].popleft()
                    self.dropped += 1
                    break
        # Do not continue if still full after the guard drop.
        if len(self) >= self.capacity:
            self.dropped += 1
            return False
        item = _QueuedItem(priority=priority.value, seq=seq, record=record)
        self._buckets[priority.value].append(item)
        return True

    def drain(self, limit: int | None = None) -> list[object]:
        """Remove and return up to *limit* records in scheduled order.

        Scheduling: CRITICAL bucket drained first, then NORMAL, then LOW.
        Within a bucket, oldest first (FIFO). None/0 -> drain everything.
        """
        if limit is None or limit <= 0:
            limit = len(self)
        out: list[object] = []
        remaining = limit
        for p in (
            TelemetryPriority.CRITICAL.value,
            TelemetryPriority.NORMAL.value,
            TelemetryPriority.LOW.value,
        ):
            bucket = self._buckets[p]
            while bucket and remaining > 0:
                out.append(bucket.popleft().record)
                remaining -= 1
            if remaining <= 0:
                break
        return out

    def peek(self, limit: int = 10) -> list[object]:
        """Return up to *limit* oldest records in scheduled order (no removal)."""
        out: list[object] = []
        for p in (
            TelemetryPriority.CRITICAL.value,
            TelemetryPriority.NORMAL.value,
            TelemetryPriority.LOW.value,
        ):
            for item in list(self._buckets[p])[: limit - len(out)]:
                out.append(item.record)
        return out

    def clear(self) -> None:
        for q in self._buckets.values():
            q.clear()
        self.dropped = 0

    def stats(self) -> dict:
        return {
            "used_packets": self.used_packets,
            "free_packets": self.free_packets,
            "capacity": self.capacity,
            "usage_percent": round(self.usage_percent, 1),
            "dropped": self.dropped,
        }