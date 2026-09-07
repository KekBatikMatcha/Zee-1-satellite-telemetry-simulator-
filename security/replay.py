"""Replay protection for telecommands.

Why replay protection is needed
-------------------------------
HMAC proves a command is authentic *now*, but an attacker who has previously
observed a valid signed command can resend it later (a replay). A repeated
command could e.g. re-trigger a payload or re-enter a mode. Replay protection
makes each command usable once.

Mechanism
---------
Every command carries a monotonically increasing sequence number per
satellite. The satellite tracks the highest valid sequence seen within a
window and rejects any sequence below (or equal to) that watermark as a
replay. This is a simplified anti-replay window, conceptually similar to the
window used in IPsec / anti-replay systems.
"""

from __future__ import annotations

import logging
from collections import deque

logger = logging.getLogger("security.replay")


class ReplayGuard:
    """Tracks command sequence numbers to detect replay attempts.

    State is held per (satellite_id) with a fixed window size. The guard is
    *inclusive*: the same sequence arriving twice is reported as replay.
    """

    def __init__(self) -> None:
        self._seen: dict[str, deque[int]] = {}
        self._watermark: dict[str, int] = {}
        self.replayed_sequences: list[tuple[str, int]] = []

    def _bucket(self, satellite_id: str) -> deque[int]:
        if satellite_id not in self._seen:
            self._seen[satellite_id] = deque(maxlen=512)
            self._watermark[satellite_id] = 0
        return self._seen[satellite_id]

    def is_replay(self, satellite_id: str, sequence: int) -> bool:
        """True if *sequence* was already seen for this satellite."""
        bucket = self._bucket(satellite_id)
        watermark = self._watermark[satellite_id]
        if sequence <= watermark:
            return True
        if sequence in bucket:
            return True
        return False

    def record(self, satellite_id: str, sequence: int) -> None:
        """Accept a sequence once authenticated (raise watermark, store)."""
        bucket = self._bucket(satellite_id)
        bucket.append(sequence)
        self._watermark[satellite_id] = max(self._watermark[satellite_id], sequence)

    def observe_replay(self, satellite_id: str, sequence: int) -> None:
        self.replayed_sequences.append((satellite_id, sequence))

    def reset(self) -> None:
        self._seen.clear()
        self._watermark.clear()
        self.replayed_sequences.clear()
