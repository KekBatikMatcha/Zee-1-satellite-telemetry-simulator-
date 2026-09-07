"""FastAPI application exposing the mission-control REST API.

Why this exists
---------------
The dashboard and external tooling need read access to the simulated mission
and safe, validated control endpoints. Every incoming request is validated;
there are no arbitrary code-execution endpoints. The API is a thin layer over
the simulation objects â€” it holds no domain logic.

Authentication
--------------
Endpoints require the ``X-API-Token`` header unless
``ALLOW_UNAUTHENTICATED_DASHBOARD=true``. The token comes from ``API_TOKEN``
(see .env.example). This keeps the simulator safe by default while easy to
run locally.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import SimulationConfig, get_config
from satellite.modes import Mode, InvalidModeTransition
from security.commands import CommandRequest, CommandError, new_command
from simulation.runner import Simulation, create_simulation

logger = logging.getLogger("mission_control.api")

WEB_DIR = Path(__file__).resolve().parent / "web"
IDEMPOTENT_COMMANDS = {"REQUEST_TELEMETRY", "RESET_SIMULATION"}


# ---------------------------------------------------------------------------
# Request/response models (auto-validated)
# ---------------------------------------------------------------------------
class ModeRequest(BaseModel):
    mode: str = Field(..., pattern="^[A-Z_]+$")


class FaultRequest(BaseModel):
    fault: str = Field(..., min_length=2, max_length=64)
    duration_s: float | None = Field(default=None, ge=0.0)


class CommandRequestModel(BaseModel):
    command: str = Field(..., min_length=2, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    sequence: int | None = Field(default=None, ge=0)


class EbN0Request(BaseModel):
    eb_n0_db: float = Field(..., ge=-10.0, le=40.0)


class ChatSendRequest(BaseModel):
    direction: str = Field(..., pattern="^UP|DOWN$")
    text: str = Field(..., min_length=1, max_length=4000)


class StartResponse(BaseModel):
    running: bool


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _token_ok(api_token: str, token: str | None) -> bool:
    if not token:
        return False
    return _secure_compare(token, api_token)


def _secure_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.sim: Simulation | None = None
        self.config: SimulationConfig | None = None


def _get_config(state: AppState) -> SimulationConfig:
    if state.config is None:
        state.config = get_config()
    return state.config


def _get_sim(state: AppState) -> Simulation:
    if state.sim is None:
        config = _get_config(state)
        state.sim = create_simulation(config)
    return state.sim


def create_app(auto_start: bool = True) -> FastAPI:
    state = AppState()
    app = FastAPI(
        title="Zee-1 Satellite Telemetry Simulator",
        description="Educational CubeSat telemetry / ground-segment simulator.",
        version="0.1.0",
    )

    def config_dep() -> SimulationConfig:
        return _get_config(state)

    def _require_token(
        config: SimulationConfig = Depends(config_dep),
        x_api_token: str | None = Header(default=None),
    ) -> None:
        if config.allow_unauthenticated_dashboard:
            return
        if not _token_ok(config.api_token, x_api_token):
            raise HTTPException(status_code=401,
                                detail="missing or invalid API token")

    def sim_dep() -> Simulation:
        return _get_sim(state)

    # ------------------------------ simulation -----------------------------
    @app.post("/api/simulation/start",
              response_model=StartResponse,
              dependencies=[Depends(_require_token)])
    def start_simulation(sim: Simulation = Depends(sim_dep)) -> dict[str, bool]:
        sim.start()
        return {"running": sim.running}

    @app.post("/api/simulation/stop",
              response_model=StartResponse,
              dependencies=[Depends(_require_token)])
    def stop_simulation(sim: Simulation = Depends(sim_dep)) -> dict[str, bool]:
        sim.stop()
        return {"running": sim.running}

    @app.post("/api/simulation/reset",
              response_model=StartResponse,
              dependencies=[Depends(_require_token)])
    def reset_simulation(sim: Simulation = Depends(sim_dep)) -> dict[str, bool]:
        sim.reset()
        sim.start()
        return {"running": sim.running}

    @app.post("/api/simulation/fault", dependencies=[Depends(_require_token)])
    def inject_fault(body: FaultRequest,
                     sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        try:
            definition = sim.faults.inject(body.fault, sim.spacecraft.sim_time,
                                           body.duration_s)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"fault": body.fault,
                "active": True,
                "description": definition.description}

    @app.delete("/api/simulation/fault/{fault_name}",
                dependencies=[Depends(_require_token)])
    def clear_fault(fault_name: str,
                    sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        cleared = sim.faults.clear(fault_name)
        if not cleared:
            raise HTTPException(404, f"fault '{fault_name}' not active")
        return {"fault": fault_name, "cleared": True}

    @app.get("/api/simulation/faults")
    def list_faults(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return {"active": sim.faults.list_active(),
                "available": sim.faults.list_all()}

    # ------------------------------ spacecraft -----------------------------
    @app.get("/api/satellite/status")
    def satellite_status(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return sim.spacecraft.state_snapshot()

    @app.post("/api/satellite/mode", dependencies=[Depends(_require_token)])
    def set_satellite_mode(body: ModeRequest,
                           sim: Simulation = Depends(sim_dep)) -> dict[str, str]:
        try:
            mode = Mode(body.mode)
        except ValueError:
            raise HTTPException(422, f"unknown mode '{body.mode}'")
        try:
            sim.spacecraft.request_mode(mode, "mission control (direct)")
        except InvalidModeTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"mode": mode.value, "status": "ok"}

    @app.get("/api/spacecraft/onboard/storage")
    def onboard_storage(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return sim.spacecraft.storage.stats()

    # ------------------------------ telemetry ------------------------------
    @app.get("/api/telemetry/latest")
    def latest_telemetry(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        row = sim.store.latest_telemetry()
        if row is None:
            return sim.spacecraft.last_record.to_dict() \
                if sim.spacecraft.last_record else {}
        return row

    @app.get("/api/telemetry/history")
    def telemetry_history(
        limit: int = Query(default=200, ge=1, le=5000),
        since: float | None = Query(default=None, ge=0),
        sim: Simulation = Depends(sim_dep),
    ) -> list[dict[str, Any]]:
        return sim.store.telemetry_history(limit=limit, since=since)

    @app.get("/api/telemetry/series")
    def telemetry_series(
        field: str = Query(...),
        limit: int = Query(default=400, ge=1, le=4000),
        sim: Simulation = Depends(sim_dep),
    ) -> list[dict[str, Any]]:
        try:
            return sim.store.telemetry_field_series(field, limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/anomalies")
    def anomalies(limit: int = Query(default=50, ge=1, le=500),
                  sim: Simulation = Depends(sim_dep)) -> list[dict[str, Any]]:
        out = []
        for item in sim.store.telemetry_history(limit):
            if item.get("anomalies"):
                out.append(item)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------ communication --------------------------
    @app.get("/api/link/status")
    def link_status(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return sim.link.statistics()

    @app.post("/api/link/eb-n0", dependencies=[Depends(_require_token)])
    def set_link_budget(body: EbN0Request,
                        sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        sim.link.set_eb_n0_db(body.eb_n0_db)
        return sim.link.statistics()

    @app.get("/api/packets/statistics")
    def packets_statistics(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return {
            "ground_station": sim.station.statistics(),
            "database": sim.store.packet_statistics(),
        }

    @app.get("/api/events")
    def events(limit: int = Query(default=100, ge=1, le=500),
               sim: Simulation = Depends(sim_dep)) -> list[dict[str, Any]]:
        return sim.store.events_history(limit=limit)

    @app.get("/api/security/events")
    def security_events(limit: int = Query(default=100, ge=1, le=500),
                        sim: Simulation = Depends(sim_dep)) -> list[dict[str, Any]]:
        return sim.store.events_history(limit=limit, kind="security")

    # ------------------------------ commands -------------------------------
    @app.post("/api/command/send", dependencies=[Depends(_require_token)])
    def send_command(body: CommandRequestModel,
                     sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        cfg = _get_config(state)
        sequence = (body.sequence if body.sequence is not None
                    else sim.station.command_seq)
        cmd = new_command(
            satellite_id=cfg.satellite_id,
            command=body.command,
            parameters=body.parameters,
            sequence=sequence,
        )
        try:
            sent = sim.station.send_telecommand(cmd)
        except CommandError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        sent["sequence"] = sequence
        return sent

    @app.get("/api/command/result/{sequence}")
    def command_result(sequence: int,
                       sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        result = sim.spacecraft.command_results.get(sequence)
        if result is None:
            return {"sequence": sequence, "pending": True, "result": None}
        return {"sequence": sequence, "pending": False, "result": result}

    # ------------------------------ chat tester ---------------------------
    @app.post("/api/chat/send", dependencies=[Depends(_require_token)])
    def chat_send(body: ChatSendRequest,
                  sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        from communication.chat import ChatError
        try:
            return sim.chat.send(body.direction, body.text)
        except ChatError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/chat/history")
    def chat_history(limit: int = Query(default=20, ge=1, le=100),
                     sim: Simulation = Depends(sim_dep)) -> list[dict[str, Any]]:
        return sim.chat.history(limit)

    @app.get("/api/chat/stats")
    def chat_stats(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return sim.chat.stats()

    # ------------------------------ config --------------------------------
    @app.get("/api/config")
    def config(config: SimulationConfig = Depends(config_dep)) -> dict[str, Any]:
        return config.as_dict()

    @app.get("/api/status")
    def overall_status(sim: Simulation = Depends(sim_dep)) -> dict[str, Any]:
        return {
            "running": sim.running,
            "satellite": sim.spacecraft.state_snapshot(),
            "link": sim.link.statistics(),
            "ground_station": sim.station.statistics(),
        }

    # ------------------------------ web UI --------------------------------
    @app.get("/")
    def dashboard_index():
        index = WEB_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "dashboard not built")
        html = index.read_text(encoding="utf-8")
        html = html.replace("__API_TOKEN__", _get_config(state).api_token)
        return HTMLResponse(content=html)

    @app.get("/favicon.ico")
    def favicon() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    if auto_start:
        _get_sim(state).start()

    return app


#: ASGI entry point used by ``uvicorn mission_control.api:app``.
app = create_app()
