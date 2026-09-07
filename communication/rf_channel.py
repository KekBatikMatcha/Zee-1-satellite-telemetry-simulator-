"""Physical-layer model: BPSK modulation over an AWGN channel.

Why this exists
---------------
The link used to corrupt a packet by rolling a dice. A real space link
degrades because the received signal arrives buried in thermal noise; the
probability of a bit error follows the modulation's bit-error-rate (BER)
curve. This module makes bit corruption *emerge from physics*:

    received_symbol = transmit_symbol + noise

For BPSK over an additive-white-Gaussian-noise (AWGN) channel the theory BER
is closed-form, which makes the educational math clean:

    BER = 0.5 * erfc(sqrt(Eb/N0))          (BPSK, coherent hard decision)

    Eb/N0(dB) = 10*log10(Eb/N0)

so once you pick an Eb/N0 your noise power is fixed and every packet that
crosses the channel is genuinely degraded or not by the noise realization
-- not by the previous dice-roll.

What is modeled here (and what is not)
--------------------------------------
Modeled:
* bits <-> modulation symbols  (BPSK: bit 1 -> +1 symbol, bit 0 -> -1)
* AWGN at a configurable Eb/N0  (additive, white, Gaussian, i.i.d. per symbol)
* hard-decision demodulation     (received symbol sign)
* measured BER bookkeeping, link-quality mapping from Eb/N0 margin

Deliberately *not* modeled (out of scope for this simulator):
* carrier frequency / wavelength, antennas, free-space path loss, EIRP
* pulse shaping, matched filters, I/Q imbalance, fading, doppler
* FEC coding (no convolutional/RS/LDPC) -- without FEC the link is "raw"
"""

from __future__ import annotations

import bisect
import math
import random
from typing import Any


# ---------------------------------------------------------------------------
# Bit <-> symbol mapping helpers
# ---------------------------------------------------------------------------
def bits_from_bytes(data: bytes) -> list[int]:
    """Expand bytes into a big-endian (MSB-first) list of 0/1 bits."""
    return [((byte >> (7 - i)) & 1) for byte in data for i in range(8)]


def bytes_from_bits(bits: list[int]) -> bytes:
    """Pack an MSB-first 0/1 bit list back into bytes (floor to full bytes)."""
    out = bytearray(len(bits) // 8)
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i + j]
        out[i // 8] = byte
    return bytes(out)


def hamming(a: bytes, b: bytes) -> int:
    """Number of differing bits between two byte strings."""
    return sum((x ^ y).bit_count() for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Modulation / noise math (educational constants kept explicit)
# ---------------------------------------------------------------------------
def ber_bpsk(eb_n0_db: float) -> float:
    """Theoretical BPSK BER over AWGN at a given Eb/N0 in dB."""
    eb_n0 = 10.0 ** (eb_n0_db / 10.0)
    return 0.5 * math.erfc(math.sqrt(eb_n0))


def required_eb_n0_db(target_ber: float = 1e-5) -> float:
    """Inverse of :func:`ber_bpsk`: Eb/N0 needed to reach a BER target.

    ``ber_bpsk`` decreases monotonically in Eb/N0, so we negate the BER grid
    (now increasing) and binary-search where ``ber <= target``.
    """
    db_grid = [x / 100.0 for x in range(-1500, 3501)]  # -15 .. +35 dB
    ber_grid = [ber_bpsk(db) for db in db_grid]
    idx = bisect.bisect_left([-b for b in ber_grid], -target_ber)
    return db_grid[max(0, min(idx, len(db_grid) - 1))]


def link_quality_percent(eb_n0_db: float, ref_db: float = 15.0) -> float:
    """Educational signal-quality metric.

    Map Eb/N0 against a reference where the link is "perfect" for the demo:
    0 dB -> 0%, ``ref_db`` dB and above -> 100%.
    """
    return max(0.0, min(100.0, (eb_n0_db / ref_db) * 100.0))


def burst_errors(data: bytes, rng: random.Random, bit_ratio: float = 0.03,
                 max_flips: int | None = None) -> bytes:
    """Flip a burst of random bits (interference), guaranteeing CRC failure."""
    bits = bits_from_bytes(data)
    k = max(1, int(round(len(bits) * bit_ratio)))
    if max_flips is not None:
        k = min(k, max_flips)
    k = min(k, len(bits))
    for idx in rng.sample(range(len(bits)), k):
        bits[idx] ^= 1
    return bytes_from_bits(bits)


# ---------------------------------------------------------------------------
# Radio channel
# ---------------------------------------------------------------------------
class RadioChannel:
    """Transmit bytes through BPSK modulation + AWGN at a fixed Eb/N0.

    The channel is *stateless between packets* except for BER bookkeeping, so
    packet #100 is as likely to be corrupted as packet #1 at a given Eb/N0.
    """

    def __init__(self, eb_n0_db: float, rng: random.Random,
                 modulation: str = "BPSK") -> None:
        if eb_n0_db is None or math.isnan(eb_n0_db):
            raise ValueError("Eb/N0 must be a finite dB value")
        self.eb_n0_db = eb_n0_db
        self.modulation = modulation
        self.rng = rng
        self._transmitted_bits = 0
        self._error_bits = 0

    # -- live controls ------------------------------------------------------
    def set_eb_n0_db(self, value: float) -> None:
        self.eb_n0_db = float(value)

    # -- observability -------------------------------------------------------
    @property
    def theoretical_ber(self) -> float:
        return ber_bpsk(self.eb_n0_db)

    @property
    def required_eb_n0_db(self) -> float:
        return required_eb_n0_db(1e-5)

    @property
    def measured_ber(self) -> float | None:
        if self._transmitted_bits == 0:
            return None
        return self._error_bits / self._transmitted_bits

    @property
    def link_quality_percent(self) -> float:
        return link_quality_percent(self.eb_n0_db)

    def stats(self) -> dict[str, Any]:
        return {
            "modulation": self.modulation,
            "eb_n0_db": round(self.eb_n0_db, 1),
            "theoretical_ber": self.theoretical_ber,
            "measured_ber": self.measured_ber,
            "required_eb_n0_db": round(self.required_eb_n0_db, 2),
            "link_quality_percent": round(self.link_quality_percent, 1),
        }

    # -- the physical pass ----------------------------------------------------
    def transmit_data(self, data: bytes) -> bytes:
        """Modulate -> add AWGN -> hard-decision demodulate -> bytes back."""
        bits = bits_from_bytes(data)
        # BPSK: bit 1 -> +1, bit 0 -> -1. Symbol energy Es = 1, so Eb = 1.
        symbol_energy = 1.0
        n0 = symbol_energy / (10.0 ** (self.eb_n0_db / 10.0))
        sigma = math.sqrt(n0 / 2.0)  # per-dimension noise power

        received = [1.0 if (s + self.rng.gauss(0.0, sigma)) >= 0.0 else 0.0
                    for s in (1.0 if b else -1.0 for b in bits)]
        out = [int(s) for s in received]

        self._transmitted_bits += len(bits)
        self._error_bits += sum(a != b for a, b in zip(bits, out))
        return bytes_from_bits(out)