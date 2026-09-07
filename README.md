<div align="center">

# Zee-1 — Satellite Telemetry Simulator

_An educational, end-to-end simulation of a fictional **Zee-1** CubeSat and its
mission-control ground station._

Every number on screen comes from a documented, deterministic model — never
from a roll of the dice. Telemetry, noise, loss and corruption are all
_physics you can read about_.

> **Zee-1 is a fictional satellite.** This project is for learning, not real
> mission operations.

</div>

---

## Contents

| Section | What you'll find |
| --- | --- |
| [Key findings](#key-findings) | What this simulation proves, and what you can observe in it |
| [What it demonstrates](#what-it-demonstrates) | The five layers of the simulator, from space segment to operations |
| [Quickstart](#quickstart) | Run with Docker or natively, plus API authentication |
| [Telecommands](#telecommands) | Every uplink command the ground station can send |
| [Physical link model (why packets get corrupted)](#physical-link-model-why-packets-get-corrupted) | The BPSK/AWGN math behind bit errors |
| [Project layout](#project-layout) | Where each part of the codebase lives |
| [Testing & Documentation](#testing--documentation) | Run the test suite and read the deep-dives |

---

## Screenshots

<p align="center">
  <img width="80%" alt="Zee-1 mission-control overview" src="https://github.com/user-attachments/assets/5b9ce634-158d-4bb2-ac58-d770e6d2a07e" />
  <br>
  <em>A complete spacecraft &amp; link readout — every subsystem (EPS, OBC, ADCS, thermal)
  and the physical link can be watched and reasoned about live.</em>
</p>
<br>
<p align="center">
  <img width="80%" alt="Zee-1 ground track and telemetry trends" src="https://github.com/user-attachments/assets/0f03e911-7f69-4544-a85d-aba58222d086" />
  <br>
  <em>Orbit matters: telemetry only arrives during the Kuching pass, and trends
  show how spacecraft state evolves between passes.</em>
</p>
<br>
<p align="center">
  <img width="80%" alt="Zee-1 telecommand operations" src="https://github.com/user-attachments/assets/909a6dd8-825d-46f1-a43f-c00c17f94f23" />
  <br>
  <em>Security &amp; noise you can touch — auth, replay and mode gates, plus injecting
  link loss/corruption and watching the chat tester degrade.</em>
</p>

---

## Key findings

What running this simulation actually demonstrates:

1. **Corruption is physics, not luck** — bit errors track the BPSK/AWGN theory
   curve (`BER = 0.5·erfc(√(Eb/N0))`). At ~10 dB the link is essentially clean;
   near 0 dB it falls apart. You can reproduce the textbook curve live.
2. **The link is the weakest — and most teachable — link** — latency, packet
   loss and bandwidth shaping visibly degrade delivery; corrupted frames are
   caught by CRC-16; delivery recovers as soon as conditions allow.
3. **Commands are secure by construction** — every uplink is checked for
   structure, HMAC-HMAC auth, replay, and mode authorization *before* it can
   touch the spacecraft, so a bad, forged or out-of-mode order never executes.
4. **Orbital mechanics are real** — an `ORBITAL_BURN` changes altitude and
   period per the vis-viva equation (a larger orbit is a *slower* one), which
   shifts pass timing; `ADJUST_ATTITUDE` changes how much sunlight hits the bus.
5. **Everything is auditable end-to-end** — every frame, error, command and
   anomaly lands in a queryable event log, so cause → effect is always traceable.

## What it demonstrates

1. **Space segment** — subsystem models (EPS, OBC, ADCS, thermal, payload),
   safety modes, orbit propagation and ground-track over a single ground site.
2. **Link physics** — data is pushed through a real **BPSK-over-AWGN** model.
   Errors come from the signal-to-noise ratio (`Eb/N0`), not dice rolls.
   Add latency, packet loss and bandwidth shaping to taste.
3. **Ground segment** — frame sync, CRC-16 validation, telemetry decoding,
   sequential-packet anomaly detection, SQLite persistence.
4. **Telecommand uplink** — HMAC-SHA256 authenticated commands with mode-aware
   authorization and replay protection.
5. **Operations** — a live mission-control dashboard plus a REST API to drive
   it: change modes, inject faults, degrade the link, fire orbit burns, and
   even send a chat message through the noisy link.

---

## Quickstart

### With Docker (recommended)

```bash
docker compose up --build -d
```

Then open <http://127.0.0.1:8000/>.

> `data/` is bind-mounted, so the SQLite database survives container restarts.
> Use **Reset** on the dashboard (or delete `data/telemetry.db`) to start a
> clean mission.

### Without Docker (native Python)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env        # optional: edit your secrets
.\.venv\Scripts\python.exe -m uvicorn mission_control.api:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>.

### Authentication

API endpoints require an `X-API-Token` header. The dashboard fetches its own
token server-side. Set real secrets in `.env` before anything beyond local
learning:

```dotenv
API_TOKEN=your-long-secret-token
SATELLITE_COMMAND_SECRET=another-long-secret-token
```

---

## Telecommands

Spacecraft-side commands are validated (structure), authenticated (HMAC),
replay-checked and mode-authorized *before* execution.

| Command | Parameters | Effect |
| --- | --- | --- |
| `SET_MODE` | `mode` | Switch operating mode (BOOT/INIT/SAFE/NORMAL/…) |
| `REQUEST_TELEMETRY` | — | Queue an immediate telemetry downlink |
| `START_PAYLOAD` | — | Activate payload → `PAYLOAD_OPERATION` |
| `STOP_PAYLOAD` | — | Return to `NORMAL` |
| `ORBITAL_BURN` | `delta_v_mps` (±200) | Along-track ΔV: **+ raises** the orbit (passes come later), **− lowers** it (passes come sooner) |
| `ADJUST_ATTITUDE` | `attitude` | `NADIR`, `SUN_POINTING` (full sun), `INERTIAL` |

The dashboard also has a **chat / message link tester**: send text
ground↔satellite through the real lossy link and watch delivered / corrupted /
lost chunks and the reconstructed message.

---

## Physical link model (why packets get corrupted)

The link uses **BPSK modulation over an AWGN channel**. The bit error rate is
computed from the link budget:

```
BER = 0.5 * erfc(sqrt(Eb/N0_linear))
```

| Eb/N0 | BER | Link behaviour |
| --- | --- | --- |
| 0 dB | ≈ 7.9e-2 | heavily corrupted |
| 10 dB | ≈ 4e-6 | essentially clean |

`link_quality_percent = clamp(Eb/N0 / 15 * 100)`. Corruption is *physics*, not
a roll of the dice. See `docs/communication.md` for the full write-up.

---

## Project layout

```
config.py               Simulation configuration (env-driven, no secrets in code)
satellite/              Spacecraft models: orbit, modes, subsystems, storage, telemetry
communication/          SpaceLink (loss/latency/bandwidth), RF channel, visibility, chat
ground_station/         Receiver, validator, decoder, anomaly rules, station gateway
security/               Command registry, HMAC auth, replay guard
simulation/             Engine: events, faults, anomaly, runner
telemetry/              Schema, CRC-16, packet framing
database/               SQLite store
mission_control/        FastAPI + dashboard (mission_control/web)
tests/                  pytest suite
docs/                   In-depth explanation of the models
```

---

## Testing & Documentation

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

The suite covers the RF channel physics, the chat tester, and the orbital
burn / attitude telecommands (**47 tests passing**).

- `docs/architecture.md` — system design and data flow
- `docs/communication.md` — RF/BPSK link model in detail

---

<div align="center">

*Zee-1 is a fictional educational CubeSat simulator. Not a real satellite.*

</div>
