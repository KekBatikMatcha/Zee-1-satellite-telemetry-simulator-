# Communication model (downlink & uplink)

This document explains how packets travel between the satellite and the ground
station, including the new **physical RF channel** that makes corruption emerge
from noise rather than from a dice roll.

## The chain

```
satellite/ground segment bytes
        |  communication/rf_channel.py   (NEW: physical layer)
        |     bits  ->  BPSK symbols  ->  + AWGN(Eb/N0)  ->  hard-decision demod -> bits
        v
  communication/link.py                 (link layer)
        |     visibility gate, loss, latency+jitter, bandwidth shaping
        v
  receiver + CRC-16 validator           (ground station, or spacecraft for uplink)
```

## Physical layer: BPSK over AWGN

Modulation is coherent BPSK with unit symbol energy (Es = 1, hence Eb = 1 for
BPSK). Noise is additive white Gaussian, so a received symbol is

    r = s + n ,   n ~ N(0, sigma^2)

Per-dimension noise power:  sigma = sqrt(N0 / 2),  N0 = Eb / 10^(Eb/N0_db / 10).

A hard-decision demapper decides `sgn(r)`. The theoretical bit-error rate is

    BER = 0.5 * erfc(sqrt(Eb/N0))

| Eb/N0 (dB) | theoretical BER | Rule of thumb                  |
|-----------:|----------------:|--------------------------------|
| 0          | 7.9e-2          | link unusable                  |
| 4          | 1.3e-2          | failing link                   |
| 6          | 2.4e-3          | marginal                       |
| 8          | 1.9e-4          | poor                           |
| 10         | 3.9e-6          | good (default)                 |
| 12         | 9.0e-9          | excellent                      |

The "required Eb/N0 for BER 1e-5" is computed by inverting the formula
(~9.6 dB for BPSK) and surfaced in the API/dashboard.

### Key knobs (config / .env)

* `LINK_EB_N0_DB` — the link budget in dB (default `10`). Lower it live via
  `api /api/link/...`/`spacecraft` to degrade the link and watch CRC failures
  accumulate at the ground station.
* `PACKET_CORRUPTION_RATE` — *interference bursts* layered on top of the AWGN
  (default `0` = physics only). Represents jamming/co-channel interference;
  each burst flips ~3% of the packet's bits, guaranteeing a CRC failure.
* The `PACKET_CORRUPTION` fault forces interference bursts on every packet.

### Behavior with corruption

Every packet crosses the modulator + AWGN channel once, on the transmit side.
If any bit flips, the packet is counted as corrupted and delivered with the
physical bit errors intact. The receiver therefore fails the CRC-16 check for
a *physically* plausible reason.

## Link layer

On top of the RF channel the link still applies the classic impairments:

* **Visibility gate** — no traffic in either direction below the elevation mask.
* **Packet loss** — probability `PACKET_LOSS_RATE` (plus the `PACKET_LOSS`
  fault); counted as lost, never delivered.
* **Latency + jitter** — delivery scheduled at
  `base + U(0, jitter)` ms.
* **Bandwidth shaping** — `LINK_BANDWIDTH_BPS` bytes/sec budget delays packets
  while the instantaneous rate is exceeded.

## What a CRC failure tells you

Because the link layer and RF channel are separated, the ground station
distinguishes:

* `packets_corrupted` — frames that arrived but failed CRC (visible noise),
* `packets_missing` — sequence gaps left by lost/corrupted frames,
* `packets_lost` — frames the link dropped outright.

## Example: live link-budget collapse

1. Run the dashboard with the default Eb/N0 = 10 dB. Data gets through cleanly.
2. Drop `LINK_EB_N0_DB` to 3 via the environment (or the API) mid-pass.
3. `measured BER` climbs toward the theoretical value (~2.3e-2), the ground
   station starts reporting CRC failures, and `missing`/`packets_rejected`
   counters rise as validation rejects the noisy frames.