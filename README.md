<div align="center">

<img src="https://em-content.zobj.net/source/apple/391/satellite_1f6f0-fe0f.png" width="72" alt="satellite"/>

# ZEE-1
### Satellite Telemetry Simulator v.1.0

_An educational, end-to-end simulation of a fictional **Zee-1** CubeSat and its_
_mission-control ground station._

<br/>

![Status](https://img.shields.io/badge/status-educational%20simulation-1a2b5c?style=for-the-badge&labelColor=0a1226)
![Language](https://img.shields.io/badge/python-3.11%2B-1a2b5c?style=for-the-badge&labelColor=0a1226)
![Tests](https://img.shields.io/badge/tests-47%20passing-1a2b5c?style=for-the-badge&labelColor=0a1226)
![License](https://img.shields.io/badge/license-MIT-1a2b5c?style=for-the-badge&labelColor=0a1226)

<br/>

*Every number on screen comes from a documented, deterministic model — never*
*from a roll of the dice. Telemetry, noise, loss and corruption are all*
***physics you can read about.***

> 🛰️ **Zee-1 is a fictional satellite.** This project is for learning, not real
> mission operations.

</div>

<br/>

---

## 🗺️ Contents

| Section | What you'll find |
| :-- | :-- |
| [✨ Key findings](#-key-findings) | What this simulation proves, and what you can observe in it |
| [🔭 What it demonstrates](#-what-it-demonstrates) | The five layers of the simulator, from space segment to operations |
| [🌌 System flow](#-system-flow) | End-to-end data flow, from sensors to dashboard |
| [🚀 Quickstart](#-quickstart) | Run with Docker or natively, plus API authentication |
| [📡 Telecommands](#-telecommands) | Every uplink command the ground station can send |
| [📶 Physical link model](#-physical-link-model-why-packets-get-corrupted) | The BPSK/AWGN math behind bit errors |
| [🧭 Project layout](#-project-layout) | Where each part of the codebase lives |
| [🧪 Testing & Documentation](#-testing--documentation) | Run the test suite and read the deep-dives |

---

## 📸 Mission Control, live

<p align="center">
  <img width="80%" alt="Zee-1 mission-control overview" src="https://github.com/user-attachments/assets/5b9ce634-158d-4bb2-ac58-d770e6d2a07e" />
  <br>
  <sub><em>A complete spacecraft &amp; link readout — every subsystem (EPS, OBC, ADCS, thermal)<br/>
  and the physical link can be watched and reasoned about live.</em></sub>
</p>

<br/>

<p align="center">
  <img width="80%" alt="Zee-1 ground track and telemetry trends" src="https://github.com/user-attachments/assets/0f03e911-7f69-4544-a85d-aba58222d086" />
  <br>
  <sub><em>Orbit matters: telemetry only arrives during the Kuching pass, and trends<br/>
  show how spacecraft state evolves between passes.</em></sub>
</p>

<br/>

<p align="center">
  <img width="80%" alt="Zee-1 telecommand operations" src="https://github.com/user-attachments/assets/909a6dd8-825d-46f1-a43f-c00c17f94f23" />
  <br>
  <sub><em>Security &amp; noise you can touch — auth, replay and mode gates, plus injecting<br/>
  link loss/corruption and watching the chat tester degrade.</em></sub>
</p>

---

## ✨ Key findings

What running this simulation actually demonstrates:

| # | Finding |
| :-: | :-- |
| 1 | **Corruption is physics, not luck** — bit errors track the BPSK/AWGN theory curve (`BER = 0.5·erfc(√(Eb/N0))`). At ~10 dB the link is essentially clean; near 0 dB it falls apart. You can reproduce the textbook curve live. |
| 2 | **The link is the weakest — and most teachable — link** — latency, packet loss and bandwidth shaping visibly degrade delivery; corrupted frames are caught by CRC-16; delivery recovers as soon as conditions allow. |
| 3 | **Commands are secure by construction** — every uplink is checked for structure, HMAC auth, replay, and mode authorization *before* it can touch the spacecraft, so a bad, forged or out-of-mode order never executes. |
| 4 | **Orbital mechanics are real** — an `ORBITAL_BURN` changes altitude and period per the vis-viva equation (a larger orbit is a *slower* one), which shifts pass timing; `ADJUST_ATTITUDE` changes how much sunlight hits the bus. |
| 5 | **Everything is auditable end-to-end** — every frame, error, command and anomaly lands in a queryable event log, so cause → effect is always traceable. |

---

## 🔭 What it demonstrates

<table>
<tr><td width="40" align="center">🛰️</td><td><b>Space segment</b><br/>Subsystem models (EPS, OBC, ADCS, thermal, payload), safety modes, orbit propagation and ground-track over a single ground site.</td></tr>
<tr><td align="center">📶</td><td><b>Link physics</b><br/>Data is pushed through a real <b>BPSK-over-AWGN</b> model. Errors come from the signal-to-noise ratio (<code>Eb/N0</code>), not dice rolls. Add latency, packet loss and bandwidth shaping to taste.</td></tr>
<tr><td align="center">📡</td><td><b>Ground segment</b><br/>Frame sync, CRC-16 validation, telemetry decoding, sequential-packet anomaly detection, SQLite persistence.</td></tr>
<tr><td align="center">🔐</td><td><b>Telecommand uplink</b><br/>HMAC-SHA256 authenticated commands with mode-aware authorization and replay protection.</td></tr>
<tr><td align="center">🎛️</td><td><b>Operations</b><br/>A live mission-control dashboard plus a REST API to drive it: change modes, inject faults, degrade the link, fire orbit burns, and even send a chat message through the noisy link.</td></tr>
</table>

---

## 🌌 System flow

End-to-end data flow, from sensors on the spacecraft to the mission-control
dashboard — including the reverse telecommand path.

```mermaid
flowchart TD
    subgraph SPACE["🛰️ SPACE SEGMENT — Zee-1"]
        SENS[Sensors<br/>power / thermal / attitude / payload]
        OBC[On-Board Computer<br/>state mgmt, mode logic]
        TGEN[Telemetry Generation<br/>deterministic subsystem models]
        PKT[Packetization<br/>SYNC · VERSION · SEQ · TIMESTAMP · PAYLOAD · CRC-16]
        RADIO[Radio / Comm Subsystem]

        SENS --> OBC --> TGEN --> PKT --> RADIO
    end

    subgraph LINK["📶 SIMULATED SPACE LINK"]
        RF[BPSK/AWGN RF Channel<br/>Eb/N0 → BER]
        DEGRADE[Loss / Latency / Jitter / Bandwidth Shaping]
        RF --> DEGRADE
    end

    subgraph GROUND["📡 GROUND SEGMENT"]
        RECV[Receiver]
        VALID[Packet Validator<br/>CRC check, sequence check]
        DECODE[Decoder]
        ANOM[Anomaly Detection<br/>rule-based thresholds]
        RECV --> VALID --> DECODE --> ANOM
    end

    subgraph DB["🗄️ DATABASE — SQLite"]
        TELE_T[(telemetry)]
        PKT_T[(packets)]
        EVT_T[(events)]
        SEC_T[(security_events)]
        STATE_T[(satellite_state)]
    end

    subgraph MC["🎛️ MISSION CONTROL"]
        API[FastAPI REST API]
        DASH[Dashboard<br/>status, charts, map, events]
        API --> DASH
    end

    RADIO --> RF
    DEGRADE --> RECV
    ANOM --> DB
    DB --> API

    %% ---- Telecommand (reverse direction) ----
    subgraph TC["🔐 TELECOMMAND — Ground → Satellite"]
        CMD[Command Builder<br/>SET_MODE / REQUEST_TELEMETRY / ORBITAL_BURN / etc.]
        AUTH[HMAC-SHA256 Auth<br/>+ Replay Guard]
        AUTHZ[Mode Authorization Check]
        EXEC[Command Execution]
        CMD --> AUTH --> AUTHZ --> EXEC
    end

    DASH -. sends command .-> CMD
    EXEC -. via link .-> DEGRADE
    DEGRADE -. uplink .-> OBC
    AUTHZ -- rejected --> SEC_T
    VALID -- CRC/seq failure --> EVT_T

    %% ---- Deep space styling ----
    style SPACE fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff
    style LINK fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff
    style GROUND fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff
    style DB fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff
    style MC fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff
    style TC fill:#0a1a3c,stroke:#4a7fd6,stroke-width:1px,color:#fff

    style SENS fill:#132a5c,stroke:#4a7fd6,color:#fff
    style OBC fill:#132a5c,stroke:#4a7fd6,color:#fff
    style TGEN fill:#132a5c,stroke:#4a7fd6,color:#fff
    style PKT fill:#132a5c,stroke:#4a7fd6,color:#fff
    style RADIO fill:#132a5c,stroke:#4a7fd6,color:#fff
    style RF fill:#132a5c,stroke:#4a7fd6,color:#fff
    style DEGRADE fill:#132a5c,stroke:#4a7fd6,color:#fff
    style RECV fill:#132a5c,stroke:#4a7fd6,color:#fff
    style VALID fill:#132a5c,stroke:#4a7fd6,color:#fff
    style DECODE fill:#132a5c,stroke:#4a7fd6,color:#fff
    style ANOM fill:#132a5c,stroke:#4a7fd6,color:#fff
    style API fill:#132a5c,stroke:#4a7fd6,color:#fff
    style DASH fill:#132a5c,stroke:#4a7fd6,color:#fff
    style CMD fill:#132a5c,stroke:#4a7fd6,color:#fff
    style AUTH fill:#132a5c,stroke:#4a7fd6,color:#fff
    style AUTHZ fill:#132a5c,stroke:#4a7fd6,color:#fff
    style EXEC fill:#132a5c,stroke:#4a7fd6,color:#fff
    style TELE_T fill:#132a5c,stroke:#4a7fd6,color:#fff
    style PKT_T fill:#132a5c,stroke:#4a7fd6,color:#fff
    style EVT_T fill:#132a5c,stroke:#4a7fd6,color:#fff
    style SEC_T fill:#132a5c,stroke:#4a7fd6,color:#fff
    style STATE_T fill:#132a5c,stroke:#4a7fd6,color:#fff
```

**⬇ Downlink (telemetry):** sensors → OBC → packetization → RF/AWGN link (with
configurable loss/latency) → ground receiver → CRC/sequence validation →
decode → anomaly check → SQLite → API → dashboard.

**⬆ Uplink (telecommand):** dashboard/API issues a command → HMAC auth + replay
check → mode-authorization gate → only then does it reach the OBC through the
same noisy link. Failures at validation or auth/authorization are logged to
`events` / `security_events`, which feed the dashboard's event log.

---

## 🚀 Quickstart

### With Docker <sub>(recommended)</sub>

```bash
docker compose up --build -d
```

Then open <http://127.0.0.1:8000/>.

> 💾 `data/` is bind-mounted, so the SQLite database survives container restarts.
> Use **Reset** on the dashboard (or delete `data/telemetry.db`) to start a
> clean mission.

### Without Docker <sub>(native Python)</sub>

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env        # optional: edit your secrets
.\.venv\Scripts\python.exe -m uvicorn mission_control.api:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>.

### 🔑 Authentication

API endpoints require an `X-API-Token` header. The dashboard fetches its own
token server-side. Set real secrets in `.env` before anything beyond local
learning:

```dotenv
API_TOKEN=your-long-secret-token
SATELLITE_COMMAND_SECRET=another-long-secret-token
```

---

## 📡 Telecommands

Spacecraft-side commands are validated (structure), authenticated (HMAC),
replay-checked and mode-authorized *before* execution.

| Command | Parameters | Effect |
| :-- | :-- | :-- |
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

## 📶 Physical link model <sub>(why packets get corrupted)</sub>

The link uses **BPSK modulation over an AWGN channel**. The bit error rate is
computed from the link budget:

```
BER = 0.5 * erfc(sqrt(Eb/N0_linear))
```

| Eb/N0 | BER | Link behaviour |
| :-: | :-: | :-- |
| 0 dB | ≈ 7.9e-2 | 🔴 heavily corrupted |
| 10 dB | ≈ 4e-6 | 🟢 essentially clean |

`link_quality_percent = clamp(Eb/N0 / 15 * 100)`. Corruption is *physics*, not
a roll of the dice. See [`docs/communication.md`](docs/communication.md) for
the full write-up.

---

## 🧭 Project layout

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

## 🧪 Testing & Documentation

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

The suite covers the RF channel physics, the chat tester, and the orbital
burn / attitude telecommands — **47 tests passing**. ✅

- 📘 [`docs/architecture.md`](docs/architecture.md) — system design and data flow
- 📘 [`docs/communication.md`](docs/communication.md) — RF/BPSK link model in detail

---

<div align="center">

⋆｡°✩ *Zee-1 is a fictional educational CubeSat simulator. Not a real satellite.* ⋆｡°✩

</div>
