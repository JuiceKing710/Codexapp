from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from sienna_diag.config import settings
from sienna_diag.models import (
    AIAlertRecord,
    AIMechanicQuestionRequest,
    AIResponseRecord,
    CommandLearningIngestRequest,
    CommandLearningRecord,
    DiagnosticTimelineEvent,
    EventTag,
    GuidedDiagnosisResultSubmitRequest,
    GuidedDiagnosisRunRequest,
    CaptureStartRequest,
    CaptureTagRequest,
    OBDReadRequest,
    OBDReadResponse,
    PhoneBridgeConnectPayload,
    PhoneLiveReadPayload,
    PreprocessRequest,
    ReadHistoryItem,
    ReplayApprovalRequest,
    ReplayExecuteRequest,
    ReviewRequest,
    SessionCreateRequest,
    VehicleVisualizationHighlightRequest,
    UserSignInRequest,
    UserSignUpRequest,
    UserProfileUpdateRequest,
    VehicleProfile,
)
from sienna_diag.obd.adapter import OBDLinkAdapter
from sienna_diag.preprocessing.pipeline import run_preprocessing
from sienna_diag.review.lmstudio import local_llama_review
from sienna_diag.review.openai_review import openai_second_opinion
from sienna_diag.safety_policy import SafetyPolicy
from sienna_diag.session_store import set_active_user_context, store
from sienna_diag.vehicle_image_library import vehicle_image_library


app = FastAPI(title="Zeb’s OBD AI", version="0.4.0")
app.mount("/assets", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="assets")
adapter = OBDLinkAdapter()

SAFE_QUICK_READS = {
    "rpm": "010C",
    "coolant_temp": "0105",
    "fuel_level": "012F",
    "vehicle_speed": "010D",
    "throttle_position": "0111",
    "intake_air_temp": "010F",
    "control_module_voltage": "0142",
    "readiness_status": "0101",
    "freeze_frame": "020C",
    "vin": "0902",
    "stored_codes": "03",
    "pending_codes": "07",
}


LIVE_POLLING_SUPPORTED_PIDS = ["010C", "0105", "012F", "0142", "010F", "010D", "0111"]
PHONE_LIVE_SOURCE_MODES = {"PHONE-LIVE", "BROWSER-DEV"}
DEFAULT_LIVE_GAUGE_PIDS = ["010C", "0105", "0142", "010D"]

PHONE_LIVE_PID_LABELS = {
    "010C": {"pid_key": "rpm", "unit": "rpm"},
    "0105": {"pid_key": "coolant_temp", "unit": "°C"},
    "012F": {"pid_key": "fuel_level", "unit": "%"},
    "0142": {"pid_key": "control_module_voltage", "unit": "V"},
    "010F": {"pid_key": "intake_air_temp", "unit": "°C"},
    "010D": {"pid_key": "vehicle_speed", "unit": "km/h"},
    "0111": {"pid_key": "throttle_position", "unit": "%"},
    "0101": {"pid_key": "readiness_status", "unit": None},
    "020C": {"pid_key": "freeze_frame", "unit": "rpm"},
    "0902": {"pid_key": "vin", "unit": None},
    "03": {"pid_key": "dtc_stored", "unit": None},
    "07": {"pid_key": "dtc_pending", "unit": None},
}


def _new_phone_bridge_state() -> dict:
    return {
        "status": "disconnected",
        "adapter_name": "OBDLink MX+",
        "platform": "unknown",
        "permission_state": "unknown",
        "source_mode": "PHONE-LIVE",
        "supports_native_bluetooth": False,
        "phone_live_requested": False,
        "fallback_reason": None,
        "last_error": None,
        "last_command": None,
        "last_response": None,
        "last_latency_ms": None,
        "backend_status": "idle",
        "last_backend_acceptance_status": "idle",
        "last_ingest_status": "idle",
        "last_ingest_error": None,
        "last_vin_command": None,
        "last_vin_response": None,
        "vin_parse_status": "not-run",
        "vin": None,
        "updated_at": None,
        "connection_sequence_started_at": None,
        "polling_state": "inactive",
        "polling_interval_ms": 500,
        "configured_gauge_pids": DEFAULT_LIVE_GAUGE_PIDS.copy(),
        "live_monitoring_state": "inactive",
        "last_replayed_command": None,
        "command_learning_status": "idle",
        "passive_can_capture_status": "not-available",
    }


class UserScopedDict:
    def __init__(self, factory):
        self._factory = factory
        self._data: dict[str, dict] = {}

    def _bucket(self) -> dict:
        user_id = store._default_user_id(None)
        if user_id not in self._data:
            self._data[user_id] = self._factory()
        return self._data[user_id]

    def get(self, key, default=None):
        return self._bucket().get(key, default)

    def __getitem__(self, key):
        return self._bucket()[key]

    def __setitem__(self, key, value):
        self._bucket()[key] = value

    def update(self, payload: dict):
        self._bucket().update(payload)

    def __iter__(self):
        return iter(self._bucket())

    def __len__(self):
        return len(self._bucket())

    def keys(self):
        return self._bucket().keys()

    def items(self):
        return self._bucket().items()

    def copy(self):
        return dict(self._bucket())

    def clear(self):
        self._bucket().clear()


phone_bridge_state = UserScopedDict(_new_phone_bridge_state)

SAFE_EVENT_TAGS = [
    "engine start",
    "engine stop",
    "idle",
    "throttle blip",
    "brake press",
    "headlights on",
    "headlights off",
]

CAPTURE_PRESETS = {
    "cold_start_capture": "Cold Start Capture",
    "warm_idle_capture": "Warm Idle Capture",
    "throttle_response_capture": "Throttle Response Capture",
    "battery_charging_capture": "Battery/Charging Capture",
    "code_check_capture": "Code Check Capture",
}

CAPTURE_FAST_COMMANDS = ["010C", "0111", "0142", "010D"]
CAPTURE_SLOW_COMMANDS = ["0105", "010F", "03", "07", "0902"]

VEHICLE_COMPONENT_GROUPS = {
    "engine": ["engine_block", "spark_plug_bank", "thermostat"],
    "cooling_system": ["radiator", "thermostat", "water_pump"],
    "intake": ["air_filter", "throttle_body", "intake_manifold"],
    "exhaust": ["exhaust_manifold", "catalytic_converter", "muffler"],
    "fuel_system": ["fuel_pump", "fuel_rail", "injector_bank"],
    "electrical_system": ["battery", "alternator", "starter"],
    "transmission": ["transmission_case", "torque_converter", "transmission_fluid_pan"],
}

capture_lock = threading.Lock()
capture_stop_event = threading.Event()
capture_thread: threading.Thread | None = None
capture_status = "idle"
capture_preset = "cold_start_capture"

vehicle_visualization_state = UserScopedDict(lambda: {
    "action": None,
    "component": None,
    "system": None,
    "source": "system",
    "updated_at": None,
})



auth_tokens: dict[str, str] = {}


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _resolve_user_id(token: str | None) -> str:
    if not token:
        return "demo"
    return auth_tokens.get(token, "demo")


@app.middleware("http")
async def bind_user_context(request: Request, call_next):
    token = request.headers.get("x-session-token")
    set_active_user_context(_resolve_user_id(token))
    response = await call_next(request)
    return response


COMPONENT_EXPLANATIONS = {
    "thermostat": "Regulates coolant flow so the engine reaches and maintains normal operating temperature.",
    "radiator": "Transfers heat from coolant to airflow to keep engine temperature stable.",
    "engine_block": "Main engine structure containing cylinders and coolant passages.",
    "throttle_body": "Controls intake airflow into the engine based on throttle input.",
    "catalytic_converter": "Reduces harmful exhaust emissions before gases exit the tailpipe.",
    "battery": "Supplies electrical power for starting and supports vehicle electronics.",
    "transmission_case": "Houses gears and hydraulic pathways that transfer engine torque.",
}


@app.on_event("startup")
def startup_event() -> None:
    adapter.connect()


@app.on_event("shutdown")
def shutdown_event() -> None:
    stop_capture()
    adapter.close()


def _resolve_source_hint(protocol: str) -> str:
    return "can_capture" if protocol.upper().startswith("CAN") else "iso9141_2"


def _run_safe_read(session_id: str, command: str, source_hint: str) -> OBDReadResponse:
    session = store.get_session(session_id)
    protocol = session.protocol.upper()
    if source_hint != "can_capture" and protocol.startswith("CAN"):
        raise HTTPException(status_code=400, detail="Session protocol hint is CAN; set source_hint='can_capture'")

    decision = SafetyPolicy.validate_obd_command(command)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)

    result = adapter.query(command)
    history_item = ReadHistoryItem(
        session_id=session.session_id,
        vehicle=session.vehicle,
        vehicle_id=session.vehicle_id,
        command=result.command,
        raw_response=result.raw,
        source_mode=adapter.mode_status(),
        raw_command=result.command,
        parsed_value=result.raw,
        polling=False,
    )
    store.add_read(history_item)

    parsed = {
        "note": "Prototype parser; raw response preserved",
        "source_hint": source_hint,
        "vehicle": session.vehicle,
        "protocol": session.protocol,
    }
    return OBDReadResponse(
        session_id=session.session_id,
        command=result.command,
        raw_response=result.raw,
        parsed=parsed,
    )


def _resolve_session_for_phone(payload: PhoneLiveReadPayload):
    if payload.session_id:
        return store.get_session(payload.session_id)
    if store.active_session_id:
        return store.get_active_session()
    if payload.vehicle_id:
        return store.create_session(vehicle_id=payload.vehicle_id)
    raise HTTPException(status_code=404, detail="Session missing; create or resume a session")


def _touch_phone_bridge(status: str | None = None, error: str | None = None) -> None:
    if status is not None:
        phone_bridge_state["status"] = status
    phone_bridge_state["last_error"] = error
    phone_bridge_state["updated_at"] = datetime.now(timezone.utc).isoformat()


def _mark_ingest_failure(status: str, error: str) -> None:
    phone_bridge_state["last_ingest_status"] = status
    phone_bridge_state["last_ingest_error"] = error
    phone_bridge_state["last_backend_acceptance_status"] = "rejected"
    phone_bridge_state["backend_status"] = "rejected"
    _touch_phone_bridge(error=error)


def _coerce_utc_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reset_phone_connection_progress(*, anchor_now: bool) -> None:
    phone_bridge_state["connection_sequence_started_at"] = (
        datetime.now(timezone.utc).isoformat() if anchor_now else None
    )
    phone_bridge_state["last_command"] = None
    phone_bridge_state["last_response"] = None
    phone_bridge_state["last_latency_ms"] = None
    phone_bridge_state["last_backend_acceptance_status"] = "idle"
    phone_bridge_state["last_ingest_status"] = "idle"
    phone_bridge_state["last_ingest_error"] = None
    phone_bridge_state["backend_status"] = "idle"
    phone_bridge_state["polling_state"] = "inactive"
    phone_bridge_state["live_monitoring_state"] = "inactive"


def _latest_phone_live_read(reads: list[dict]) -> dict | None:
    connection_started_at = _coerce_utc_datetime(phone_bridge_state.get("connection_sequence_started_at"))
    for item in reversed(reads):
        if item.get("source_mode") not in PHONE_LIVE_SOURCE_MODES:
            continue
        if connection_started_at is not None:
            read_ts = _coerce_utc_datetime(item.get("ts"))
            if read_ts is None or read_ts < connection_started_at:
                continue
        return item
    return None


def _mock_mode_reason() -> str:
    return "Mock mode is active only because phone-live and local hardware are both disabled."


def _derive_current_mode(reads: list[dict]) -> tuple[str, str | None]:
    bridge_status = phone_bridge_state.get("status")
    requested_mode = phone_bridge_state.get("source_mode")
    latest_live = _latest_phone_live_read(reads)
    if bridge_status == "connected" and latest_live:
        return "PHONE-LIVE", "Derived from the latest accepted phone-live read in the current connection sequence."
    if bridge_status == "connected":
        return "CONNECTED_WAITING_FOR_FIRST_READ", "Bluetooth is connected and polling has started, but the first live PID read has not been accepted yet."
    if bridge_status == "connecting":
        return "CONNECTING", f"Bluetooth transport is still connecting through {requested_mode or 'the selected bridge'}."
    if bridge_status == "failed":
        return "ERROR", phone_bridge_state.get("fallback_reason") or "Bluetooth transport did not connect."
    if bridge_status == "disconnected" and (
        phone_bridge_state.get("phone_live_requested") or requested_mode in PHONE_LIVE_SOURCE_MODES
    ):
        return "DISCONNECTED", "Bluetooth transport is not connected."
    if latest_live:
        return str(latest_live["source_mode"]), "Derived from the latest accepted phone-live read."
    adapter_mode = adapter.mode_status()
    if adapter_mode == "MOCK":
        return "MOCK", _mock_mode_reason()
    return adapter_mode, None


def _timeline_event_exists(session_id: str, event_type: str) -> bool:
    return any(item.event_type == event_type for item in store.get_timeline_events(session_id))


def _build_connection_diagnostics(latest_live_read: dict | None, current_mode: str) -> dict:
    status = phone_bridge_state.get("status")
    permission_state = phone_bridge_state.get("permission_state") or "unknown"
    return {
        "native_bridge_available": bool(phone_bridge_state.get("supports_native_bluetooth")),
        "bluetooth_permission_status": permission_state,
        "adapter_discovery_started": status in {"connecting", "connected", "failed"},
        "adapter_found": status == "connected",
        "adapter_connection_attempted": status in {"connecting", "connected", "failed"},
        "adapter_connected": status == "connected",
        "first_pid_command_sent": bool(phone_bridge_state.get("last_command")),
        "first_pid_response_received": latest_live_read is not None,
        "backend_ingest_success": phone_bridge_state.get("last_backend_acceptance_status") == "accepted",
        "mode_switched_to_phone_live": current_mode == "PHONE-LIVE",
    }


def _build_phone_bridge_snapshot(active: dict | None = None, reads: list[dict] | None = None) -> dict:
    resolved_active = active
    if resolved_active is None and store.active_session_id:
        resolved_active = store.get_session(store.active_session_id).model_dump()

    resolved_reads = reads
    if resolved_reads is None:
        resolved_reads = [item.model_dump() for item in store.get_reads(resolved_active["session_id"])] if resolved_active else []

    latest_live_read = _latest_phone_live_read(resolved_reads)
    current_mode, current_mode_reason = _derive_current_mode(resolved_reads)
    polling_state = phone_bridge_state.get("polling_state") or "inactive"
    polling_active = phone_bridge_state.get("status") == "connected" and polling_state in {"starting", "active"}
    ai_monitoring_active = (
        phone_bridge_state.get("status") == "connected"
        and phone_bridge_state.get("live_monitoring_state") == "active"
        and latest_live_read is not None
    )

    snapshot = phone_bridge_state.copy()
    snapshot.update({
        "bluetooth_connected": phone_bridge_state.get("status") == "connected",
        "polling_active": polling_active,
        "first_live_read_received": latest_live_read is not None,
        "current_mode": current_mode,
        "current_mode_reason": current_mode_reason,
        "current_source_mode": latest_live_read.get("source_mode") if latest_live_read else phone_bridge_state.get("source_mode"),
        "last_live_pid_command": phone_bridge_state.get("last_command"),
        "last_live_pid_response": phone_bridge_state.get("last_response"),
        "backend_acceptance_status": phone_bridge_state.get("last_backend_acceptance_status"),
        "mock_reason": _mock_mode_reason() if current_mode == "MOCK" else None,
        "last_successful_live_read": latest_live_read,
        "ai_monitoring_active": ai_monitoring_active,
        "connection_diagnostics": _build_connection_diagnostics(latest_live_read, current_mode),
    })
    return snapshot


def _build_ai_monitoring_snapshot(bridge_snapshot: dict, alerts: list[dict]) -> dict:
    if bridge_snapshot.get("ai_monitoring_active"):
        if alerts:
            return {
                "active": True,
                "status": "alerts_active",
                "message": f"{len(alerts)} proactive alert(s) active.",
            }
        return {
            "active": True,
            "status": "active_no_alerts",
            "message": "Live monitoring active, no alerts detected",
        }
    if bridge_snapshot.get("status") == "connected" and not bridge_snapshot.get("first_live_read_received"):
        return {
            "active": False,
            "status": "waiting_for_first_live_read",
            "message": "Waiting for first live PID read.",
        }
    return {
        "active": False,
        "status": "waiting",
        "message": "Waiting for live monitoring.",
    }


def _decode_mode09_vin(raw_response: str) -> str | None:
    cleaned = raw_response.replace(" ", "").replace("\r", "").replace("\n", "").upper()
    marker = "490201"
    if marker not in cleaned:
        return None
    idx = cleaned.index(marker) + len(marker)
    hex_part = ""
    for ch in cleaned[idx:]:
        if ch in "0123456789ABCDEF":
            hex_part += ch
    if len(hex_part) < 34:
        return None
    vin = ""
    for i in range(0, min(len(hex_part), 34), 2):
        pair = hex_part[i : i + 2]
        if len(pair) < 2:
            break
        value = int(pair, 16)
        if 32 <= value <= 126:
            vin += chr(value)
    vin = vin.strip()
    return vin if len(vin) == 17 else None




def _record_learning(
    session_id: str,
    vehicle_id: str,
    source_type: str,
    raw_command: str,
    raw_response: str | None,
    mode: str,
    protocol: str | None,
    parsed_response: dict | None = None,
    before_state_snapshot: dict | None = None,
    after_state_snapshot: dict | None = None,
    tags: list[str] | None = None,
    confidence_score: float | None = None,
    risk_classification: str = "unknown",
    manually_approved_for_replay: bool = False,
    replay_succeeded: bool | None = None,
    notes: str | None = None,
) -> CommandLearningRecord:
    record = CommandLearningRecord(
        session_id=session_id,
        vehicle_id=vehicle_id,
        source_type=source_type,
        raw_command=raw_command.upper(),
        raw_response=raw_response,
        parsed_response=parsed_response,
        protocol=protocol,
        mode=mode,
        before_state_snapshot=before_state_snapshot or {},
        after_state_snapshot=after_state_snapshot or {},
        tags=tags or [],
        confidence_score=confidence_score,
        risk_classification=risk_classification,
        manually_approved_for_replay=manually_approved_for_replay,
        replay_succeeded=replay_succeeded,
        notes=notes,
    )
    store.add_learning_record(record)
    phone_bridge_state["command_learning_status"] = "active"
    return record

def _capture_loop(session_id: str) -> None:
    fast_interval = 1.0
    slow_interval = 4.0
    next_fast = 0.0
    next_slow = 0.0
    source_hint = _resolve_source_hint(store.get_session(session_id).protocol)

    while not capture_stop_event.is_set():
        now = time.time()

        if now >= next_fast:
            for command in CAPTURE_FAST_COMMANDS:
                try:
                    _run_safe_read(session_id=session_id, command=command, source_hint=source_hint)
                except Exception:
                    # Continue safe polling even if one command fails.
                    pass
            next_fast = now + fast_interval

        if now >= next_slow:
            for command in CAPTURE_SLOW_COMMANDS:
                try:
                    _run_safe_read(session_id=session_id, command=command, source_hint=source_hint)
                except Exception:
                    pass
            next_slow = now + slow_interval

        time.sleep(0.1)


def start_capture(vehicle_id: str | None, preset: str) -> dict:
    global capture_thread, capture_status, capture_preset
    with capture_lock:
        if capture_status == "recording":
            session = store.get_active_session()
            return {
                "status": capture_status,
                "session_id": session.session_id,
                "vehicle": session.vehicle,
                "preset": capture_preset,
            }

        if preset not in CAPTURE_PRESETS:
            raise HTTPException(status_code=400, detail="Unknown capture preset")

        if store.active_session_id is None:
            session = store.create_session(vehicle_id=vehicle_id)
        else:
            session = store.get_active_session()

        capture_stop_event.clear()
        capture_thread = threading.Thread(target=_capture_loop, args=(session.session_id,), daemon=True)
        capture_thread.start()
        capture_status = "recording"
        capture_preset = preset
        store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=session.session_id,
            event_type="capture_started",
            title="Capture started",
            detail=f"Capture preset: {CAPTURE_PRESETS[preset]}",
        ))
        return {
            "status": capture_status,
            "session_id": session.session_id,
            "vehicle": session.vehicle,
            "preset": capture_preset,
        }


def stop_capture() -> dict:
    global capture_thread, capture_status
    with capture_lock:
        if capture_status != "recording":
            if capture_status == "idle":
                capture_status = "stopped"
            return {"status": capture_status}

        capture_stop_event.set()
        if capture_thread and capture_thread.is_alive():
            capture_thread.join(timeout=1.5)
        capture_thread = None
        capture_status = "stopped"
        if store.active_session_id:
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=store.active_session_id,
                event_type="capture_stopped",
                title="Capture stopped",
                detail="Capture polling stopped.",
            ))
        return {"status": capture_status}




@app.post("/auth/signup")
def auth_signup(payload: UserSignUpRequest) -> dict:
    try:
        user = store.register_user(email=payload.email, password_hash=_hash_password(payload.password), display_name=payload.display_name)
    except ValueError:
        raise HTTPException(status_code=409, detail="Email already exists")
    token = secrets.token_urlsafe(24)
    auth_tokens[token] = user.user_id
    profile = store.get_user_profile(user.user_id)
    return {"session_token": token, "user": user.model_dump(), "profile": profile.model_dump()}


@app.post("/auth/signin")
def auth_signin(payload: UserSignInRequest) -> dict:
    user = store.authenticate_user(email=payload.email, password_hash=_hash_password(payload.password))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = secrets.token_urlsafe(24)
    auth_tokens[token] = user.user_id
    profile = store.get_user_profile(user.user_id)
    return {"session_token": token, "user": user.model_dump(), "profile": profile.model_dump()}


@app.post("/auth/signout")
def auth_signout(x_session_token: str | None = Header(default=None)) -> dict:
    if x_session_token:
        auth_tokens.pop(x_session_token, None)
    return {"status": "signed_out"}


@app.get("/auth/me")
def auth_me(x_session_token: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(x_session_token)
    user = store.get_user(user_id)
    profile = store.get_user_profile(user_id)
    return {"authenticated": user_id != "demo", "user": user.model_dump(), "profile": profile.model_dump()}


@app.patch("/profile")
def update_profile(payload: UserProfileUpdateRequest, x_session_token: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(x_session_token)
    if user_id == "demo":
        raise HTTPException(status_code=401, detail="Authentication required")
    user, profile = store.update_user_profile(user_id, payload.model_dump(exclude_none=True))
    return {"user": user.model_dump(), "profile": profile.model_dump()}


@app.post("/vehicles")
def add_vehicle(payload: dict, x_session_token: str | None = Header(default=None)) -> dict:
    user_id = _resolve_user_id(x_session_token)
    if user_id == "demo":
        raise HTTPException(status_code=401, detail="Authentication required")
    vehicle = VehicleProfile(
        vehicle_id=payload.get("vehicle_id") or secrets.token_hex(6),
        user_id=user_id,
        label=payload.get("nickname") or payload.get("label") or "Custom Vehicle",
        protocol_hint=payload.get("protocol") or payload.get("protocol_hint") or "ISO_9141_2",
        notes=payload.get("notes"),
    )
    saved = store.create_vehicle(user_id=user_id, vehicle=vehicle)
    return saved.model_dump()


@app.get("/health")
def health() -> dict:
    bridge_snapshot = _build_phone_bridge_snapshot()
    return {
        "ok": True,
        "app_id": settings.app_id,
        "display_name": settings.app_display_name,
        "mode": "read-only",
        "adapter_mode": adapter.mode_status(),
        "current_mode": bridge_snapshot["current_mode"],
        "connection_status": adapter.connection_status(),
        "phone_bridge": bridge_snapshot,
    }


@app.get("/vehicles")
def list_vehicles() -> dict:
    return {"vehicles": [item.model_dump() for item in store.list_vehicles()]}


@app.post("/sessions")
def create_session(payload: SessionCreateRequest | None = None) -> dict:
    vehicle_id = payload.vehicle_id if payload else None
    try:
        session = store.create_session(vehicle_id=vehicle_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Vehicle profile not found")
    return session.model_dump()


@app.get("/sessions")
def list_sessions() -> dict:
    sessions = [s.model_dump() for s in store.sessions.values()]
    return {"active_session_id": store.active_session_id, "sessions": sessions}


@app.post("/sessions/active/stop")
def stop_active_session() -> dict:
    session = store.close_active_session()
    if session is None:
        return {"status": "no_active_session"}
    stop_capture()
    return {"status": "stopped", "session_id": session.session_id}


@app.get("/sessions/active")
def get_active_session() -> dict:
    try:
        session = store.get_active_session()
    except KeyError:
        raise HTTPException(status_code=404, detail="No active session")
    return session.model_dump()


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    try:
        session = store.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    events = [event.model_dump() for event in store.get_events(session_id)]
    reads = [read.model_dump() for read in store.get_reads(session_id)]
    return {"session": session.model_dump(), "events": events, "reads": reads}


@app.get("/sessions/{session_id}/reads")
def get_reads(session_id: str) -> dict:
    try:
        store.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"reads": [read.model_dump() for read in store.get_reads(session_id)]}


@app.post("/sessions/{session_id}/events")
def tag_event(session_id: str, payload: EventTag) -> dict:
    if payload.session_id != session_id:
        raise HTTPException(status_code=400, detail="session_id mismatch")

    try:
        event = store.add_event(payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    return event.model_dump()


@app.post("/obd/read", response_model=OBDReadResponse)
def obd_read(payload: OBDReadRequest) -> OBDReadResponse:
    try:
        session = store.get_session(payload.session_id) if payload.session_id else store.get_active_session()
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    return _run_safe_read(session.session_id, payload.command, payload.source_hint)


@app.post("/obd/read/quick/{read_key}")
def quick_read(read_key: str) -> OBDReadResponse:
    if read_key not in SAFE_QUICK_READS:
        raise HTTPException(status_code=404, detail="Unknown quick-read key")
    try:
        active = store.get_active_session()
    except KeyError:
        raise HTTPException(status_code=404, detail="No active session")

    source_hint = _resolve_source_hint(active.protocol)
    return obd_read(OBDReadRequest(command=SAFE_QUICK_READS[read_key], source_hint=source_hint))


@app.post("/capture/start")
def capture_start(payload: CaptureStartRequest | None = None) -> dict:
    request = payload or CaptureStartRequest()
    try:
        return start_capture(vehicle_id=request.vehicle_id, preset=request.preset)
    except KeyError:
        raise HTTPException(status_code=404, detail="Vehicle profile not found")


@app.post("/capture/stop")
def capture_stop() -> dict:
    return stop_capture()


@app.post("/capture/tag")
def capture_tag(payload: CaptureTagRequest) -> dict:
    if payload.tag not in SAFE_EVENT_TAGS:
        raise HTTPException(status_code=400, detail="Unsupported event tag")
    if capture_status != "recording":
        raise HTTPException(status_code=400, detail="Capture is not recording")
    try:
        session = store.get_active_session()
    except KeyError:
        raise HTTPException(status_code=404, detail="No active session")

    event = EventTag(
        session_id=session.session_id,
        ts=datetime.now(timezone.utc),
        tag=payload.tag,
        note=payload.note,
    )
    return store.add_event(event).model_dump()


@app.get("/phone/bridge/state")
def phone_bridge_state_get() -> dict:
    return _build_phone_bridge_snapshot()


@app.post("/phone/bridge/connect")
def phone_bridge_connect(payload: PhoneBridgeConnectPayload) -> dict:
    previous_status = phone_bridge_state.get("status")
    previous_polling_state = phone_bridge_state.get("polling_state")
    if payload.status in {"connecting", "failed"} or (payload.status == "connected" and previous_status != "connecting"):
        _reset_phone_connection_progress(anchor_now=True)

    _touch_phone_bridge(status=payload.status, error=payload.fallback_reason if payload.status == "failed" else None)
    phone_bridge_state["platform"] = payload.platform
    phone_bridge_state["adapter_name"] = payload.adapter_name
    phone_bridge_state["permission_state"] = payload.permission_state or "unknown"
    phone_bridge_state["source_mode"] = payload.source_mode
    phone_bridge_state["phone_live_requested"] = payload.source_mode == "PHONE-LIVE"
    phone_bridge_state["supports_native_bluetooth"] = payload.supports_native_bluetooth
    phone_bridge_state["fallback_reason"] = payload.fallback_reason
    phone_bridge_state["backend_status"] = (
        "connection-failed"
        if payload.status == "failed"
        else ("awaiting-adapter" if payload.status == "connecting" else ("awaiting-live-read" if payload.status == "connected" else "disconnected"))
    )
    phone_bridge_state["polling_state"] = "starting" if payload.status == "connected" else "inactive"
    if payload.status == "connected":
        phone_bridge_state["last_backend_acceptance_status"] = "pending"
        phone_bridge_state["last_ingest_status"] = "idle"
        phone_bridge_state["last_ingest_error"] = None
    phone_bridge_state["live_monitoring_state"] = "waiting_for_first_live_read" if payload.status == "connected" else "inactive"
    phone_bridge_state["command_learning_status"] = "active" if payload.status == "connected" else "idle"
    if store.active_session_id and payload.status == "connected":
        if previous_status != "connected":
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=store.active_session_id,
                event_type="vehicle_connected",
                title="Vehicle connected",
                detail=f"{payload.adapter_name} connected through hybrid mobile app.",
            ))
        if previous_polling_state == "inactive":
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=store.active_session_id,
                event_type="polling_started",
                title="Polling started",
                detail=f"Phone-live polling armed for {len(phone_bridge_state['configured_gauge_pids'])} gauges at {phone_bridge_state['polling_interval_ms']} ms.",
                metadata={"polling_interval_ms": phone_bridge_state["polling_interval_ms"], "gauge_pids": phone_bridge_state["configured_gauge_pids"]},
            ))
    return _build_phone_bridge_snapshot()


@app.post("/phone/bridge/disconnect")
def phone_bridge_disconnect() -> dict:
    previous_status = phone_bridge_state.get("status")
    _reset_phone_connection_progress(anchor_now=True)
    _touch_phone_bridge(status="disconnected", error=None)
    phone_bridge_state["backend_status"] = "disconnected"
    phone_bridge_state["polling_state"] = "inactive"
    phone_bridge_state["live_monitoring_state"] = "inactive"
    phone_bridge_state["command_learning_status"] = "idle"
    if store.active_session_id and previous_status == "connected":
        store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=store.active_session_id,
            event_type="vehicle_disconnected",
            title="Vehicle disconnected",
            detail="Phone-managed Bluetooth disconnected.",
        ))
    return _build_phone_bridge_snapshot()


@app.post("/phone/bridge/read")
def phone_bridge_read(payload: PhoneLiveReadPayload) -> dict:
    if payload.source_mode not in PHONE_LIVE_SOURCE_MODES:
        detail = "Phone bridge reads must be labeled PHONE-LIVE or BROWSER-DEV"
        _mark_ingest_failure("invalid-source-mode", detail)
        raise HTTPException(status_code=400, detail=detail)

    try:
        session = _resolve_session_for_phone(payload)
    except KeyError:
        detail = "Session not found"
        _mark_ingest_failure("session-missing", detail)
        raise HTTPException(status_code=404, detail=detail)

    prior_reads = [item.model_dump() for item in store.get_reads(session.session_id)]
    had_live_read = _latest_phone_live_read(prior_reads) is not None

    pid_meta = PHONE_LIVE_PID_LABELS.get(payload.command.upper())
    if pid_meta is None:
        detail = "Unsupported PID for phone-live endpoint"
        _mark_ingest_failure("unsupported-pid", detail)
        raise HTTPException(status_code=400, detail=detail)

    mode_label = "PHONE-LIVE" if payload.source_mode == "PHONE-LIVE" else "BROWSER-DEV"

    history_item = ReadHistoryItem(
        session_id=session.session_id,
        vehicle=session.vehicle,
        vehicle_id=session.vehicle_id,
        command=payload.command.upper(),
        raw_response=payload.raw_response,
        source_mode=mode_label,
        pid_key=payload.pid_key or pid_meta["pid_key"],
        value=payload.value,
        parsed_value=payload.value,
        unit=payload.unit or pid_meta["unit"],
        raw_command=payload.command.upper(),
        polling=payload.polling,
        ts=payload.ts,
    )
    store.add_read(history_item)

    phone_bridge_state["last_command"] = payload.command.upper()
    phone_bridge_state["last_response"] = payload.raw_response
    phone_bridge_state["last_latency_ms"] = payload.latency_ms
    phone_bridge_state["source_mode"] = mode_label
    phone_bridge_state["backend_status"] = payload.backend_status or "accepted"
    phone_bridge_state["last_backend_acceptance_status"] = "accepted"
    phone_bridge_state["last_ingest_status"] = "accepted"
    phone_bridge_state["last_ingest_error"] = payload.error
    phone_bridge_state["polling_state"] = "active" if payload.polling else phone_bridge_state.get("polling_state", "inactive")
    phone_bridge_state["live_monitoring_state"] = "active"
    if payload.command.upper() == "0902":
        phone_bridge_state["last_vin_command"] = payload.command.upper()
        phone_bridge_state["last_vin_response"] = payload.raw_response
        parsed_vin = payload.value if isinstance(payload.value, str) and len(str(payload.value)) == 17 else _decode_mode09_vin(payload.raw_response)
        if parsed_vin:
            phone_bridge_state["vin_parse_status"] = "parsed"
            phone_bridge_state["vin"] = parsed_vin
            store.assign_session_vin(session.session_id, parsed_vin, "auto_vin")
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=session.session_id,
                event_type="vin_detected",
                title="VIN detected",
                detail=f"VIN detected automatically: {parsed_vin}",
                metadata={"source": "auto_vin"},
            ))
        else:
            phone_bridge_state["vin_parse_status"] = "failed-manual-selection-required"
            store.assign_session_vin(session.session_id, None, "manual")
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=session.session_id,
                event_type="connection_issue",
                title="VIN parse issue",
                detail="VIN parse failed; manual vehicle selection required.",
            ))
    _touch_phone_bridge(status="connected", error=payload.error)
    if not had_live_read:
        store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=session.session_id,
            event_type="first_live_read_received",
            title="First live read received",
            detail=f"{payload.command.upper()} was accepted from the phone-live bridge and attached to the active vehicle check.",
            metadata={"pid": payload.command.upper(), "polling": payload.polling},
        ))
        if not _timeline_event_exists(session.session_id, "live_monitoring_active"):
            store.add_timeline_event(DiagnosticTimelineEvent(
                session_id=session.session_id,
                event_type="live_monitoring_active",
                title="Live monitoring active",
                detail="AI live monitoring activated after the first successful phone-live read.",
            ))
    _record_learning(
        session_id=session.session_id,
        vehicle_id=session.vehicle_id,
        source_type="obd_request_response",
        raw_command=payload.command.upper(),
        raw_response=payload.raw_response,
        parsed_response={"pid_key": history_item.pid_key, "value": history_item.value, "unit": history_item.unit},
        protocol=session.protocol,
        mode=mode_label,
        risk_classification="safe" if payload.command.upper() in LIVE_POLLING_SUPPORTED_PIDS + ["0902"] else "unknown",
        notes="polling" if payload.polling else "manual",
    )

    current_reads = [item.model_dump() for item in store.get_reads(session.session_id)]
    current_events = [item.model_dump() for item in store.get_events(session.session_id)]
    bridge_snapshot = _build_phone_bridge_snapshot(active=session.model_dump(), reads=current_reads)
    context = _build_ai_context(
        active=session.model_dump(),
        reads=current_reads,
        events=current_events,
        memory=store.get_vehicle_memory(session.vehicle_id),
        bridge_snapshot=bridge_snapshot,
    )
    proactive_alerts = _run_proactive_monitoring(session.session_id, session.vehicle_id, context)

    return {
        "status": "accepted",
        "session_id": session.session_id,
        "vehicle_id": session.vehicle_id,
        "mode": mode_label,
        "read": history_item.model_dump(),
        "backend_acceptance_status": phone_bridge_state["last_backend_acceptance_status"],
        "current_mode": bridge_snapshot["current_mode"],
        "first_live_read_received": bridge_snapshot["first_live_read_received"],
        "live_monitoring_active": bridge_snapshot["ai_monitoring_active"],
        "ai_alerts_generated": proactive_alerts,
        "bridge_state": bridge_snapshot,
    }




@app.post("/phone/vehicle/assign")
def phone_vehicle_assign(payload: SessionCreateRequest) -> dict:
    if not store.active_session_id:
        session = store.create_session(vehicle_id=payload.vehicle_id)
    else:
        session = store.get_active_session()
        if payload.vehicle_id and payload.vehicle_id != session.vehicle_id:
            session = store.create_session(vehicle_id=payload.vehicle_id)
    session = store.assign_session_vin(session.session_id, session.vin, "manual")
    store.add_timeline_event(DiagnosticTimelineEvent(
        session_id=session.session_id,
        event_type="manual_vehicle_selected",
        title="Manual vehicle selected",
        detail=f"Manual vehicle assignment: {session.vehicle}",
        source="user",
    ))
    phone_bridge_state["vin_parse_status"] = "manual-selection"
    return session.model_dump()


@app.post("/learning/ingest")
def learning_ingest(payload: CommandLearningIngestRequest) -> dict:
    session = _resolve_session_for_phone(PhoneLiveReadPayload(
        session_id=payload.session_id,
        vehicle_id=payload.vehicle_id,
        command="010C",
        raw_response=payload.raw_response or "",
        pid_key="rpm",
    ))
    record = _record_learning(
        session_id=session.session_id,
        vehicle_id=session.vehicle_id,
        source_type=payload.source_type,
        raw_command=payload.raw_command,
        raw_response=payload.raw_response,
        parsed_response=payload.parsed_response,
        protocol=payload.protocol or session.protocol,
        mode=payload.mode,
        before_state_snapshot=payload.before_state_snapshot,
        after_state_snapshot=payload.after_state_snapshot,
        tags=payload.tags,
        confidence_score=payload.confidence_score,
        risk_classification=payload.risk_classification,
        manually_approved_for_replay=payload.manually_approved_for_replay,
        replay_succeeded=payload.replay_succeeded,
        notes=payload.notes,
    )
    if payload.source_type == "can_passive":
        phone_bridge_state["passive_can_capture_status"] = "available"
    return {"status": "accepted", "record": record.model_dump()}


@app.get("/learning/library")
def learning_library() -> dict:
    return {"command_library": store.command_library}


@app.get("/learning/session/{session_id}")
def learning_session(session_id: str) -> dict:
    store.get_session(session_id)
    return {"records": [r.model_dump() for r in store.get_learning_records(session_id)]}


@app.post("/learning/replay/approve")
def replay_approve(payload: ReplayApprovalRequest) -> dict:
    store.get_session(payload.session_id)
    approval = store.set_replay_approval(payload.session_id, payload.raw_command, payload.approve, payload.notes)
    return {"status": "ok", "approval": approval, "raw_command": payload.raw_command.upper()}


@app.post("/learning/replay/execute")
def replay_execute(payload: ReplayExecuteRequest) -> dict:
    _ = payload
    raise HTTPException(
        status_code=403,
        detail="Replay execution is permanently blocked in AI Mechanic read-only safety mode",
    )

@app.post("/review/local")
def review_local(payload: ReviewRequest) -> dict:
    try:
        store.get_session(payload.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    return local_llama_review(payload.summary)


@app.post("/review/openai")
def review_openai(payload: ReviewRequest) -> dict:
    try:
        store.get_session(payload.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    result = openai_second_opinion(payload.summary)
    return result.model_dump()


@app.post("/preprocess")
def preprocess_logs(payload: PreprocessRequest) -> dict:
    return run_preprocessing(Path(payload.input_path), Path(payload.output_dir))


def _resolve_vehicle_image(manual_vehicle_id: str | None = None) -> dict:
    active = store.get_session(store.active_session_id).model_dump() if store.active_session_id else None
    resolution = vehicle_image_library.resolve(
        vin=phone_bridge_state.get("vin") if active else None,
        vin_parse_status=phone_bridge_state.get("vin_parse_status"),
        manual_vehicle_id=manual_vehicle_id,
        active_vehicle_id=active.get("vehicle_id") if active else None,
        assignment_source=active.get("assignment_source") if active else None,
    )
    return {
        "vehicle_id": resolution.vehicle_id,
        "year": resolution.year,
        "make": resolution.make,
        "model": resolution.model,
        "trim": resolution.trim,
        "image_asset_path": resolution.image_asset_path,
        "fallback_image_asset_path": resolution.fallback_image_asset_path,
        "model_3d_ref": resolution.model_3d_ref,
        "resolved_from": resolution.resolved_from,
        "lookup_key_used": resolution.lookup_key_used,
    }


@app.get("/vehicle-image/current")
def current_vehicle_image(manual_vehicle_id: str | None = None) -> dict:
    return _resolve_vehicle_image(manual_vehicle_id)


def _compute_vehicle_health_score(reads: list[dict], alerts: list[dict]) -> dict:
    latest = _latest_values(reads, ["rpm", "coolant_temp", "control_module_voltage"])
    score = 100
    factors = []

    dtc_count = sum(1 for item in reads if item.get("command") in {"03", "07"})
    if dtc_count:
        penalty = min(30, dtc_count * 8)
        score -= penalty
        factors.append(f"DTC activity penalty -{penalty}")

    coolant = latest.get("coolant_temp", {}).get("value")
    if isinstance(coolant, (int, float)) and coolant >= 108:
        score -= 18
        factors.append("Coolant temperature elevated")

    voltage = latest.get("control_module_voltage", {}).get("value")
    if isinstance(voltage, (int, float)) and (voltage < 12.2 or voltage > 15.1):
        score -= 14
        factors.append("Control module voltage unstable")

    rpm = latest.get("rpm", {}).get("value")
    if isinstance(rpm, (int, float)) and rpm > 3500:
        score -= 6
        factors.append("RPM outlier observed")

    if alerts:
        penalty = min(20, len(alerts) * 5)
        score -= penalty
        factors.append(f"AI alert activity penalty -{penalty}")

    score = max(0, min(100, int(score)))
    status = "good" if score >= 80 else ("watch" if score >= 60 else "service recommended")
    return {"score": score, "status": status, "factors": factors}


def _vehicle_intelligence_core_snapshot(
    active: dict | None,
    reads: list[dict],
    events: list[dict],
    alerts: list[dict],
    bridge_snapshot: dict,
    ai_monitoring: dict,
) -> dict:
    latest = _latest_values(reads, ["rpm", "coolant_temp", "fuel_level", "control_module_voltage", "vehicle_speed", "throttle_position", "intake_air_temp"])
    dtc_stored = [r for r in reads if r.get("command") == "03" or r.get("pid_key") == "dtc_stored"][-10:]
    dtc_pending = [r for r in reads if r.get("command") == "07" or r.get("pid_key") == "dtc_pending"][-10:]
    freeze = [r for r in reads if r.get("pid_key") == "freeze_frame" or r.get("command") == "020C"][-5:]
    readiness = [r for r in reads if r.get("pid_key") == "readiness_status" or r.get("command") == "0101"][-5:]
    plan = store.get_guided_diagnosis_plan(active["session_id"]) if active else None
    results = store.get_guided_diagnosis_results(active["session_id"]) if active else []
    return {
        "vehicle_identity_manager": {
            "vin": active.get("vin") if active else None,
            "vin_source": active.get("assignment_source") if active else None,
            "active_vehicle_profile": active,
        },
        "live_telemetry_engine": {
            "polling_interval_ms": 500,
            "current_values": latest,
            "rolling_sensor_history": reads[-120:],
            "reconnect_state": bridge_snapshot.get("status"),
            "current_mode": bridge_snapshot.get("current_mode"),
            "polling_state": bridge_snapshot.get("polling_state"),
            "polling_active": bridge_snapshot.get("polling_active"),
            "first_live_read_received": bridge_snapshot.get("first_live_read_received"),
            "last_live_pid_command": bridge_snapshot.get("last_live_pid_command"),
            "last_live_pid_response": bridge_snapshot.get("last_live_pid_response"),
            "backend_acceptance_status": bridge_snapshot.get("backend_acceptance_status"),
            "ai_monitoring": ai_monitoring,
        },
        "diagnostic_state_engine": {
            "stored_dtcs": dtc_stored,
            "pending_dtcs": dtc_pending,
            "freeze_frame_data": freeze,
            "readiness_monitors": readiness,
            "vehicle_health_score": _compute_vehicle_health_score(reads, alerts),
        },
        "timeline_engine": {
            "events": events[-200:],
            "ai_alerts": alerts[-120:],
        },
        "command_learning_engine": {
            "command_library": store.command_library,
            "session_records": store.get_learning_records(active["session_id"]) if active else [],
        },
        "ai_test_assistant": {
            "guided_plan": plan,
            "submitted_results": results[-50:],
        },
    }


@app.get("/vehicle-visualization/state")
def vehicle_visualization_state_view() -> dict:
    return {
        "component_groups": VEHICLE_COMPONENT_GROUPS,
        "highlight": vehicle_visualization_state.copy(),
    }


@app.post("/vehicle-visualization/highlight")
def vehicle_visualization_highlight(payload: VehicleVisualizationHighlightRequest) -> dict:
    if payload.action == "highlight_component" and not payload.component:
        raise HTTPException(status_code=400, detail="component is required for highlight_component")
    if payload.action == "highlight_system" and not payload.system:
        raise HTTPException(status_code=400, detail="system is required for highlight_system")

    component = payload.component.strip().lower() if payload.component else None
    system = payload.system.strip().lower() if payload.system else None
    system_alias = {"cooling": "cooling_system", "electrical": "electrical_system", "fuel": "fuel_system"}
    if system:
        system = system_alias.get(system, system)

    vehicle_visualization_state.update({
        "action": payload.action,
        "component": component,
        "system": system,
        "source": payload.source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "ok", "highlight": vehicle_visualization_state.copy()}


@app.get("/vehicle-visualization/explain")
def vehicle_visualization_explain(component: str) -> dict:
    normalized = component.strip().lower()
    explanation = COMPONENT_EXPLANATIONS.get(normalized, "Component detail not available yet. Use AI Mechanic for deeper diagnostic context.")
    return {"component": normalized, "explanation": explanation}


@app.get("/dashboard/state")
def dashboard_state() -> dict:
    active = None
    if store.active_session_id:
        active = store.get_session(store.active_session_id).model_dump()

    reads = []
    events = []
    if active:
        reads = [item.model_dump() for item in store.get_reads(active["session_id"])]
        events = [item.model_dump() for item in store.get_events(active["session_id"])]

    report_tiers = [
        {"id": "customer_summary", "label": "Customer Summary", "description": "Plain-language, customer-facing visit summary."},
        {"id": "technician_detail", "label": "Technician Detail", "description": "Diagnostic details with observations and data context."},
        {"id": "ai_training_export", "label": "AI Training Export", "description": "Structured export scaffold for model training review."},
    ]

    learning_records = [item.model_dump() for item in store.get_learning_records(active["session_id"])] if active else []
    alerts = [item.model_dump() for item in store.get_ai_alerts(active["session_id"])] if active else []
    bridge_snapshot = _build_phone_bridge_snapshot(active=active, reads=reads)
    ai_monitoring = _build_ai_monitoring_snapshot(bridge_snapshot, alerts)
    core_snapshot = _vehicle_intelligence_core_snapshot(active, reads, events, alerts, bridge_snapshot, ai_monitoring)

    current_user = store.get_user(store._default_user_id(None))
    current_profile = store.get_user_profile(current_user.user_id)
    return {
        "app_id": settings.app_id,
        "display_name": settings.app_display_name,
        "current_user": current_user.model_dump(),
        "current_profile": current_profile.model_dump(),
        "vehicles": [item.model_dump() for item in store.list_vehicles()],
        "active_session": active,
        "adapter_mode": adapter.mode_status(),
        "current_mode": bridge_snapshot["current_mode"],
        "current_mode_reason": bridge_snapshot["current_mode_reason"],
        "connection_status": adapter.connection_status(),
        "phone_bridge": bridge_snapshot,
        "capture_status": capture_status,
        "capture_preset": capture_preset,
        "capture_presets": CAPTURE_PRESETS,
        "event_tags": SAFE_EVENT_TAGS,
        "quick_reads": SAFE_QUICK_READS,
        "recent_reads": reads[-40:],
        "recent_events": events[-40:],
        "last_successful_read": reads[-1] if reads else None,
        "report_tiers": report_tiers,
        "polling_supported_pids": LIVE_POLLING_SUPPORTED_PIDS,
        "learning_records": learning_records[-40:],
        "learning_library": store.command_library,
        "vehicle_image": _resolve_vehicle_image(active.get("vehicle_id") if active else None),
        "ai_monitoring": ai_monitoring,
        "vehicle_visualization": {"component_groups": VEHICLE_COMPONENT_GROUPS, "highlight": vehicle_visualization_state.copy()},
        "ai_alerts": [item.model_dump() for item in store.get_ai_alerts(active["session_id"])] if active else [],
        "diagnostic_timeline": [item.model_dump() for item in store.get_timeline_events(active["session_id"])] if active else [],
        "ai_response_history": [item.model_dump() for item in store.get_ai_responses(active["session_id"])] if active else [],
        "ai_debug": {
            "last_ai_request": store.last_ai_request,
            "last_ai_response_timestamp": store.last_ai_response_timestamp,
        },
        "vehicle_intelligence_core": core_snapshot,
        "vehicle_health_score": core_snapshot.get("diagnostic_state_engine", {}).get("vehicle_health_score"),
        "report_integration_hooks": {
            "ai_summary": True,
            "notable_live_data_alerts": True,
            "timeline_highlights": True,
            "plain_english_recommendations": True,
        },
    }




@app.get("/ai/memory/{vehicle_id}")
def ai_memory(vehicle_id: str) -> dict:
    return {"vehicle_id": vehicle_id, "memory": store.get_vehicle_memory(vehicle_id)}


@app.post("/ai/memory/{vehicle_id}")
def ai_memory_update(vehicle_id: str, payload: dict) -> dict:
    memory = store.update_vehicle_memory(vehicle_id, payload)
    return {"status": "updated", "vehicle_id": vehicle_id, "memory": memory}


@app.get("/ai/knowledge")
def ai_knowledge() -> dict:
    return {"knowledge_library": store.knowledge_library}


def _latest_values(reads: list[dict], keys: list[str]) -> dict:
    result = {}
    for key in keys:
        for item in reversed(reads):
            if item.get("pid_key") == key:
                result[key] = {
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "source_mode": item.get("source_mode"),
                    "ts": item.get("ts"),
                }
                break
    return result


def _build_ai_context(active: dict | None, reads: list[dict], events: list[dict], memory: dict, bridge_snapshot: dict) -> dict:
    live_keys = ["rpm", "coolant_temp", "fuel_level", "control_module_voltage", "vehicle_speed", "throttle_position", "intake_air_temp"]
    latest_values = _latest_values(reads, live_keys)
    recent_live = [
        r
        for r in reads
        if r.get("pid_key") in set(live_keys + ["dtc_stored", "dtc_pending", "readiness_status", "freeze_frame", "vin"])
    ][-25:]
    dtc_stored = [r for r in reads if r.get("command") == "03" or r.get("pid_key") == "dtc_stored"][-8:]
    dtc_pending = [r for r in reads if r.get("command") == "07" or r.get("pid_key") == "dtc_pending"][-8:]
    freeze_frames = [r for r in reads if r.get("pid_key") == "freeze_frame" or r.get("command") == "020C"][-5:]
    readiness = [r for r in reads if r.get("pid_key") == "readiness_status" or r.get("command") == "0101"][-5:]

    return {
        "current_vehicle": active.get("vehicle") if active else None,
        "vehicle_id": active.get("vehicle_id") if active else "toyota_sienna_2006",
        "vin": active.get("vin") if active else None,
        "vehicle_assignment_source": active.get("assignment_source") if active else "manual",
        "current_mode": bridge_snapshot.get("current_mode"),
        "active_vehicle_check": active.get("session_id") if active else None,
        "stored_dtcs": dtc_stored,
        "pending_dtcs": dtc_pending,
        "freeze_frame_data": freeze_frames,
        "readiness_monitor_status": readiness,
        "recent_live_sensor_readings": recent_live,
        "current_4_gauge_values": latest_values,
        "event_tags": events[-15:],
        "prior_vehicle_history": memory.get("prior_vehicle_checks", []),
        "technician_notes": memory.get("notes", []),
        "report_summaries": memory.get("report_summaries", []),
        "confidence_tags": memory.get("confidence_tags", []),
        "timeline_events": store.get_timeline_events(active["session_id"])[-60:] if active else [],
        "component_mappings": store.knowledge_library.get("component_mappings", {}),
        "unresolved_issues": memory.get("unresolved_issues", []),
        "false_positives": memory.get("false_positives", []),
        "safety_guardrails": {
            "read_only": True,
            "blocked": [
                "actuator commands",
                "replay execution",
                "code clearing",
                "vehicle control actions",
            ],
        },
        "vehicle_component_groups": VEHICLE_COMPONENT_GROUPS,
    }


def _resolve_visualization_request(payload: AIMechanicQuestionRequest) -> dict | None:
    component = payload.memory_updates.get("highlight_component")
    system = payload.memory_updates.get("highlight_system") or payload.memory_updates.get("system")
    system_alias = {
        "cooling": "cooling_system",
        "electrical": "electrical_system",
        "fuel": "fuel_system",
    }
    normalized_system = system.strip().lower() if isinstance(system, str) and system.strip() else None
    if normalized_system:
        normalized_system = system_alias.get(normalized_system, normalized_system)
    if isinstance(component, str) and component.strip():
        return {"action": "highlight_component", "component": component.strip().lower(), "system": normalized_system}
    if normalized_system:
        return {"action": "highlight_system", "system": normalized_system}
    return None


def _run_proactive_monitoring(session_id: str, vehicle_id: str, context: dict) -> list[dict]:
    alerts: list[AIAlertRecord] = []
    gauge = context.get("current_4_gauge_values", {})
    existing_alert_keys = {
        (item.title, item.trigger_reason)
        for item in store.get_ai_alerts(session_id)
    }

    coolant = gauge.get("coolant_temp", {}).get("value")
    rpm = gauge.get("rpm", {}).get("value")
    voltage = gauge.get("control_module_voltage", {}).get("value")

    if isinstance(coolant, (int, float)) and coolant >= 108:
        alerts.append(AIAlertRecord(
            session_id=session_id,
            vehicle_id=vehicle_id,
            title="Coolant temperature is rising unusually",
            explanation="Engine coolant temperature is above expected warm range.",
            trigger_reason=f"coolant_temp={coolant}°C",
            confidence="likely",
            suggested_next_step="Monitor fan operation and check coolant level before further driving.",
            related_sensors=["coolant_temp"],
        ))

    if isinstance(rpm, (int, float)) and 550 <= rpm <= 1100:
        recent = [r.get("value") for r in context.get("recent_live_sensor_readings", []) if r.get("pid_key") == "rpm" and isinstance(r.get("value"), (int, float))][-8:]
        if len(recent) >= 4 and (max(recent) - min(recent)) > 220:
            alerts.append(AIAlertRecord(
                session_id=session_id,
                vehicle_id=vehicle_id,
                title="RPM appears unstable at idle",
                explanation="RPM variability in recent idle samples is wider than expected.",
                trigger_reason=f"idle rpm spread={max(recent)-min(recent):.1f}",
                confidence="suspected",
                suggested_next_step="Check for vacuum leaks and review fuel trim behavior.",
                related_sensors=["rpm"],
            ))

    if isinstance(voltage, (int, float)) and (voltage < 12.2 or voltage > 15.1):
        alerts.append(AIAlertRecord(
            session_id=session_id,
            vehicle_id=vehicle_id,
            title="Control module voltage outside expected range",
            explanation="Battery/charging voltage is outside normal operating window.",
            trigger_reason=f"control_module_voltage={voltage}V",
            confidence="likely",
            suggested_next_step="Inspect battery terminals and charging system output.",
            related_sensors=["control_module_voltage"],
        ))

    new_alerts: list[dict] = []
    for alert in alerts:
        alert_key = (alert.title, alert.trigger_reason)
        if alert_key in existing_alert_keys:
            continue
        existing_alert_keys.add(alert_key)
        store.add_ai_alert(alert)
        timeline_event = store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=session_id,
            event_type="ai_alert_created",
            title=alert.title,
            detail=alert.explanation,
            source="ai",
            related_sensors=alert.related_sensors,
            related_codes=alert.related_codes,
            linked_ai_alert_id=alert.alert_id,
            metadata={"confidence": alert.confidence, "proactive": True},
        ))
        _ = timeline_event
        new_alerts.append(alert.model_dump())
    return new_alerts


@app.post("/ai/guided-diagnosis/run")
def run_guided_diagnosis(payload: GuidedDiagnosisRunRequest) -> dict:
    if payload.session_id:
        session = store.get_session(payload.session_id)
    elif store.active_session_id:
        session = store.get_active_session()
    else:
        raise HTTPException(status_code=404, detail="Start a vehicle check before running guided diagnosis")

    reads = [item.model_dump() for item in store.get_reads(session.session_id)]
    coolant = _latest_values(reads, ["coolant_temp"]).get("coolant_temp", {}).get("value")

    ranked_causes = [
        {"cause": "Air/fuel imbalance", "confidence": "suspected"},
        {"cause": "Ignition performance issue", "confidence": "suspected"},
        {"cause": "Cooling system instability" if isinstance(coolant, (int, float)) and coolant > 105 else "Charging system instability", "confidence": "monitor only"},
    ]
    recommended_tests = [
        {"step_id": "gd-1", "name": "Warm idle baseline", "instruction": "Observe idle RPM for 60 seconds.", "expected_sensor_values": {"rpm": "650-850"}},
        {"step_id": "gd-2", "name": "Cooling trend check", "instruction": "Monitor coolant temp for 3 minutes at idle.", "expected_sensor_values": {"coolant_temp": "85-102°C"}},
        {"step_id": "gd-3", "name": "Charging stability check", "instruction": "Monitor control module voltage with headlights on.", "expected_sensor_values": {"control_module_voltage": "13.2-14.8V"}},
    ]
    plan = {
        "session_id": session.session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symptom": payload.symptom or "general diagnostic",
        "ranked_possible_causes": ranked_causes,
        "recommended_tests": recommended_tests,
        "dynamic_tree_state": "open",
    }
    store.set_guided_diagnosis_plan(session.session_id, plan)
    store.add_timeline_event(DiagnosticTimelineEvent(
        session_id=session.session_id,
        event_type="ai_advice_generated",
        title="Run Guided Diagnosis created plan",
        detail=f"Plan generated with {len(recommended_tests)} read-only test steps.",
        source="ai",
        metadata={"feature": "run_guided_diagnosis"},
    ))
    return {"status": "ok", "plan": plan, "read_only_enforced": True}


@app.post("/ai/guided-diagnosis/result")
def submit_guided_diagnosis_result(payload: GuidedDiagnosisResultSubmitRequest) -> dict:
    store.get_session(payload.session_id)
    result = {
        "step_id": payload.step_id,
        "observed_result": payload.observed_result,
        "notes": payload.notes,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    store.add_guided_diagnosis_result(payload.session_id, result)
    store.add_timeline_event(DiagnosticTimelineEvent(
        session_id=payload.session_id,
        event_type="user_tag_event",
        title="Guided diagnosis result submitted",
        detail=f"{payload.step_id}: {payload.observed_result}",
        source="user",
        metadata={"feature": "run_guided_diagnosis", "step_id": payload.step_id},
    ))

    existing_plan = store.get_guided_diagnosis_plan(payload.session_id)
    if existing_plan:
        existing_plan["dynamic_tree_state"] = "updated_after_result"
        existing_plan["last_result_step"] = payload.step_id
        store.set_guided_diagnosis_plan(payload.session_id, existing_plan)

    return {
        "status": "accepted",
        "result": result,
        "updated_plan": store.get_guided_diagnosis_plan(payload.session_id),
    }


@app.get("/diagnostic-timeline/{session_id}")
def diagnostic_timeline(session_id: str) -> dict:
    store.get_session(session_id)
    return {"session_id": session_id, "timeline": [item.model_dump() for item in store.get_timeline_events(session_id)]}


@app.get("/ai/alerts/{session_id}")
def ai_alerts(session_id: str) -> dict:
    store.get_session(session_id)
    return {"session_id": session_id, "alerts": [item.model_dump() for item in store.get_ai_alerts(session_id)]}


@app.post("/ai/mechanic")
def ai_mechanic(payload: AIMechanicQuestionRequest) -> dict:
    question = payload.question.strip()
    memory_updates = payload.memory_updates or {}

    active = store.get_session(store.active_session_id).model_dump() if store.active_session_id else None
    if not active:
        return {
            "answer": "Start a vehicle check first so AI Mechanic can use live context.",
            "context": {},
            "response_basis": "general_knowledge",
            "read_only_enforced": True,
            "visualization_hook": _resolve_visualization_request(payload),
        }

    reads = [item.model_dump() for item in store.get_reads(active["session_id"])]
    events = [item.model_dump() for item in store.get_events(active["session_id"])]

    vehicle_id = active.get("vehicle_id")
    memory = store.get_vehicle_memory(vehicle_id)
    if active.get("vin"):
        memory_updates.setdefault("vin", active["vin"])
    if memory_updates:
        memory = store.update_vehicle_memory(vehicle_id, memory_updates)

    bridge_snapshot = _build_phone_bridge_snapshot(active=active, reads=reads)
    context = _build_ai_context(active=active, reads=reads, events=events, memory=memory, bridge_snapshot=bridge_snapshot)
    proactive_alerts = _run_proactive_monitoring(active["session_id"], vehicle_id, context)

    response_basis = "live_data" if context.get("recent_live_sensor_readings") else ("stored_history" if memory.get("prior_vehicle_checks") else "general_knowledge")
    if not question:
        answer = "Ask your question by text or microphone. I can proactively monitor live data and provide read-only diagnostic advice."
    else:
        answer = (
            f"Confirmed facts: active vehicle is {context.get('current_vehicle') or 'not selected'}, "
            f"VIN is {context.get('vin') or 'not available'}, and mode is {context.get('current_mode')}. "
            "Likely causes: based on DTCs + live data patterns in this check only. "
            "Suggested next checks: I will recommend safe read-only tests and observations. "
            "Uncertainty: any conclusion with limited evidence will be labeled suspected/likely/monitor only. "
            f"Question received: {question}"
        )

    request_record = {
        "question": question,
        "session_id": active["session_id"],
        "vehicle_id": vehicle_id,
        "used_live_data": bool(context.get("recent_live_sensor_readings")),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    store.last_ai_request = request_record

    response_record = store.add_ai_response(AIResponseRecord(
        session_id=active["session_id"],
        vehicle_id=vehicle_id,
        question=question,
        answer=answer,
        response_basis=response_basis,
        used_live_data=bool(context.get("recent_live_sensor_readings")),
        proactive=False,
        context_summary={
            "stored_dtcs": len(context.get("stored_dtcs", [])),
            "pending_dtcs": len(context.get("pending_dtcs", [])),
            "recent_live_points": len(context.get("recent_live_sensor_readings", [])),
        },
    ))

    timeline = store.add_timeline_event(DiagnosticTimelineEvent(
        session_id=active["session_id"],
        event_type="ai_advice_generated",
        title="AI advice generated",
        detail=f"AI Mechanic answered: {question or 'quick-open'}",
        source="ai",
        linked_ai_response_id=response_record.response_id,
        metadata={"response_basis": response_basis, "used_live_data": bool(context.get("recent_live_sensor_readings"))},
    ))

    return {
        "answer": answer,
        "context": context,
        "response_basis": response_basis,
        "read_only_enforced": True,
        "visualization_hook": _resolve_visualization_request(payload),
        "conversation_history": [r.model_dump() for r in store.get_ai_responses(active["session_id"])][-20:],
        "proactive_alerts": proactive_alerts,
        "timeline_event_id": timeline.timeline_event_id,
        "debug": {
            "last_ai_request": store.last_ai_request,
            "last_ai_response_timestamp": store.last_ai_response_timestamp,
            "used_live_data": bool(context.get("recent_live_sensor_readings")),
            "request_kind": "user-requested" if payload.source == "user" else "proactive",
            "linked_timeline_event_id": timeline.timeline_event_id,
        },
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Zeb's OBD AI Dashboard</title>
  <style>
    :root {
      --bg: #0B0F14;
      --panel: #141B23;
      --panel-elevated: #19222C;
      --panel-strong: #1F2A35;
      --accent: #00E5FF;
      --accent-soft: rgba(0,229,255,0.18);
      --success: #32D74B;
      --warning: #FF9F0A;
      --danger: #FF453A;
      --text: #F5F7FA;
      --text-secondary: #A7B0BA;
      --border: rgba(255,255,255,0.08);
      --border-strong: rgba(255,255,255,0.14);
      --shadow-lg: 0 22px 48px rgba(0,0,0,0.34);
      --shadow-md: 0 14px 30px rgba(0,0,0,0.26);
      --radius-lg: 24px;
      --radius-md: 20px;
      --radius-sm: 16px;
    }

    * { box-sizing: border-box; }
    html { color-scheme: dark; background: var(--bg); }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(0,229,255,0.14), transparent 28%),
        radial-gradient(circle at top right, rgba(50,215,75,0.07), transparent 20%),
        linear-gradient(180deg, #091018 0%, #0B0F14 52%, #0A0E13 100%);
      position: relative;
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.38;
      background:
        linear-gradient(transparent 0, transparent calc(100% - 1px), rgba(255,255,255,0.02) calc(100% - 1px)),
        linear-gradient(90deg, transparent 0, transparent calc(100% - 1px), rgba(255,255,255,0.02) calc(100% - 1px));
      background-size: 100% 120px, 120px 100%;
      mask-image: radial-gradient(circle at center, black 40%, transparent 88%);
    }
    ::selection { background: rgba(0,229,255,0.28); color: var(--text); }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .wrap {
      max-width: 1220px;
      margin: 0 auto;
      padding: 18px 14px 42px;
      display: grid;
      gap: 16px;
      position: relative;
      z-index: 1;
    }
    .card {
      position: relative;
      overflow: hidden;
      border-radius: var(--radius-lg);
      border: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(25,34,44,0.98) 0%, rgba(20,27,35,0.96) 100%);
      box-shadow: var(--shadow-lg);
      padding: 18px;
      animation: panelIn 0.55s ease both;
    }
    .card::after {
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: calc(var(--radius-lg) - 1px);
      border: 1px solid rgba(255,255,255,0.02);
      pointer-events: none;
    }
    .hero-card {
      padding: 22px;
      background:
        radial-gradient(circle at top right, rgba(0,229,255,0.12), transparent 34%),
        linear-gradient(155deg, rgba(25,34,44,0.99) 0%, rgba(16,21,28,0.98) 100%);
    }
    .hero-grid,
    .inspection-grid,
    .support-grid {
      display: grid;
      gap: 16px;
    }
    .hero-main,
    .hero-panel,
    .inspection-sidebar {
      display: grid;
      gap: 14px;
    }
    .hero-title,
    .metric-value,
    .info-title {
      font-family: "Eurostile", "Avenir Next", "Segoe UI", sans-serif;
    }
    .section-kicker,
    .field-label,
    .status-label,
    .metric-label,
    .detail-label {
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 0.72rem;
      color: var(--text-secondary);
    }
    .section-kicker { color: var(--accent); }
    .section-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }
    .section-title {
      margin: 6px 0 0;
      font-size: 1.16rem;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .section-copy,
    .hero-copy,
    .tiny,
    .panel-note {
      color: var(--text-secondary);
      line-height: 1.55;
    }
    .tiny { font-size: 0.84rem; }
    .hero-title {
      margin: 6px 0 0;
      font-size: clamp(1.7rem, 5vw, 2.7rem);
      line-height: 1.08;
      letter-spacing: 0.02em;
    }
    .hero-copy {
      margin: 0;
      max-width: 60ch;
      font-size: 0.96rem;
    }
    .hero-panel {
      align-content: space-between;
      padding: 18px;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(11,15,20,0.72) 0%, rgba(16,21,28,0.94) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    }
    .hero-badges,
    .ai-quick {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .badge,
    .component-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
      font-size: 0.82rem;
      font-weight: 700;
      color: var(--text);
      min-height: 40px;
    }
    .badge-dot,
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(167,176,186,0.44);
      box-shadow: 0 0 0 0 rgba(0,229,255,0);
      flex: 0 0 auto;
    }
    .badge[data-tone="accent"] .badge-dot,
    .status-indicator[data-tone="accent"] .status-dot,
    .component-pill[data-tone="accent"] .status-dot {
      background: var(--accent);
      box-shadow: 0 0 0 6px rgba(0,229,255,0.14);
    }
    .badge[data-tone="success"] .badge-dot,
    .status-indicator[data-tone="success"] .status-dot,
    .component-pill[data-tone="success"] .status-dot {
      background: var(--success);
      box-shadow: 0 0 0 6px rgba(50,215,75,0.12);
    }
    .badge[data-tone="warning"] .badge-dot,
    .status-indicator[data-tone="warning"] .status-dot,
    .component-pill[data-tone="warning"] .status-dot {
      background: var(--warning);
      box-shadow: 0 0 0 6px rgba(255,159,10,0.13);
    }
    .badge[data-tone="danger"] .badge-dot,
    .status-indicator[data-tone="danger"] .status-dot,
    .component-pill[data-tone="danger"] .status-dot {
      background: var(--danger);
      box-shadow: 0 0 0 6px rgba(255,69,58,0.14);
    }
    .status-strip,
    .telemetry-grid,
    .action-grid,
    .tool-grid {
      display: grid;
      gap: 12px;
    }
    .status-strip {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .status-indicator {
      display: flex;
      align-items: center;
      gap: 12px;
      min-height: 74px;
      padding: 12px 14px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: rgba(11,15,20,0.42);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .status-value {
      display: block;
      margin-top: 4px;
      font-size: 1rem;
      font-weight: 700;
      color: var(--text);
    }
    .telemetry-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .metric-card {
      position: relative;
      min-height: 118px;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(25,34,44,0.98) 0%, rgba(15,20,26,0.94) 100%);
      box-shadow: var(--shadow-md);
      padding: 16px;
      overflow: hidden;
    }
    .metric-card::before {
      content: "";
      position: absolute;
      inset: auto 16px 16px 16px;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(0,229,255,0.42), transparent);
      opacity: 0.6;
    }
    .metric-card[data-live="true"] {
      border-color: rgba(0,229,255,0.24);
      box-shadow:
        var(--shadow-md),
        0 0 0 1px rgba(0,229,255,0.05),
        0 0 24px rgba(0,229,255,0.08);
    }
    .metric-value {
      margin-top: 16px;
      font-size: clamp(1.45rem, 5vw, 2.02rem);
      font-weight: 700;
      line-height: 1.05;
    }
    .metric-meta {
      margin-top: 14px;
      font-size: 0.82rem;
      color: var(--text-secondary);
    }
    .inspection-grid {
      align-items: start;
    }
    .vehicle-stage {
      position: relative;
      padding: 12px;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      background:
        radial-gradient(circle at top, rgba(0,229,255,0.08), transparent 50%),
        linear-gradient(180deg, rgba(9,13,18,0.98) 0%, rgba(14,18,24,0.96) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
      min-height: 260px;
    }
    .vehicle-stage::after {
      content: "";
      position: absolute;
      inset: 12px;
      border-radius: calc(var(--radius-md) - 6px);
      border: 1px solid rgba(255,255,255,0.03);
      pointer-events: none;
    }
    .vehicle-canvas {
      width: 100%;
      height: min(58vw, 380px);
      min-height: 240px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.06);
      background:
        radial-gradient(circle at top, rgba(0,229,255,0.10), transparent 42%),
        linear-gradient(180deg, #05080D 0%, #101720 100%);
      touch-action: manipulation;
      overflow: hidden;
    }
    .vehicle-fallback {
      display: none;
      width: 100%;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.02);
    }
    .info-panel {
      display: grid;
      gap: 12px;
      padding: 16px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(17,23,30,0.92) 0%, rgba(13,17,22,0.95) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .info-title {
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      color: var(--text);
    }
    button,
    select,
    input {
      font: inherit;
    }
    button {
      width: 100%;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--text);
      cursor: pointer;
      transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
      -webkit-tap-highlight-color: transparent;
    }
    button:hover {
      transform: translateY(-1px);
      border-color: rgba(0,229,255,0.22);
      box-shadow:
        0 16px 30px rgba(0,0,0,0.24),
        0 0 0 1px rgba(0,229,255,0.04);
    }
    button:active { transform: translateY(0); }
    button[disabled] {
      cursor: not-allowed;
      opacity: 0.58;
      box-shadow: none;
      transform: none;
    }
    select {
      width: 100%;
      min-height: 54px;
      border-radius: 16px;
      padding: 14px 44px 14px 14px;
      border: 1px solid var(--border-strong);
      background:
        linear-gradient(135deg, rgba(17,23,30,1) 0%, rgba(13,17,22,0.98) 100%);
      color: var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      appearance: none;
    }
    button:focus-visible,
    select:focus-visible,
    input:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .action-banner,
    .action-tile,
    .tool-tile,
    .panel-button,
    .quick-chip {
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-md);
      box-shadow: 0 16px 30px rgba(0,0,0,0.22);
    }
    .action-banner,
    .action-tile,
    .tool-tile,
    .panel-button {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px;
      text-align: left;
      background: linear-gradient(180deg, rgba(25,34,44,0.98) 0%, rgba(16,21,28,0.96) 100%);
      min-height: 82px;
    }
    .action-banner[data-tone="accent"],
    .action-tile[data-tone="accent"],
    .tool-tile[data-tone="accent"] {
      border-color: rgba(0,229,255,0.24);
      background: linear-gradient(135deg, rgba(0,229,255,0.10) 0%, rgba(18,26,34,0.96) 48%, rgba(13,17,22,0.98) 100%);
    }
    .action-banner[data-tone="success"],
    .action-tile[data-tone="success"],
    .tool-tile[data-tone="success"] {
      border-color: rgba(50,215,75,0.26);
      background: linear-gradient(135deg, rgba(50,215,75,0.12) 0%, rgba(17,25,21,0.96) 52%, rgba(13,17,22,0.98) 100%);
    }
    .action-banner[data-tone="danger"],
    .action-tile[data-tone="danger"],
    .tool-tile[data-tone="danger"] {
      border-color: rgba(255,69,58,0.26);
      background: linear-gradient(135deg, rgba(255,69,58,0.11) 0%, rgba(29,18,18,0.96) 52%, rgba(13,17,22,0.98) 100%);
    }
    .action-banner[data-tone="warning"],
    .action-tile[data-tone="warning"],
    .tool-tile[data-tone="warning"] {
      border-color: rgba(255,159,10,0.24);
      background: linear-gradient(135deg, rgba(255,159,10,0.10) 0%, rgba(31,22,14,0.96) 52%, rgba(13,17,22,0.98) 100%);
    }
    .action-banner[data-active="true"],
    .action-tile[data-active="true"],
    .tool-tile[data-active="true"] {
      box-shadow:
        0 20px 34px rgba(0,0,0,0.28),
        0 0 0 1px rgba(255,255,255,0.03),
        0 0 26px rgba(0,229,255,0.08);
    }
    .action-banner[data-pulse="true"],
    .action-tile[data-pulse="true"],
    .tool-tile[data-pulse="true"],
    .badge[data-pulse="true"] {
      animation: pulseGlow 2.2s ease-in-out infinite;
    }
    .action-grid,
    .tool-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .action-tile,
    .tool-tile {
      flex-direction: column;
      align-items: flex-start;
      justify-content: space-between;
      min-height: 138px;
    }
    .tool-tile {
      min-height: 120px;
      padding: 16px;
    }
    .tool-tile[data-feature="signature"] {
      border-color: rgba(0,229,255,0.28);
      background:
        radial-gradient(circle at top right, rgba(0,229,255,0.14), transparent 36%),
        linear-gradient(180deg, rgba(25,34,44,0.99) 0%, rgba(14,18,24,0.96) 100%);
      box-shadow:
        0 18px 34px rgba(0,0,0,0.28),
        0 0 0 1px rgba(0,229,255,0.05),
        0 0 26px rgba(0,229,255,0.10);
    }
    .tile-lead {
      display: flex;
      align-items: center;
      gap: 14px;
      width: 100%;
    }
    .tile-copy {
      display: flex;
      flex-direction: column;
      gap: 5px;
      min-width: 0;
    }
    .tile-title {
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      color: var(--text);
    }
    .tile-meta {
      font-size: 0.84rem;
      color: var(--text-secondary);
      line-height: 1.5;
    }
    .tile-state {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.05);
      color: var(--text);
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.10em;
    }
    .tile-icon {
      display: grid;
      place-items: center;
      width: 50px;
      height: 50px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(8,12,18,0.54);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      color: var(--text);
      flex: 0 0 auto;
    }
    .tile-icon svg { width: 22px; height: 22px; }
    .action-banner[data-tone="accent"] .tile-icon,
    .action-tile[data-tone="accent"] .tile-icon,
    .tool-tile[data-tone="accent"] .tile-icon,
    .panel-button[data-tone="accent"] .tile-icon {
      color: var(--accent);
      border-color: rgba(0,229,255,0.22);
      background: rgba(0,229,255,0.12);
    }
    .action-banner[data-tone="success"] .tile-icon,
    .action-tile[data-tone="success"] .tile-icon,
    .tool-tile[data-tone="success"] .tile-icon,
    .panel-button[data-tone="success"] .tile-icon {
      color: var(--success);
      border-color: rgba(50,215,75,0.22);
      background: rgba(50,215,75,0.10);
    }
    .action-banner[data-tone="danger"] .tile-icon,
    .action-tile[data-tone="danger"] .tile-icon,
    .tool-tile[data-tone="danger"] .tile-icon,
    .panel-button[data-tone="danger"] .tile-icon {
      color: var(--danger);
      border-color: rgba(255,69,58,0.22);
      background: rgba(255,69,58,0.10);
    }
    .action-banner[data-tone="warning"] .tile-icon,
    .action-tile[data-tone="warning"] .tile-icon,
    .tool-tile[data-tone="warning"] .tile-icon,
    .panel-button[data-tone="warning"] .tile-icon {
      color: var(--warning);
      border-color: rgba(255,159,10,0.22);
      background: rgba(255,159,10,0.10);
    }
    .panel-button {
      min-height: 76px;
      padding: 14px 16px;
    }
    .panel-button--ghost {
      background: linear-gradient(180deg, rgba(18,24,30,0.96) 0%, rgba(12,16,21,0.96) 100%);
    }
    .quick-chip {
      width: auto;
      min-height: 42px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(10,14,20,0.82);
      color: var(--text);
      border: 1px solid var(--border);
      font-size: 0.88rem;
      line-height: 1.2;
    }
    .ai-panel {
      margin-top: 16px;
      padding: 18px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(0,229,255,0.20);
      background:
        radial-gradient(circle at top right, rgba(0,229,255,0.14), transparent 35%),
        linear-gradient(180deg, rgba(19,27,35,0.99) 0%, rgba(12,16,21,0.96) 100%);
      box-shadow:
        0 18px 36px rgba(0,0,0,0.28),
        0 0 0 1px rgba(0,229,255,0.04),
        0 0 28px rgba(0,229,255,0.10);
    }
    .support-grid {
      gap: 16px;
    }
    .info-card {
      display: grid;
      gap: 14px;
      min-height: 100%;
    }
    .detail-list,
    .alert-list,
    .timeline-list {
      display: grid;
      gap: 10px;
    }
    .detail-row,
    .timeline-item,
    .alert-item {
      display: grid;
      gap: 6px;
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(10,14,20,0.62);
    }
    .detail-row {
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
    }
    .detail-value {
      color: var(--text);
      font-weight: 600;
      text-align: right;
      word-break: break-word;
    }
    .alert-head,
    .timeline-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .confidence-pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 9px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .confidence-pill[data-tone="danger"] {
      color: #FFD0CC;
      border-color: rgba(255,69,58,0.24);
      background: rgba(255,69,58,0.10);
    }
    .confidence-pill[data-tone="warning"] {
      color: #FFE1B2;
      border-color: rgba(255,159,10,0.24);
      background: rgba(255,159,10,0.10);
    }
    .confidence-pill[data-tone="success"] {
      color: #CFF6D5;
      border-color: rgba(50,215,75,0.24);
      background: rgba(50,215,75,0.10);
    }
    .timeline-item {
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }
    .timeline-dot {
      width: 10px;
      height: 10px;
      margin-top: 6px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 0 6px rgba(0,229,255,0.10);
    }
    .empty-state {
      padding: 14px;
      border-radius: 16px;
      border: 1px dashed var(--border);
      color: var(--text-secondary);
      background: rgba(255,255,255,0.02);
    }
    .pulse-dot {
      display: inline-flex;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--danger);
      box-shadow: 0 0 0 0 rgba(255,69,58,0.38);
      animation: recordingPulse 1.8s ease-in-out infinite;
    }

    @media (min-width: 760px) {
      .telemetry-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    }
    @media (min-width: 900px) {
      .hero-grid { grid-template-columns: minmax(0, 1.7fr) minmax(290px, 0.95fr); }
      .inspection-grid { grid-template-columns: minmax(0, 1.45fr) minmax(300px, 0.95fr); }
      .support-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .status-strip,
      .action-grid,
      .tool-grid {
        grid-template-columns: 1fr;
      }
      .section-head {
        flex-direction: column;
      }
      .action-banner {
        align-items: flex-start;
      }
    }
    @media (max-width: 520px) {
      .hero-card,
      .card { padding: 16px; }
      .hero-panel,
      .metric-card,
      .info-panel,
      .ai-panel { padding: 15px; }
      .tile-icon { width: 46px; height: 46px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        animation: none !important;
        transition: none !important;
        scroll-behavior: auto !important;
      }
    }

    @keyframes panelIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulseGlow {
      0%, 100% { box-shadow: 0 18px 34px rgba(0,0,0,0.28), 0 0 0 1px rgba(255,255,255,0.03), 0 0 0 rgba(0,229,255,0.0); }
      50% { box-shadow: 0 18px 34px rgba(0,0,0,0.32), 0 0 0 1px rgba(255,255,255,0.03), 0 0 22px rgba(0,229,255,0.12); }
    }
    @keyframes recordingPulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(255,69,58,0.34); }
      50% { box-shadow: 0 0 0 8px rgba(255,69,58,0); }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="card hero-card">
      <div class="hero-grid">
        <div class="hero-main">
          <div>
            <div class="section-kicker">Vehicle Command Center</div>
            <h1 class="hero-title" id="headerVehicleName">2006 Toyota Sienna FWD V6 3.3L</h1>
            <p class="hero-copy" id="headerVehicleDetail">Safe read-only diagnostics with a live command surface for Bluetooth status, OBD streaming, capture, and AI guidance.</p>
          </div>
          <div class="status-strip" aria-live="polite">
            <div class="status-indicator" id="statusBluetooth" data-tone="warning">
              <span class="status-dot" aria-hidden="true"></span>
              <span>
                <span class="status-label">Bluetooth</span>
                <span class="status-value" id="statusBluetoothValue">Not Connected</span>
              </span>
            </div>
            <div class="status-indicator" id="statusStreaming" data-tone="neutral">
              <span class="status-dot" aria-hidden="true"></span>
              <span>
                <span class="status-label">OBD Data</span>
                <span class="status-value" id="statusStreamingValue">Idle</span>
              </span>
            </div>
            <div class="status-indicator" id="statusAiAssist" data-tone="neutral">
              <span class="status-dot" aria-hidden="true"></span>
              <span>
                <span class="status-label">AI Assist</span>
                <span class="status-value" id="statusAiAssistValue">Offline</span>
              </span>
            </div>
          </div>
        </div>
        <aside class="hero-panel">
          <div>
            <label class="field-label" for="vehicleSelect">Vehicle Profile</label>
            <select id="vehicleSelect" aria-label="Select current vehicle profile"></select>
            <div class="panel-note tiny" id="vehicleProfileNote">Select the active vehicle profile for safe read-only diagnostics.</div>
          </div>
          <div class="hero-badges" aria-live="polite">
            <span class="badge" id="sessionBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="sessionBadgeText">Vehicle Check Idle</span></span>
            <span class="badge" id="captureBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="captureBadgeText">Capture Idle</span></span>
            <span class="badge" id="modeBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="modeBadgeText">Standby</span></span>
            <span class="badge" id="userBadge" data-tone="accent"><span class="badge-dot" aria-hidden="true"></span><span id="userBadgeText">Signed in: Demo Tester</span></span>
          </div>
        </aside>
      </div>
    </section>

    <section class="telemetry-grid" aria-label="Mini live telemetry strip">
      <article class="metric-card" id="metricCardRpm" data-live="false">
        <div class="metric-label">RPM</div>
        <div class="metric-value" id="metricRpmValue">--</div>
        <div class="metric-meta" id="metricRpmMeta">Awaiting live data</div>
      </article>
      <article class="metric-card" id="metricCardVoltage" data-live="false">
        <div class="metric-label">Battery Voltage</div>
        <div class="metric-value" id="metricVoltageValue">--</div>
        <div class="metric-meta" id="metricVoltageMeta">Awaiting live data</div>
      </article>
      <article class="metric-card" id="metricCardCoolant" data-live="false">
        <div class="metric-label">Coolant Temp</div>
        <div class="metric-value" id="metricCoolantValue">--</div>
        <div class="metric-meta" id="metricCoolantMeta">Awaiting live data</div>
      </article>
      <article class="metric-card" id="metricCardThrottle" data-live="false">
        <div class="metric-label">Throttle Position</div>
        <div class="metric-value" id="metricThrottleValue">--</div>
        <div class="metric-meta" id="metricThrottleMeta">Awaiting live data</div>
      </article>
    </section>

    <section class="card">
      <div class="section-head">
        <div>
          <div class="section-kicker">Featured Module</div>
          <h2 class="section-title">3D Vehicle Inspection</h2>
          <div class="section-copy">Tap a component to inspect and explain. The viewer remains advisory and read-only at all times.</div>
        </div>
        <span class="badge" id="inspectionBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="inspectionBadgeText">No highlighted component</span></span>
      </div>
      <div class="inspection-grid">
        <div class="vehicle-stage">
          <div id="vehicleCanvas" class="vehicle-canvas" aria-label="3D vehicle model viewer"></div>
          <img id="vehicleImageAsset" class="vehicle-fallback" alt="Fallback vehicle image" />
        </div>
        <div class="inspection-sidebar">
          <div class="info-panel">
            <div class="field-label">Highlighted Component</div>
            <div class="component-pill" id="componentStatusPill" data-tone="neutral">
              <span class="status-dot" aria-hidden="true"></span>
              <span id="highlightedComponentName">No highlighted component</span>
            </div>
            <button id="componentExplainBtn" class="panel-button panel-button--ghost" data-tone="accent" disabled>
              <span class="tile-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 18h.01"></path>
                  <path d="M9.09 9a3 3 0 1 1 5.82 1c0 2-3 2-3 4"></path>
                </svg>
              </span>
              <span class="tile-copy">
                <span class="tile-title">Explain Component</span>
                <span class="tile-meta">Tap a component to inspect and explain</span>
              </span>
              <span class="tile-state" id="componentExplainState">Select Part</span>
            </button>
          </div>
          <div class="info-panel">
            <div class="field-label">Inspection Source</div>
            <div class="info-title" id="vehicleImageLabel">Vehicle placeholder</div>
            <div class="tiny" id="vehicleImageSource">Loading vehicle source...</div>
          </div>
          <div class="info-panel">
            <div class="field-label">Safety Envelope</div>
            <div class="tiny">No control commands, no actuations, no code clearing, and no unsafe programming paths. Visual inspection is strictly read-only.</div>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="section-head">
        <div>
          <div class="section-kicker">Primary Command Center</div>
          <h2 class="section-title">Diagnostic Actions</h2>
          <div class="section-copy">Connect the adapter, launch a vehicle check, and control capture without breaking the current diagnostic flow.</div>
        </div>
        <span class="badge" id="diagnosticStateBadge" data-tone="warning"><span class="badge-dot" aria-hidden="true"></span><span id="diagnosticStateText">Awaiting connection</span></span>
      </div>

      <button id="connectVehicleBtn" class="action-banner" data-tone="accent" data-active="false" data-pulse="false">
        <span class="tile-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 7V5a3 3 0 0 1 6 0v2"></path>
            <path d="M6 11h12"></path>
            <path d="M8 11v4a4 4 0 0 0 8 0v-4"></path>
          </svg>
        </span>
        <span class="tile-copy">
          <span class="tile-title">Connect Vehicle</span>
          <span class="tile-meta" id="connectVehicleMeta">Pair with OBDLink MX+ and arm live read-only telemetry.</span>
        </span>
        <span class="tile-state" id="connectVehicleState">Standby</span>
      </button>

      <div class="action-grid" style="margin-top:12px;">
        <button id="startSessionBtn" class="action-tile" data-tone="accent" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 12h4l2-5 4 10 2-5h4"></path>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Start Vehicle Check</span>
              <span class="tile-meta">Run system-wide diagnostic scan</span>
            </span>
          </span>
          <span class="tile-state" id="startSessionState">Ready</span>
        </button>

        <button id="stopSessionBtn" class="action-tile" data-tone="danger" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <rect x="7" y="7" width="10" height="10" rx="1.8"></rect>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">End Vehicle Check</span>
              <span class="tile-meta">Close the active diagnostic session</span>
            </span>
          </span>
          <span class="tile-state" id="stopSessionState">Idle</span>
        </button>

        <button id="startCaptureBtn" class="action-tile" data-tone="success" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="7"></circle>
                <path d="M12 3v3"></path>
                <path d="M12 18v3"></path>
                <path d="M3 12h3"></path>
                <path d="M18 12h3"></path>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Start Capture</span>
              <span class="tile-meta">Record live data stream</span>
            </span>
          </span>
          <span class="tile-state" id="startCaptureState">Ready</span>
        </button>

        <button id="stopCaptureBtn" class="action-tile" data-tone="danger" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="8"></circle>
                <rect x="9" y="9" width="6" height="6" rx="1.2" fill="currentColor" stroke="none"></rect>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Stop Capture</span>
              <span class="tile-meta">End active recording safely</span>
            </span>
          </span>
          <span class="tile-state" id="stopCaptureState">Idle</span>
        </button>
      </div>
    </section>

    <section class="card">
      <div class="section-head">
        <div>
          <div class="section-kicker">Secondary Tools</div>
          <h2 class="section-title">Analysis and Workflow Utilities</h2>
          <div class="section-copy">Fast access to event tagging, saved outputs, live gauges, and the AI diagnostic assistant.</div>
        </div>
      </div>

      <div class="tool-grid">
        <button id="tagEventBtn" class="tool-tile" data-tone="warning" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 13l-7 7-9-9V4h7z"></path>
                <circle cx="8.5" cy="8.5" r="1"></circle>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Tag Event</span>
              <span class="tile-meta">Bookmark a notable moment</span>
            </span>
          </span>
          <span class="tile-state" id="tagEventState">Session Needed</span>
        </button>

        <button id="reportsBtn" class="tool-tile" data-tone="neutral" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M8 6h8"></path>
                <path d="M8 10h8"></path>
                <path d="M8 14h5"></path>
                <path d="M6 3h12a2 2 0 0 1 2 2v14l-4-2-4 2-4-2-4 2V5a2 2 0 0 1 2-2z"></path>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Reports</span>
              <span class="tile-meta">View saved diagnostic summaries</span>
            </span>
          </span>
          <span class="tile-state" id="reportsState">Available</span>
        </button>

        <button id="liveGaugesBtn" class="tool-tile" data-tone="accent" data-active="false" data-pulse="false">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 16a8 8 0 1 1 16 0"></path>
                <path d="M12 12l4-4"></path>
                <path d="M12 16h.01"></path>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Live Gauges</span>
              <span class="tile-meta">Monitor sensor data in real time</span>
            </span>
          </span>
          <span class="tile-state" id="liveGaugesState">Standby</span>
        </button>

        <button id="askAiBtn" class="tool-tile" data-tone="accent" data-active="true" data-pulse="false" data-feature="signature">
          <span class="tile-lead">
            <span class="tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 3l7 4v5c0 5-3.4 8.6-7 9-3.6-.4-7-4-7-9V7z"></path>
                <path d="M9.5 11.5a2.5 2.5 0 0 1 5 0c0 1.6-2.5 1.8-2.5 3.5"></path>
                <path d="M12 18h.01"></path>
              </svg>
            </span>
            <span class="tile-copy">
              <span class="tile-title">Ask AI Mechanic</span>
              <span class="tile-meta">Voice and diagnostic assistant</span>
            </span>
          </span>
          <span class="tile-state" id="askAiState">Standby</span>
        </button>
      </div>

      <div class="ai-panel">
        <div class="section-head" style="margin-bottom:12px;">
          <div>
            <div class="section-kicker">AI Mechanic</div>
            <h2 class="section-title">Diagnostic Guidance Layer</h2>
            <div class="section-copy">Ask about codes, sensors, symptoms, and repairs. Guidance stays read-only and grounded in the current vehicle context when a vehicle check is active.</div>
          </div>
          <span class="badge" id="aiPanelBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="aiPanelBadgeText">Awaiting vehicle check</span></span>
        </div>
        <div class="ai-quick">
          <button class="quick-chip" onclick="openAiWithPrompt('What does this code mean?')">What does this code mean?</button>
          <button class="quick-chip" onclick="openAiWithPrompt('Is it safe to drive?')">Is it safe to drive?</button>
          <button class="quick-chip" onclick="openAiWithPrompt('What should I test next?')">What should I test next?</button>
          <button class="quick-chip" onclick="openAiWithPrompt('Explain this in simple language')">Explain this in simple language</button>
          <button class="quick-chip" onclick="openAiWithPrompt('What changed in live data?')">What changed in live data?</button>
          <button class="quick-chip" onclick="openAiWithPrompt('What should I watch right now?')">What should I watch right now?</button>
        </div>
      </div>
    </section>

    <section class="support-grid">
      <div class="card info-card" id="bluetoothCard">
        <div>
          <div class="section-kicker">Connection Telemetry</div>
          <h2 class="section-title">Bluetooth and Bridge Status</h2>
        </div>
        <span class="badge" id="btStatusBadge" data-tone="neutral"><span class="badge-dot" aria-hidden="true"></span><span id="btStatusText">Disconnected</span></span>
        <div class="tiny" id="btError">No Bluetooth errors</div>
        <div id="btDebug" class="detail-list"></div>
      </div>

      <div class="card info-card">
        <div>
          <div class="section-kicker">Proactive Monitoring</div>
          <h2 class="section-title">AI Alerts</h2>
        </div>
        <div class="tiny" id="aiAlertSummary">No active alerts.</div>
        <div id="aiAlertList" class="alert-list"></div>
      </div>

      <div class="card info-card">
        <div>
          <div class="section-kicker">Traceability</div>
          <h2 class="section-title">Diagnostic Timeline</h2>
        </div>
        <div id="timelineList" class="timeline-list"></div>
      </div>
    </section>
  </div>
<script type="module">
import * as THREE from 'https://unpkg.com/three@0.162.0/build/three.module.js';

let state = null;
let selectedVehicleId = 'toyota_sienna_2006';
let phoneBridge = { status: 'disconnected', source_mode: 'PHONE-LIVE' };
let vehicleRenderer = null;
let selectedMesh = null;
let livePollingHandle = null;
let livePollingInFlight = false;
let renderedVehicleKey = '';
let lastVehicleImageSelection = null;
let lastVehicleImageRequestId = 0;
const SENSOR_TO_PID = { rpm:'010C', coolant_temp:'0105', control_module_voltage:'0142', vehicle_speed:'010D' };
const LIVE_SENSOR_ORDER = ['rpm', 'coolant_temp', 'control_module_voltage', 'vehicle_speed'];
const LIVE_TELEMETRY_KEYS = ['rpm', 'coolant_temp', 'control_module_voltage', 'vehicle_speed', 'throttle_position'];
const TOYOTA_PREMIUM_NAME = '2006 Toyota Sienna FWD V6 3.3L';
const IOS_NATIVE_APP_MESSAGE = 'Bluetooth live connection requires the native app on iPhone. Web Bluetooth is not available in this environment.';
const MOBILE_NATIVE_APP_MESSAGE = 'Bluetooth live connection on phones must use the native Bluetooth bridge. Direct browser Bluetooth is disabled on mobile runtimes.';
const GENERIC_UNSUPPORTED_MESSAGE = 'Bluetooth live connection is not available in this environment.';
const REQUIRED_NATIVE_BRIDGE_METHODS = ['connectToAdapter', 'disconnectAdapter', 'getConnectionState', 'requestBluetoothPermission', 'readPid', 'readVin', 'startPolling', 'stopPolling'];
const SAFE_BROWSER_ADAPTER_COMMANDS = ['ATZ', 'ATE0', 'ATL0', 'ATS0', 'ATH0', 'ATSP3'];
const OBDLINK_BLE_UART_SERVICE_UUID = '0000fff0-0000-1000-8000-00805f9b34fb';
const OBDLINK_BLE_UART_NOTIFY_UUID = '0000fff1-0000-1000-8000-00805f9b34fb';
const OBDLINK_BLE_UART_WRITE_UUID = '0000fff2-0000-1000-8000-00805f9b34fb';
const PID_RESPONSE_PREFIXES = {
  '0105': '41 05',
  '010C': '41 0C',
  '010D': '41 0D',
  '010F': '41 0F',
  '0111': '41 11',
  '012F': '41 2F',
  '0142': '41 42',
  '0902': '49 02 01',
};
let transportContext = {
  selected_transport: 'unsupported',
  runtime_platform: 'unknown',
  native_bridge_detected: false,
  navigator_bluetooth_available: false,
  status_label: 'Unsupported environment',
  message: GENERIC_UNSUPPORTED_MESSAGE,
  last_connection_error: null,
  first_pid_read_logged: false,
};
function emptyLocalConnectionDiagnostics(permissionStatus = 'unknown', nativeBridgeAvailable = false){
  return {
    native_bridge_available: nativeBridgeAvailable,
    bluetooth_permission_status: permissionStatus,
    adapter_discovery_started: false,
    adapter_found: false,
    adapter_connection_attempted: false,
    adapter_connected: false,
    first_pid_command_sent: false,
    first_pid_response_received: false,
    backend_ingest_success: false,
    mode_switched_to_phone_live: false,
  };
}
let localConnectionDiagnostics = emptyLocalConnectionDiagnostics();
let lastLoggedTransportSelection = '';
function getMobileBluetoothService(){
  const service = window.MobileBluetoothService || null;
  if(!service){ return null; }
  return REQUIRED_NATIVE_BRIDGE_METHODS.every((method) => typeof service[method] === 'function') ? service : null;
}
function webBluetoothAvailable(){
  return Boolean(navigator.bluetooth && typeof navigator.bluetooth.requestDevice === 'function');
}
function inferRuntimePlatform(payload = null){
  const declaredPlatform = String(payload?.platform || '').trim().toLowerCase();
  if(['ios', 'ipad', 'android', 'browser'].includes(declaredPlatform)){
    return declaredPlatform;
  }
  const userAgent = navigator.userAgent || '';
  const touchMac = /Macintosh/.test(userAgent) && (navigator.maxTouchPoints || 0) > 1;
  if(/iPad/.test(userAgent) || touchMac){ return 'ipad'; }
  if(/iPhone|iPod/.test(userAgent)){ return 'ios'; }
  if(/Android/.test(userAgent)){ return 'android'; }
  return webBluetoothAvailable() ? 'browser' : 'unknown';
}
function isMobileRuntimePlatform(platform = inferRuntimePlatform()){
  return ['ios', 'ipad', 'android'].includes(platform);
}
function nativeBridgeRequiredMessage(platform){
  if(platform === 'ios' || platform === 'ipad'){
    return IOS_NATIVE_APP_MESSAGE;
  }
  if(platform === 'android'){
    return MOBILE_NATIVE_APP_MESSAGE;
  }
  return GENERIC_UNSUPPORTED_MESSAGE;
}
function isAppleTouchWebEnvironment(){
  return typeof window.webkit === 'object' && (navigator.maxTouchPoints || 0) > 1;
}
function transportLog(event, extra = {}){
  console.debug('[transport]', {
    event,
    selected_transport: transportContext.selected_transport,
    runtime_platform: transportContext.runtime_platform,
    native_bridge_detected: transportContext.native_bridge_detected,
    navigator_bluetooth_available: transportContext.navigator_bluetooth_available,
    last_connection_error: transportContext.last_connection_error,
    ...extra,
  });
}
function setLastConnectionError(error){
  const nextError = error || null;
  if(transportContext.last_connection_error === nextError){ return; }
  transportContext = { ...transportContext, last_connection_error: nextError };
  if(nextError){ transportLog('last connection error', { error: nextError }); }
}
function normalizeNativeDisconnectPayload(payload = {}){
  const fallbackReason = payload?.fallback_reason || payload?.fallbackReason || payload?.last_error || payload?.lastError || 'adapter-disconnected';
  return {
    platform: payload?.platform || phoneBridge.platform || inferRuntimePlatform(),
    adapter_name: payload?.adapter_name || payload?.adapterName || phoneBridge.adapter_name || 'OBDLink MX+',
    status: 'disconnected',
    permission_state: payload?.permission_state || payload?.permissionState || localConnectionDiagnostics.bluetooth_permission_status || 'unknown',
    source_mode: payload?.source_mode || payload?.sourceMode || phoneBridge.source_mode || 'PHONE-LIVE',
    supports_native_bluetooth: payload?.supports_native_bluetooth ?? payload?.supportsNativeBluetooth ?? true,
    fallback_reason: fallbackReason,
    last_error: fallbackReason,
  };
}
function normalizePermissionStatus(value){
  if(value === true){ return 'granted'; }
  if(value === false){ return 'denied'; }
  const normalized = String(value || 'unknown').trim().toLowerCase();
  if(!normalized){ return 'unknown'; }
  if(['authorized', 'allowed', 'granted'].includes(normalized)){ return 'granted'; }
  if(['denied', 'blocked', 'restricted'].includes(normalized)){ return normalized; }
  if(['prompt', 'prompt-with-rationale', 'not-determined', 'undetermined'].includes(normalized)){ return 'prompt'; }
  return normalized;
}
function setLocalConnectionDiagnostics(updates = {}){
  localConnectionDiagnostics = { ...localConnectionDiagnostics, ...updates };
}
function resetLocalConnectionDiagnostics({ permissionStatus, nativeBridgeAvailable } = {}){
  const nextPermission = permissionStatus ?? localConnectionDiagnostics.bluetooth_permission_status ?? 'unknown';
  const nextNativeBridge = nativeBridgeAvailable ?? localConnectionDiagnostics.native_bridge_available ?? false;
  localConnectionDiagnostics = emptyLocalConnectionDiagnostics(nextPermission, nextNativeBridge);
}
function resolvedConnectionDiagnostics(){
  const backend = phoneBridge.connection_diagnostics || {};
  const backendPermission = normalizePermissionStatus(backend.bluetooth_permission_status || phoneBridge.permission_state);
  const localPermission = normalizePermissionStatus(localConnectionDiagnostics.bluetooth_permission_status);
  return {
    native_bridge_available: Boolean(localConnectionDiagnostics.native_bridge_available || backend.native_bridge_available || transportContext.native_bridge_detected),
    bluetooth_permission_status: localPermission !== 'unknown' ? localPermission : backendPermission,
    adapter_discovery_started: Boolean(localConnectionDiagnostics.adapter_discovery_started || backend.adapter_discovery_started),
    adapter_found: Boolean(localConnectionDiagnostics.adapter_found || backend.adapter_found),
    adapter_connection_attempted: Boolean(localConnectionDiagnostics.adapter_connection_attempted || backend.adapter_connection_attempted),
    adapter_connected: Boolean(localConnectionDiagnostics.adapter_connected || backend.adapter_connected || transportServiceReady()),
    first_pid_command_sent: Boolean(localConnectionDiagnostics.first_pid_command_sent || backend.first_pid_command_sent),
    first_pid_response_received: Boolean(localConnectionDiagnostics.first_pid_response_received || backend.first_pid_response_received || phoneBridge.first_live_read_received),
    backend_ingest_success: Boolean(localConnectionDiagnostics.backend_ingest_success || backend.backend_ingest_success),
    mode_switched_to_phone_live: Boolean(localConnectionDiagnostics.mode_switched_to_phone_live || backend.mode_switched_to_phone_live || state?.current_mode === 'PHONE-LIVE'),
  };
}
function formatDiagnosticValue(value){
  if(typeof value === 'boolean'){ return value ? 'true' : 'false'; }
  return String(value ?? 'unknown');
}
function detectTransport(){
  const nativeService = getMobileBluetoothService();
  const nativeBridgeDetected = Boolean(nativeService);
  const runtimePlatform = inferRuntimePlatform();
  const mobileRuntime = isMobileRuntimePlatform(runtimePlatform);
  const bluetoothAvailable = webBluetoothAvailable();
  const nativeBridgeRequired = !nativeBridgeDetected && mobileRuntime;
  let nextContext;
  if(nativeBridgeDetected){
    nextContext = {
      selected_transport: 'native-phone-bridge',
      runtime_platform: runtimePlatform,
      native_bridge_detected: true,
      navigator_bluetooth_available: bluetoothAvailable,
      status_label: 'Native bridge ready',
      message: 'Native phone bridge detected and callable.',
    };
  } else if(bluetoothAvailable && !mobileRuntime){
    nextContext = {
      selected_transport: 'web-bluetooth',
      runtime_platform: runtimePlatform,
      native_bridge_detected: false,
      navigator_bluetooth_available: true,
      status_label: 'Web Bluetooth available',
      message: 'Desktop Chrome or Edge can use Web Bluetooth for live reads.',
    };
  } else if(nativeBridgeRequired || isAppleTouchWebEnvironment()){
    nextContext = {
      selected_transport: 'unsupported',
      runtime_platform: runtimePlatform,
      native_bridge_detected: false,
      navigator_bluetooth_available: bluetoothAvailable,
      status_label: bluetoothAvailable ? 'Native bridge required' : 'Native bridge missing',
      message: nativeBridgeRequiredMessage(runtimePlatform),
    };
  } else {
    nextContext = {
      selected_transport: 'unsupported',
      runtime_platform: runtimePlatform,
      native_bridge_detected: false,
      navigator_bluetooth_available: false,
      status_label: 'Unsupported environment',
      message: GENERIC_UNSUPPORTED_MESSAGE,
    };
  }
  const nextSelection = JSON.stringify(nextContext);
  transportContext = { ...transportContext, ...nextContext };
  setLocalConnectionDiagnostics({ native_bridge_available: nativeBridgeDetected });
  if(lastLoggedTransportSelection !== nextSelection){
    lastLoggedTransportSelection = nextSelection;
    transportLog('selected transport', {
      status_label: transportContext.status_label,
      message: transportContext.message,
    });
  }
  return transportContext;
}
function normalizeConnectPayload(payload, detection = detectTransport()){
  return {
    platform: payload?.platform || detection.runtime_platform || (detection.selected_transport === 'web-bluetooth' ? 'browser' : 'unknown'),
    adapter_name: payload?.adapter_name || payload?.adapterName || 'OBDLink MX+',
    status: payload?.status || 'failed',
    permission_state: payload?.permission_state || payload?.permissionState || null,
    source_mode: payload?.source_mode || payload?.sourceMode || (detection.selected_transport === 'web-bluetooth' ? 'BROWSER-DEV' : 'PHONE-LIVE'),
    supports_native_bluetooth: payload?.supports_native_bluetooth ?? payload?.supportsNativeBluetooth ?? detection.native_bridge_detected,
    fallback_reason: payload?.fallback_reason || payload?.fallbackReason || null,
  };
}
function getBrowserBluetoothTransport(){
  if(window.__siennaBrowserBluetoothTransport){ return window.__siennaBrowserBluetoothTransport; }

  class BrowserBluetoothTransport {
    constructor(){
      this.device = null;
      this.server = null;
      this.notifyCharacteristic = null;
      this.writeCharacteristic = null;
      this.pendingResponse = null;
      this.responseBuffer = '';
      this.commandQueue = Promise.resolve();
      this.decoder = new TextDecoder();
      this.encoder = new TextEncoder();
      this.handleNotification = this.handleNotification.bind(this);
      this.handleDisconnect = this.handleDisconnect.bind(this);
    }

    isConnected(){
      return Boolean(this.device?.gatt?.connected && this.writeCharacteristic && this.notifyCharacteristic);
    }

    async connectToAdapter(){
      if(!webBluetoothAvailable()){
        throw new Error('web-bluetooth-unavailable');
      }
      if(this.isConnected()){
        return this.connectionPayload('connected');
      }
      const device = await navigator.bluetooth.requestDevice({
        filters: [{ namePrefix: 'OBDLink' }],
        optionalServices: [OBDLINK_BLE_UART_SERVICE_UUID],
      });
      await this.attachDevice(device);
      await this.initializeAdapter();
      return this.connectionPayload('connected');
    }

    async reconnectIfNeeded(){
      if(this.isConnected()){
        return this.connectionPayload('connected');
      }
      if(!navigator.bluetooth || typeof navigator.bluetooth.getDevices !== 'function'){
        throw new Error('web-bluetooth-reconnect-unavailable');
      }
      const devices = await navigator.bluetooth.getDevices();
      const device = devices.find((candidate) => candidate.name && candidate.name.startsWith('OBDLink'));
      if(!device){
        throw new Error('web-bluetooth-device-not-granted');
      }
      await this.attachDevice(device);
      await this.initializeAdapter();
      return this.connectionPayload('connected');
    }

    async attachDevice(device){
      this.reset('replace-device');
      this.device = device;
      this.device.addEventListener('gattserverdisconnected', this.handleDisconnect);
      this.server = await this.device.gatt.connect();
      const service = await this.server.getPrimaryService(OBDLINK_BLE_UART_SERVICE_UUID);
      this.notifyCharacteristic = await service.getCharacteristic(OBDLINK_BLE_UART_NOTIFY_UUID);
      this.writeCharacteristic = await service.getCharacteristic(OBDLINK_BLE_UART_WRITE_UUID);
      await this.notifyCharacteristic.startNotifications();
      this.notifyCharacteristic.addEventListener('characteristicvaluechanged', this.handleNotification);
    }

    async initializeAdapter(){
      for(const command of SAFE_BROWSER_ADAPTER_COMMANDS){
        await this.sendCommand(command, command === 'ATZ' ? 5000 : 2500);
      }
    }

    async disconnectAdapter(){
      this.reset('manual-disconnect');
      return this.connectionPayload('failed', 'web-bluetooth-disconnected');
    }

    async readPid(pid){
      const command = String(pid || '').toUpperCase();
      const rawResponse = await this.sendCommand(command, 3500);
      const cleaned = cleanAdapterResponse(rawResponse);
      return {
        command,
        pid_key: Object.keys(SENSOR_TO_PID).find((sensor) => SENSOR_TO_PID[sensor] === command) || null,
        source_mode: 'BROWSER-DEV',
        source_hint: 'iso9141_2',
        raw_response: cleaned,
        value: parsePidValue(command, cleaned),
        unit: pidUnit(command),
        backend_status: 'accepted',
        ts: new Date().toISOString(),
      };
    }

    async readVin(){
      return this.readPid('0902');
    }

    connectionPayload(status, fallbackReason = null){
      return {
        platform: 'browser',
        adapter_name: this.device?.name || 'OBDLink MX+',
        status,
        permission_state: this.device ? 'granted' : 'prompt',
        source_mode: 'BROWSER-DEV',
        supports_native_bluetooth: false,
        fallback_reason: fallbackReason,
      };
    }

    async sendCommand(command, timeoutMs){
      const queued = this.commandQueue.then(() => this.sendCommandOnce(command, timeoutMs));
      this.commandQueue = queued.catch(() => undefined);
      return queued;
    }

    async sendCommandOnce(command, timeoutMs){
      if(!this.writeCharacteristic){
        throw new Error('web-bluetooth-not-connected');
      }
      this.responseBuffer = '';
      const payload = this.encoder.encode(`${command}\\r`);
      const responsePromise = new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          if(this.pendingResponse){
            this.pendingResponse = null;
            reject(new Error(`timeout:${command}`));
          }
        }, timeoutMs);
        this.pendingResponse = { resolve, reject, timer };
      });
      const writeWithoutResponse = this.writeCharacteristic.writeValueWithoutResponse;
      if(typeof writeWithoutResponse === 'function'){
        await writeWithoutResponse.call(this.writeCharacteristic, payload);
      } else {
        await this.writeCharacteristic.writeValue(payload);
      }
      return responsePromise;
    }

    handleNotification(event){
      this.responseBuffer += this.decoder.decode(event.target.value);
      if(!this.pendingResponse || !this.responseBuffer.includes('>')){ return; }
      const { resolve, timer } = this.pendingResponse;
      window.clearTimeout(timer);
      this.pendingResponse = null;
      const rawResponse = this.responseBuffer;
      this.responseBuffer = '';
      resolve(rawResponse);
    }

    async handleDisconnect(){
      const disconnectReason = 'web-bluetooth-disconnected';
      setLastConnectionError(disconnectReason);
      resetLocalConnectionDiagnostics({
        permissionStatus: localConnectionDiagnostics.bluetooth_permission_status,
        nativeBridgeAvailable: transportContext.native_bridge_detected,
      });
      phoneBridge = { ...phoneBridge, status: 'disconnected', last_error: disconnectReason };
      transportLog('connect failure', { reason: disconnectReason });
      this.reset(disconnectReason);
      try {
        await fetch('/phone/bridge/disconnect', { method: 'POST' });
      } catch (error) {
        console.debug('[transport]', { event: 'disconnect sync failed', error: error?.message || String(error) });
      }
      await fetchState();
    }

    reset(reason){
      if(this.pendingResponse){
        window.clearTimeout(this.pendingResponse.timer);
        this.pendingResponse.reject(new Error(reason));
        this.pendingResponse = null;
      }
      if(this.notifyCharacteristic){
        this.notifyCharacteristic.removeEventListener('characteristicvaluechanged', this.handleNotification);
      }
      if(this.device){
        this.device.removeEventListener('gattserverdisconnected', this.handleDisconnect);
      }
      if(this.device?.gatt?.connected){
        this.device.gatt.disconnect();
      }
      this.server = null;
      this.notifyCharacteristic = null;
      this.writeCharacteristic = null;
      this.responseBuffer = '';
    }
  }

  window.__siennaBrowserBluetoothTransport = new BrowserBluetoothTransport();
  return window.__siennaBrowserBluetoothTransport;
}
function cleanAdapterResponse(rawResponse){
  return String(rawResponse || '')
    .replace(/\\u0000/g, '')
    .replace(/[\\r\\n>]+/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim();
}
function extractResponseBytes(command, rawResponse){
  const prefix = PID_RESPONSE_PREFIXES[command];
  if(!prefix){ return []; }
  const tokens = cleanAdapterResponse(rawResponse).toUpperCase().split(' ').filter((token) => /^[0-9A-F]{2}$/.test(token));
  const prefixTokens = prefix.split(' ');
  const startIndex = tokens.findIndex((token, index) => prefixTokens.every((part, offset) => tokens[index + offset] === part));
  return startIndex === -1 ? [] : tokens.slice(startIndex + prefixTokens.length);
}
function parsePidValue(command, rawResponse){
  const bytes = extractResponseBytes(command, rawResponse).map((token) => Number.parseInt(token, 16));
  if(command === '010C' && bytes.length >= 2){ return ((bytes[0] * 256) + bytes[1]) / 4; }
  if(command === '0105' && bytes.length >= 1){ return bytes[0] - 40; }
  if(command === '010D' && bytes.length >= 1){ return bytes[0]; }
  if(command === '010F' && bytes.length >= 1){ return bytes[0] - 40; }
  if(command === '0111' && bytes.length >= 1){ return Number(((bytes[0] * 100) / 255).toFixed(1)); }
  if(command === '012F' && bytes.length >= 1){ return Number(((bytes[0] * 100) / 255).toFixed(1)); }
  if(command === '0142' && bytes.length >= 2){ return Number((((bytes[0] * 256) + bytes[1]) / 1000).toFixed(3)); }
  return null;
}
function pidUnit(command){
  if(command === '010C'){ return 'rpm'; }
  if(command === '0105' || command === '010F'){ return '°C'; }
  if(command === '010D'){ return 'km/h'; }
  if(command === '0111' || command === '012F'){ return '%'; }
  if(command === '0142'){ return 'V'; }
  return null;
}
function getTransportService(detection = detectTransport()){
  if(detection.selected_transport === 'native-phone-bridge'){
    return getMobileBluetoothService();
  }
  if(detection.selected_transport === 'web-bluetooth'){
    return getBrowserBluetoothTransport();
  }
  return null;
}
async function publishBridgeConnection(payload){
  await fetch('/phone/bridge/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}
async function publishBridgeDisconnect(){
  await fetch('/phone/bridge/disconnect', { method: 'POST' });
}
async function getNativeBridgeState(service){
  if(!service || typeof service.getConnectionState !== 'function'){ return null; }
  try {
    return await service.getConnectionState();
  } catch (error) {
    console.debug('[transport]', { event: 'getConnectionState failed', error: error?.message || String(error) });
    return null;
  }
}
async function ensureBluetoothPermissionForConnection(service, detection){
  if(detection.selected_transport !== 'native-phone-bridge'){ return null; }
  let permissionStatus = normalizePermissionStatus(phoneBridge.permission_state);
  const bridgeState = await getNativeBridgeState(service);
  if(bridgeState){
    permissionStatus = normalizePermissionStatus(bridgeState.permission_state || bridgeState.permissionState || permissionStatus);
  }
  if(['unknown', 'prompt'].includes(permissionStatus) && typeof service.requestBluetoothPermission === 'function'){
    const permissionResult = await service.requestBluetoothPermission();
    permissionStatus = normalizePermissionStatus(permissionResult?.permission_state || permissionResult?.permissionState || permissionResult?.status || permissionStatus);
  }
  setLocalConnectionDiagnostics({ bluetooth_permission_status: permissionStatus });
  return permissionStatus;
}
async function resetRealConnectionSequence(service){
  stopLivePolling();
  resetLocalConnectionDiagnostics({
    permissionStatus: localConnectionDiagnostics.bluetooth_permission_status,
    nativeBridgeAvailable: transportContext.native_bridge_detected,
  });
  try {
    if(service && typeof service.disconnectAdapter === 'function'){
      await service.disconnectAdapter();
    }
  } catch (error) {
    console.debug('[transport]', { event: 'disconnect before reconnect failed', error: error?.message || String(error) });
  }
  try {
    await publishBridgeDisconnect();
  } catch (error) {
    console.debug('[transport]', { event: 'disconnect state sync failed', error: error?.message || String(error) });
  }
}
function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function formatLabel(value){
  return String(value ?? '')
    .replace(/_/g, ' ')
    .replace(/\\b\\w/g, (char) => char.toUpperCase());
}
function formatMode(value){
  return String(value || 'standby')
    .toLowerCase()
    .replace(/_/g, ' ')
    .replace(/\\b\\w/g, (char) => char.toUpperCase());
}
function shortId(value){
  if(!value){ return 'None'; }
  return `${String(value).slice(0, 8)}...`;
}
function getVehicleProfileById(vehicleId){
  return (state?.vehicles || []).find((vehicle) => vehicle.vehicle_id === vehicleId) || null;
}
function getSelectedVehicleProfile(){
  return getVehicleProfileById(selectedVehicleId);
}
function getVehicleDisplayName(){
  const activeVehicleId = state?.active_session?.vehicle_id || selectedVehicleId;
  if(activeVehicleId === 'toyota_sienna_2006'){ return TOYOTA_PREMIUM_NAME; }
  return getVehicleProfileById(activeVehicleId)?.label || state?.active_session?.vehicle || 'Vehicle not selected';
}
function getLatestRead(pidKey){
  const reads = state?.recent_reads || [];
  for(let index = reads.length - 1; index >= 0; index -= 1){
    if(reads[index].pid_key === pidKey){ return reads[index]; }
  }
  return null;
}
function getTelemetryAge(ts){
  if(!ts){ return null; }
  const ageMs = Date.now() - Date.parse(ts);
  if(!Number.isFinite(ageMs) || ageMs < 0){ return null; }
  return ageMs;
}
function isFreshRead(read){
  const ageMs = getTelemetryAge(read?.ts);
  return ageMs !== null && ageMs <= 15000;
}
function formatAge(ts){
  const ageMs = getTelemetryAge(ts);
  if(ageMs === null){ return 'Timestamp unavailable'; }
  if(ageMs < 2000){ return 'Updated just now'; }
  const seconds = Math.round(ageMs / 1000);
  if(seconds < 60){ return `Updated ${seconds}s ago`; }
  return `Updated ${Math.round(seconds / 60)}m ago`;
}
function formatSourceLabel(source){
  if(!source){ return 'Source pending'; }
  if(source === 'PHONE-LIVE'){ return 'Phone live'; }
  if(source === 'LOCAL-HARDWARE'){ return 'Local hardware'; }
  if(source === 'BROWSER-DEV'){ return 'Browser bridge'; }
  return formatMode(source);
}
function formatMetricValue(read, fallbackUnit, precision){
  if(!read || read.value === null || read.value === undefined || read.value === ''){ return { value: '--', meta: 'Awaiting live data', live: false }; }
  const numeric = Number(read.value);
  const hasNumeric = Number.isFinite(numeric);
  const value = hasNumeric ? numeric.toFixed(precision) : String(read.value);
  const unit = read.unit || fallbackUnit || '';
  return {
    value: unit ? `${value} ${unit}` : value,
    meta: `${formatSourceLabel(read.source_mode)} · ${formatAge(read.ts)}`,
    live: isFreshRead(read),
  };
}
function applyTone(id, tone){
  const element = document.getElementById(id);
  if(element){ element.dataset.tone = tone; }
}
function updateIndicator(rootId, valueId, tone, value){
  applyTone(rootId, tone);
  const target = document.getElementById(valueId);
  if(target){ target.textContent = value; }
}
function updateBadge(rootId, textId, tone, value, pulse=false){
  const badge = document.getElementById(rootId);
  if(badge){
    badge.dataset.tone = tone;
    badge.dataset.pulse = pulse ? 'true' : 'false';
  }
  const text = document.getElementById(textId);
  if(text){ text.textContent = value; }
}
function setButtonState(buttonId, tone, stateText, active=false, pulse=false, ariaPressed=false, disabled=false){
  const button = document.getElementById(buttonId);
  if(!button){ return; }
  button.dataset.tone = tone;
  button.dataset.active = active ? 'true' : 'false';
  button.dataset.pulse = pulse ? 'true' : 'false';
  button.setAttribute('aria-pressed', ariaPressed ? 'true' : 'false');
  button.disabled = disabled;
  const stateEl = button.querySelector('.tile-state');
  if(stateEl){ stateEl.textContent = stateText; }
}
function hasUsablePidRead(read){
  return Boolean(read && read.raw_response && !read.error);
}
function markFirstPidRead(read){
  if(!read || transportContext.first_pid_read_logged){ return; }
  transportContext = { ...transportContext, first_pid_read_logged: true };
  transportLog('first PID read', { pid: read.command, source_mode: read.source_mode });
}
function handleNativePollEvent(event){
  const nativeRead = event?.detail || null;
  if(!nativeRead || !hasUsablePidRead(nativeRead)){ return; }
  setLocalConnectionDiagnostics({
    first_pid_command_sent: true,
    first_pid_response_received: true,
  });
  markFirstPidRead(nativeRead);
}
async function handleNativeDisconnectEvent(event){
  const detail = normalizeNativeDisconnectPayload(event?.detail || {});
  const reason = detail.last_error || detail.fallback_reason || 'adapter-disconnected';
  setLastConnectionError(reason);
  setLocalConnectionDiagnostics({ adapter_connected: false });
  phoneBridge = {
    ...phoneBridge,
    ...detail,
    status: 'disconnected',
    last_error: reason,
    fallback_reason: reason,
  };
  stopLivePolling();
  renderDashboardState();
  renderBluetoothCard();
  try {
    await publishBridgeDisconnect();
  } catch (error) {
    console.debug('[transport]', { event: 'native disconnect sync failed', error: error?.message || String(error) });
  }
  await fetchState();
}
function transportServiceReady(){
  const detection = detectTransport();
  if((phoneBridge.status || 'disconnected') !== 'connected'){ return false; }
  if(detection.selected_transport === 'native-phone-bridge'){
    return Boolean(getMobileBluetoothService());
  }
  if(detection.selected_transport === 'web-bluetooth'){
    return getBrowserBluetoothTransport().isConnected();
  }
  return false;
}
async function readTransportPid(sensor){
  const detection = detectTransport();
  const service = getTransportService(detection);
  if(!service || typeof service.readPid !== 'function'){ return null; }
  try {
    setLocalConnectionDiagnostics({ first_pid_command_sent: true });
    renderBluetoothCard();
    const liveRead = await service.readPid(SENSOR_TO_PID[sensor]);
    if(!liveRead){ return null; }
    const rawResponse = liveRead.raw_response || liveRead.rawResponse || null;
    if(!rawResponse){ return null; }
    const normalizedRead = {
      command: SENSOR_TO_PID[sensor],
      pid_key: sensor,
      source_mode: liveRead.source_mode || liveRead.sourceMode || (detection.selected_transport === 'web-bluetooth' ? 'BROWSER-DEV' : 'PHONE-LIVE'),
      source_hint: liveRead.source_hint || liveRead.sourceHint || 'iso9141_2',
      raw_response: rawResponse,
      value: liveRead.value ?? null,
      unit: liveRead.unit ?? null,
      latency_ms: liveRead.latency_ms ?? liveRead.latencyMs ?? null,
      error: liveRead.error ?? null,
      backend_status: liveRead.backend_status || liveRead.backendStatus || undefined,
      ts: liveRead.ts || liveRead.timestamp || undefined,
    };
    if(hasUsablePidRead(normalizedRead)){
      setLocalConnectionDiagnostics({ first_pid_response_received: true });
      markFirstPidRead(normalizedRead);
    }
    return normalizedRead;
  } catch (error) {
    const reason = error?.message || 'transport-read-failed';
    setLastConnectionError(reason);
    phoneBridge = { ...phoneBridge, last_error: reason };
    renderBluetoothCard();
    return null;
  }
}
function webglAvailable(){ try { const c=document.createElement('canvas'); return !!window.WebGLRenderingContext && !!(c.getContext('webgl') || c.getContext('experimental-webgl')); } catch { return false; } }

function buildGenericVehicleViewer(){
  const host=document.getElementById('vehicleCanvas');
  if(!host || !webglAvailable()){ showFallbackImage('WebGL unavailable - static fallback image in use'); return; }
  const scene=new THREE.Scene(); scene.background=new THREE.Color(0x05080d);
  const camera=new THREE.PerspectiveCamera(52, host.clientWidth/host.clientHeight, 0.1, 100); camera.position.set(5.5,3.2,6.6);
  const renderer=new THREE.WebGLRenderer({antialias:false,alpha:false,powerPreference:'low-power'}); renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2)); renderer.setSize(host.clientWidth,host.clientHeight); host.innerHTML=''; host.appendChild(renderer.domElement);
  scene.add(new THREE.AmbientLight(0xffffff,0.76));
  const key=new THREE.DirectionalLight(0xffffff,0.88); key.position.set(4,7,6); scene.add(key);
  const fill=new THREE.PointLight(0x00e5ff,0.45,18); fill.position.set(-3,3,4); scene.add(fill);
  const root=new THREE.Group(); scene.add(root);
  const partMap={};
  function addPart(name,system,geo,color,pos,scale){ const mat=new THREE.MeshStandardMaterial({color,roughness:0.72,metalness:0.08,transparent:true,opacity:1}); const m=new THREE.Mesh(geo,mat); m.position.set(...pos); m.scale.set(...scale); m.userData={component:name,system,baseColor:color}; root.add(m); partMap[name]=m; return m; }
  addPart('engine_block','engine',new THREE.BoxGeometry(1.35,0.72,1.05),0x5b6774,[-0.85,0.35,0.2],[1,1,1]);
  addPart('thermostat','cooling_system',new THREE.SphereGeometry(0.16,12,10),0x1c9aae,[-0.4,0.75,0.35],[1,1,1]);
  addPart('radiator','cooling_system',new THREE.BoxGeometry(0.18,0.86,1.26),0x216578,[1.45,0.38,0],[1,1,1]);
  addPart('intake_manifold','intake',new THREE.BoxGeometry(1.2,0.28,0.72),0x426070,[-0.82,0.9,0.14],[1,1,1]);
  addPart('throttle_body','intake',new THREE.CylinderGeometry(0.12,0.12,0.36,10),0x3b8797,[0.18,0.84,0.58],[1,1,1]);
  addPart('exhaust_manifold','exhaust',new THREE.BoxGeometry(0.88,0.2,0.26),0x7d6338,[-1.25,0.08,0.78],[1,1,1]);
  addPart('catalytic_converter','exhaust',new THREE.CylinderGeometry(0.17,0.17,0.65,12),0xa87434,[0.72,-0.18,0.88],[1,1,1]);
  addPart('fuel_rail','fuel_system',new THREE.BoxGeometry(0.9,0.1,0.12),0x5f657f,[-0.85,0.64,-0.36],[1,1,1]);
  addPart('fuel_pump','fuel_system',new THREE.CylinderGeometry(0.11,0.11,0.24,10),0x6d7396,[-2.2,-0.3,0],[1,1,1]);
  addPart('battery','electrical_system',new THREE.BoxGeometry(0.58,0.36,0.42),0x357757,[0.92,0.42,-0.72],[1,1,1]);
  addPart('alternator','electrical_system',new THREE.CylinderGeometry(0.17,0.17,0.3,12),0x4d9b72,[0.15,0.24,-0.58],[1,1,1]);
  addPart('transmission_case','transmission',new THREE.BoxGeometry(1.3,0.62,0.9),0x6c7683,[-2.05,0.08,0],[1,1,1]);
  const body=new THREE.Mesh(new THREE.BoxGeometry(5.1,1.2,2.25),new THREE.MeshStandardMaterial({color:0x243241,roughness:0.82,metalness:0.12,transparent:true,opacity:0.95})); body.position.y=0.6; root.add(body);
  [[-1.7,-0.05,1.2],[-1.7,-0.05,-1.2],[1.7,-0.05,1.2],[1.7,-0.05,-1.2]].forEach(p=>{const w=new THREE.Mesh(new THREE.CylinderGeometry(0.47,0.47,0.3,16),new THREE.MeshStandardMaterial({color:0x0b1120,roughness:0.95})); w.rotation.z=Math.PI/2; w.position.set(...p); root.add(w);});

  const raycaster=new THREE.Raycaster(); const pointer=new THREE.Vector2();
  function applyHighlight(action,component,system){
    Object.values(partMap).forEach(m=>{m.material.color.setHex(m.userData.baseColor); m.material.opacity=1;});
    selectedMesh=null;
    if(action==='highlight_component' && partMap[component]){selectedMesh=partMap[component]; selectedMesh.material.color.set('#00E5FF'); Object.values(partMap).forEach(m=>{if(m!==selectedMesh){m.material.opacity=(m.userData.system===selectedMesh.userData.system)?0.42:0.16;}});} 
    if(action==='highlight_system' && system){Object.values(partMap).forEach(m=>{if(m.userData.system===system){m.material.color.set('#00E5FF'); m.material.opacity=0.94;} else {m.material.opacity=0.16;}});} 
    const name=component|| (selectedMesh?selectedMesh.userData.component:null) || (system?`${system} system` : null);
    const hasComponent = Boolean(component);
    document.getElementById('highlightedComponentName').textContent=name?formatLabel(name):'No highlighted component';
    document.getElementById('componentExplainBtn').disabled=!hasComponent;
    document.getElementById('componentExplainBtn').dataset.component=component || '';
    document.getElementById('componentExplainState').textContent=hasComponent?'Explain':'Select Part';
    applyTone('componentStatusPill', hasComponent ? 'accent' : system ? 'warning' : 'neutral');
    updateBadge('inspectionBadge', 'inspectionBadgeText', hasComponent ? 'accent' : system ? 'warning' : 'neutral', hasComponent ? `Focused: ${formatLabel(component)}` : system ? `${formatLabel(system)} selected` : 'No highlighted component');
  }
  function pick(ev){ const rect=renderer.domElement.getBoundingClientRect(); pointer.x=((ev.clientX-rect.left)/rect.width)*2-1; pointer.y=-((ev.clientY-rect.top)/rect.height)*2+1; raycaster.setFromCamera(pointer,camera); const hit=raycaster.intersectObjects(Object.values(partMap))[0]; if(!hit){return;} const {component,system}=hit.object.userData; applyHighlight('highlight_component',component,system); syncHighlight('highlight_component',component,system,'user'); }
  renderer.domElement.addEventListener('pointerdown',pick,{passive:true});

  function animate(){ root.rotation.y += 0.0028; renderer.render(scene,camera); requestAnimationFrame(animate); }
  animate();
  window.addEventListener('resize',()=>{ if(!host.clientWidth || !host.clientHeight){return;} camera.aspect=host.clientWidth/host.clientHeight; camera.updateProjectionMatrix(); renderer.setSize(host.clientWidth,host.clientHeight); });
  vehicleRenderer={applyHighlight};
}

function showFallbackImage(reason){ const img=document.getElementById('vehicleImageAsset'); img.style.display='block'; const source=document.getElementById('vehicleImageSource'); source.textContent=reason; const host=document.getElementById('vehicleCanvas'); if(host){host.innerHTML='';} }

async function fetchState(){
  const res = await fetch('/dashboard/state');
  state = await res.json();
  phoneBridge = { ...phoneBridge, ...state.phone_bridge };
  const backendPermissionStatus = normalizePermissionStatus(state?.phone_bridge?.permission_state);
  setLocalConnectionDiagnostics({
    bluetooth_permission_status: backendPermissionStatus === 'unknown' ? localConnectionDiagnostics.bluetooth_permission_status : backendPermissionStatus,
    mode_switched_to_phone_live: state?.current_mode === 'PHONE-LIVE',
    backend_ingest_success: state?.phone_bridge?.backend_acceptance_status === 'accepted',
    first_pid_response_received: Boolean(state?.phone_bridge?.first_live_read_received),
    adapter_connected: state?.phone_bridge?.status === 'connected',
  });
  detectTransport();
  renderVehicles(); renderDashboardState(); renderBluetoothCard(); renderAiAlerts(); renderTimeline(); syncLivePolling();
  if(vehicleRenderer && state.vehicle_visualization?.highlight){ const h=state.vehicle_visualization.highlight; vehicleRenderer.applyHighlight(h.action,h.component,h.system); }
}
function renderVehicles(){
  const select = document.getElementById('vehicleSelect');
  const vehicles = state?.vehicles || [];
  if(!select || !vehicles.length){ return; }
  if(state?.active_session?.vehicle_id && vehicles.some((vehicle) => vehicle.vehicle_id === state.active_session.vehicle_id)){
    selectedVehicleId = state.active_session.vehicle_id;
  } else if(!vehicles.some((vehicle) => vehicle.vehicle_id === selectedVehicleId)){
    selectedVehicleId = vehicles[0].vehicle_id;
  }
  const nextKey = vehicles.map((vehicle) => `${vehicle.vehicle_id}:${vehicle.label}:${vehicle.protocol_hint}`).join('|');
  if(nextKey !== renderedVehicleKey){
    select.innerHTML = '';
    vehicles.forEach((vehicle) => {
      const option = document.createElement('option');
      option.value = vehicle.vehicle_id;
      option.textContent = `${vehicle.label} (${vehicle.protocol_hint})`;
      if(vehicle.vehicle_id === selectedVehicleId){ option.selected = true; }
      select.appendChild(option);
    });
    renderedVehicleKey = nextKey;
  }
  select.value = selectedVehicleId;
  select.onchange = async (event) => {
    selectedVehicleId = event.target.value;
    renderDashboardState();
    if(lastVehicleImageSelection !== selectedVehicleId){
      lastVehicleImageSelection = selectedVehicleId;
      await updateVehicleImage();
    }
  };
  if(lastVehicleImageSelection !== selectedVehicleId){
    lastVehicleImageSelection = selectedVehicleId;
    updateVehicleImage();
  }
}
function getTransportPresentation(){
  const detection = detectTransport();
  const connected = transportServiceReady();
  const status = phoneBridge.status || 'disconnected';
  const errorMessage = transportContext.last_connection_error || phoneBridge.last_error || phoneBridge.fallback_reason || detection.message;
  if(connected){
    return {
      tone: 'success',
      label: 'Connected',
      detail: transportContext.first_pid_read_logged || phoneBridge.first_live_read_received
        ? 'Bluetooth transport is connected and live PID reads are running.'
        : 'Bluetooth transport is connected. Waiting for the first live PID read.',
    };
  }
  if(status === 'connecting'){
    return {
      tone: 'warning',
      label: 'Connecting',
      detail: 'Negotiating Bluetooth link and waiting for transport startup.',
    };
  }
  if(status === 'failed'){
    return {
      tone: 'danger',
      label: 'Error',
      detail: errorMessage,
    };
  }
  if(detection.selected_transport === 'web-bluetooth'){
    return {
      tone: 'accent',
      label: 'Web Bluetooth available',
      detail: 'Desktop Chrome or Edge can use Web Bluetooth for live PID reads.',
    };
  }
  if(['Native bridge missing', 'Native bridge required'].includes(detection.status_label)){
    return {
      tone: 'warning',
      label: detection.status_label,
      detail: detection.message,
    };
  }
  if(detection.selected_transport === 'native-phone-bridge'){
    return {
      tone: 'accent',
      label: 'Native bridge ready',
      detail: 'Native phone bridge detected. Tap Connect Vehicle to start live reads.',
    };
  }
  return {
    tone: 'neutral',
    label: 'Unsupported environment',
    detail: detection.message,
  };
}
function renderDashboardState(){
  const active = state?.active_session;
  const profile = getVehicleProfileById(active?.vehicle_id || selectedVehicleId);
  const transportStatus = getTransportPresentation();
  const connected = transportServiceReady();
  const hasSession = Boolean(active);
  const vehicleCheckActive = Boolean(active && connected);
  const captureRecording = connected && state?.capture_status === 'recording';
  const waitingForLiveRead = connected && !phoneBridge.first_live_read_received;
  const aiMonitoringActive = connected && Boolean(state?.ai_monitoring?.active);
  const liveReadActive = connected && (captureRecording || phoneBridge.ai_monitoring_active || (state?.recent_reads || []).some((read) => LIVE_TELEMETRY_KEYS.includes(read.pid_key) && isFreshRead(read)));
  const aiReady = hasSession;
  const bluetoothTone = transportStatus.tone;
  const bluetoothLabel = transportStatus.label;
  const streamTone = liveReadActive ? 'accent' : waitingForLiveRead ? 'warning' : 'neutral';
  const streamLabel = liveReadActive ? 'Streaming' : waitingForLiveRead ? 'Priming' : 'Idle';
  const aiTone = aiReady ? (aiMonitoringActive ? 'accent' : 'success') : 'neutral';
  const aiLabel = aiReady ? (aiMonitoringActive ? 'Monitoring' : 'Ready') : 'Offline';
  const protocolLabel = formatMode(active?.protocol || profile?.protocol_hint || 'protocol pending');
  const profileNote = profile?.notes || 'Safe read-only diagnostic workflow enabled.';

  document.getElementById('headerVehicleName').textContent = getVehicleDisplayName();
  document.getElementById('headerVehicleDetail').textContent = vehicleCheckActive
    ? `${protocolLabel} workflow armed. ${profileNote} Vehicle check ${shortId(active.session_id)} is active and ready for live context.`
    : hasSession
      ? `${protocolLabel} workflow armed. ${profileNote} Vehicle check ${shortId(active.session_id)} is prepared and waiting for a supported Bluetooth transport.`
    : `${protocolLabel} workflow armed. ${profileNote} Start a vehicle check to unlock capture, reports, and full AI context.`;
  document.getElementById('vehicleProfileNote').textContent = profileNote;

  updateIndicator('statusBluetooth', 'statusBluetoothValue', bluetoothTone, bluetoothLabel);
  updateIndicator('statusStreaming', 'statusStreamingValue', streamTone, streamLabel);
  updateIndicator('statusAiAssist', 'statusAiAssistValue', aiTone, aiLabel);

  updateBadge('sessionBadge', 'sessionBadgeText', vehicleCheckActive ? 'success' : 'neutral', vehicleCheckActive ? 'Vehicle Check Active' : 'Vehicle Check Idle');
  updateBadge('captureBadge', 'captureBadgeText', captureRecording ? 'danger' : state?.capture_status === 'stopped' ? 'warning' : 'neutral', captureRecording ? 'Capture Recording' : state?.capture_status === 'stopped' ? 'Capture Stopped' : 'Capture Idle', captureRecording);
  updateBadge('modeBadge', 'modeBadgeText', connected ? 'accent' : 'neutral', formatMode(state?.current_mode || phoneBridge.current_mode || 'standby'));
  const who = state?.current_user?.display_name || 'Demo Tester';
  document.getElementById('userBadgeText').textContent = `Signed in: ${who}`;
  updateBadge('diagnosticStateBadge', 'diagnosticStateText', captureRecording ? 'danger' : vehicleCheckActive ? 'success' : connected ? 'accent' : transportStatus.tone, captureRecording ? 'Recording live capture' : vehicleCheckActive ? 'Vehicle check active' : connected ? 'Connected and ready' : transportStatus.label, captureRecording);
  updateBadge('aiPanelBadge', 'aiPanelBadgeText', aiMonitoringActive ? 'accent' : aiReady ? 'success' : 'neutral', aiMonitoringActive ? 'Live monitoring active' : aiReady ? 'AI ready for diagnostics' : 'Awaiting vehicle check');

  renderTelemetry();

  const connectMeta = connected
    ? `OBDLink bridge active. ${liveReadActive ? 'Live telemetry is streaming.' : 'Waiting for live PID traffic.'}`
    : transportStatus.detail;
  document.getElementById('connectVehicleMeta').textContent = connectMeta;
  setButtonState(
    'connectVehicleBtn',
    connected ? 'success' : transportStatus.tone,
    connected ? 'Connected' : phoneBridge.status === 'failed' ? 'Retry' : phoneBridge.status === 'connecting' ? 'Connecting' : transportContext.selected_transport === 'unsupported' ? 'Unavailable' : 'Connect',
    connected,
    phoneBridge.status === 'connecting',
    connected,
    transportContext.selected_transport === 'unsupported' || phoneBridge.status === 'connecting',
  );

  setButtonState('startSessionBtn', vehicleCheckActive ? 'success' : 'accent', vehicleCheckActive ? 'Active' : 'Ready', vehicleCheckActive, false, vehicleCheckActive);
  setButtonState('stopSessionBtn', hasSession ? 'danger' : 'warning', hasSession ? 'Available' : 'Idle', hasSession, false, false);
  setButtonState('startCaptureBtn', captureRecording ? 'success' : 'accent', captureRecording ? 'Recording' : 'Ready', captureRecording, captureRecording, captureRecording);
  setButtonState('stopCaptureBtn', captureRecording ? 'danger' : 'warning', captureRecording ? 'Armed' : 'Idle', captureRecording, captureRecording, false);
  setButtonState('tagEventBtn', hasSession ? 'warning' : 'neutral', captureRecording ? 'Live Tagging' : hasSession ? 'Ready' : 'Session Needed', hasSession, captureRecording, false);
  setButtonState('reportsBtn', 'neutral', hasSession ? 'Review' : 'Available', hasSession, false, false);
  setButtonState('liveGaugesBtn', connected ? 'accent' : 'neutral', liveReadActive ? 'Live' : connected ? 'Ready' : 'Standby', connected, liveReadActive, connected, !connected);
  setButtonState('askAiBtn', aiMonitoringActive ? 'accent' : aiReady ? 'success' : 'accent', aiMonitoringActive ? 'Monitoring' : aiReady ? 'Ready' : 'Standby', Boolean(aiMonitoringActive || aiReady), false, false);
}
function renderTelemetry(){
  const transportRunning = transportServiceReady();
  const metrics = [
    { cardId:'metricCardRpm', valueId:'metricRpmValue', metaId:'metricRpmMeta', read:getLatestRead('rpm'), unit:'rpm', precision:0 },
    { cardId:'metricCardVoltage', valueId:'metricVoltageValue', metaId:'metricVoltageMeta', read:getLatestRead('control_module_voltage'), unit:'V', precision:1 },
    { cardId:'metricCardCoolant', valueId:'metricCoolantValue', metaId:'metricCoolantMeta', read:getLatestRead('coolant_temp'), unit:'C', precision:0 },
    { cardId:'metricCardThrottle', valueId:'metricThrottleValue', metaId:'metricThrottleMeta', read:getLatestRead('throttle_position'), unit:'%', precision:0 },
  ];
  metrics.forEach((metric) => {
    const summary = formatMetricValue(metric.read, metric.unit, metric.precision);
    document.getElementById(metric.valueId).textContent = summary.value;
    document.getElementById(metric.metaId).textContent = summary.meta;
    document.getElementById(metric.cardId).dataset.live = summary.live && transportRunning ? 'true' : 'false';
  });
}
async function updateVehicleImage(){
  const requestId = ++lastVehicleImageRequestId;
  const res = await fetch(`/vehicle-image/current?manual_vehicle_id=${encodeURIComponent(selectedVehicleId)}`);
  const image = await res.json();
  if(requestId !== lastVehicleImageRequestId){ return; }
  const trim = image.trim ? ` ${image.trim}` : '';
  const label = `${image.year || ''} ${image.make} ${image.model}${trim}`.replace(/\\s+/g,' ').trim();
  document.getElementById('vehicleImageLabel').textContent = label || 'Generic vehicle';
  const sourceMap = {
    auto_vin: 'Auto VIN detection result',
    manual_selection: 'Manual vehicle selection fallback',
    active_session_vehicle: 'Active session vehicle fallback',
    closest_supported: 'Closest supported vehicle image fallback',
    generic_placeholder: 'Generic placeholder image',
  };
  const img = document.getElementById('vehicleImageAsset');
  img.src = image.image_asset_path || image.fallback_image_asset_path;
  img.onerror = () => { img.src = image.fallback_image_asset_path; };
  if(!vehicleRenderer){ buildGenericVehicleViewer(); }
  if(!vehicleRenderer){
    showFallbackImage(`${sourceMap[image.resolved_from] || image.resolved_from} - WebGL fallback image`);
  } else {
    img.style.display = 'none';
    document.getElementById('vehicleImageSource').textContent = '3D view active - tap a component to inspect and explain';
  }
}
function renderBluetoothCard(){
  const transportStatus = getTransportPresentation();
  const connectionDiagnostics = resolvedConnectionDiagnostics();
  updateBadge('btStatusBadge', 'btStatusText', transportStatus.tone, transportStatus.label);
  document.getElementById('btError').textContent = phoneBridge.last_error || transportContext.last_connection_error || transportStatus.detail || 'No Bluetooth errors';
  const detailRows = [
    { label: 'Runtime platform', value: phoneBridge.platform || transportContext.runtime_platform || 'unknown' },
    { label: 'Selected transport', value: transportContext.selected_transport },
    { label: 'Native bridge detected', value: transportContext.native_bridge_detected ? 'true' : 'false' },
    { label: 'Web Bluetooth available', value: transportContext.navigator_bluetooth_available ? 'true' : 'false' },
    { label: 'Bluetooth link', value: transportServiceReady() ? 'Connected' : 'Idle' },
    { label: 'Polling state', value: `${transportServiceReady() && phoneBridge.polling_active ? 'Active' : 'Inactive'} (${phoneBridge.polling_state || 'inactive'})` },
    { label: 'First live read', value: phoneBridge.first_live_read_received ? 'Received' : 'Pending' },
    { label: 'Source mode', value: transportServiceReady() ? (phoneBridge.current_source_mode || phoneBridge.source_mode || 'unknown') : 'standby' },
    { label: 'Last PID command', value: phoneBridge.last_live_pid_command || 'None' },
    { label: 'Last PID response', value: phoneBridge.last_live_pid_response || 'None' },
    { label: 'Ingest status', value: phoneBridge.last_ingest_status || 'idle' },
    { label: 'Backend acceptance', value: phoneBridge.backend_acceptance_status || 'idle' },
    { label: 'Last connection error', value: transportContext.last_connection_error || phoneBridge.last_error || 'None' },
  ];
  Object.entries(connectionDiagnostics).forEach(([label, value]) => {
    detailRows.push({ label, value: formatDiagnosticValue(value) });
  });
  if(phoneBridge.fallback_reason){ detailRows.push({ label: 'Fallback reason', value: phoneBridge.fallback_reason }); }
  if((state.current_mode || phoneBridge.current_mode) === 'MOCK'){ detailRows.push({ label: 'Mock mode reason', value: phoneBridge.mock_reason || state.current_mode_reason || 'Unknown' }); }
  document.getElementById('btDebug').innerHTML = detailRows.map((row) => `<div class="detail-row"><span class="detail-label">${escapeHtml(row.label)}</span><span class="detail-value">${escapeHtml(row.value)}</span></div>`).join('');
}
function alertTone(confidence){
  if(confidence === 'high confidence'){ return 'danger'; }
  if(confidence === 'likely' || confidence === 'suspected'){ return 'warning'; }
  return 'success';
}
function renderAiAlerts(){
  const alerts = state.ai_alerts || [];
  const monitoring = state.ai_monitoring || { active:false, message:'Waiting for live monitoring.' };
  const transportRunning = transportServiceReady();
  document.getElementById('aiAlertSummary').textContent = alerts.length
    ? `${alerts.length} proactive alert(s) linked to this vehicle check.`
    : (transportRunning && monitoring.active ? 'Live monitoring active.' : 'No active alerts.');
  document.getElementById('aiAlertList').innerHTML = alerts.slice(-5).reverse().map((alert) => `
    <div class="alert-item">
      <div class="alert-head">
        <strong>${escapeHtml(alert.title)}</strong>
        <span class="confidence-pill" data-tone="${escapeHtml(alertTone(alert.confidence))}">${escapeHtml(alert.confidence)}</span>
      </div>
      <div class="tiny">${escapeHtml(alert.explanation)}</div>
      <div class="tiny"><span class="detail-label">Next</span> ${escapeHtml(alert.suggested_next_step)}</div>
    </div>
  `).join('') || `<div class="empty-state">${escapeHtml(transportRunning ? monitoring.message : 'Waiting for live monitoring.')}</div>`;
}
function renderTimeline(){
  const timeline = state.diagnostic_timeline || [];
  document.getElementById('timelineList').innerHTML = timeline.slice(-8).reverse().map((item) => `
    <div class="timeline-item">
      <span class="timeline-dot" aria-hidden="true"></span>
      <div>
        <div class="timeline-top">
          <strong>${escapeHtml(item.title)}</strong>
          <span class="tiny">${escapeHtml(new Date(item.ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }))}</span>
        </div>
        <div class="tiny">${escapeHtml(item.detail || '')}${item.linked_ai_response_id ? ` <a href="/dashboard/ai">AI explanation</a>` : ''}</div>
      </div>
    </div>
  `).join('') || '<div class="empty-state">No timeline events yet.</div>';
}
async function ensureSession(){ if (state && state.active_session) return state.active_session; await fetch('/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId})}); await fetchState(); return state.active_session; }
async function createSession(){ await ensureSession(); }
async function ensureTransportForPolling(){
  const detection = detectTransport();
  const service = getTransportService(detection);
  if(!service){ return null; }
  if(detection.selected_transport === 'web-bluetooth' && typeof service.reconnectIfNeeded === 'function' && !service.isConnected()){
    try {
      await service.reconnectIfNeeded();
      transportLog('connect success', { reconnected: true, source_mode: 'BROWSER-DEV' });
    } catch (error) {
      const reason = error?.message || 'web-bluetooth-reconnect-failed';
      setLastConnectionError(reason);
      phoneBridge = { ...phoneBridge, status: 'failed', last_error: reason, fallback_reason: reason };
      transportLog('connect failure', { reason });
      return null;
    }
  }
  return service;
}
async function pollLiveSensorsOnce(){
  if(livePollingInFlight){ return; }
  if(!transportServiceReady()){
    stopLivePolling();
    return;
  }
  const service = await ensureTransportForPolling();
  if(!service || typeof service.readPid !== 'function'){
    await fetchState();
    return;
  }
  const active = await ensureSession();
  livePollingInFlight = true;
  try {
    const nativeReads = await Promise.allSettled(LIVE_SENSOR_ORDER.map((sensor) => readTransportPid(sensor)));
    const ingestJobs = nativeReads
      .filter((result) => result.status === 'fulfilled' && hasUsablePidRead(result.value))
      .map(async (result) => {
        const response = await fetch('/phone/bridge/read', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({session_id: active.session_id, vehicle_id: active.vehicle_id, ...result.value, polling: true}),
        });
        if(response.ok){
          setLocalConnectionDiagnostics({ backend_ingest_success: true });
        }
        return response;
      });
    if(ingestJobs.length){ await Promise.allSettled(ingestJobs); }
  } finally {
    livePollingInFlight = false;
    await fetchState();
  }
}
function startLivePolling(){
  if(livePollingHandle || !transportServiceReady()){ return; }
  transportLog('polling start', { polling_interval_ms: 500 });
  pollLiveSensorsOnce();
  livePollingHandle = setInterval(() => { pollLiveSensorsOnce(); }, 500);
}
function stopLivePolling(){ if(livePollingHandle){ clearInterval(livePollingHandle); livePollingHandle=null; } }
function syncLivePolling(){ if(transportServiceReady() && state?.active_session && (phoneBridge.polling_state||'inactive')!=='inactive'){ startLivePolling(); return; } stopLivePolling(); }
async function stopSession(){ stopLivePolling(); await fetch('/sessions/active/stop',{method:'POST'}); await fetchState(); }
async function startCapture(){ await ensureSession(); await fetch('/capture/start',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId,preset:'cold_start_capture'})}); await fetchState(); }
async function stopCapture(){ await fetch('/capture/stop',{method:'POST'}); await fetchState(); }
async function tagEvent(){ if (!state.active_session) { alert('Vehicle Check missing: start Vehicle Check first.'); return; } await fetch('/capture/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:'idle'})}); await fetchState(); }
async function connectVehicle(){
  const detection = detectTransport();
  transportContext = { ...transportContext, first_pid_read_logged: false };
  transportLog('connect attempt start', { requested_transport: detection.selected_transport });
  resetLocalConnectionDiagnostics({
    permissionStatus: localConnectionDiagnostics.bluetooth_permission_status,
    nativeBridgeAvailable: detection.native_bridge_detected,
  });
  if(detection.selected_transport === 'unsupported'){
    const reason = detection.message;
    const failedPayload = normalizeConnectPayload({
      platform: detection.runtime_platform,
      status: 'failed',
      source_mode: 'PHONE-LIVE',
      supports_native_bluetooth: detection.native_bridge_detected,
      fallback_reason: reason,
    }, detection);
    setLastConnectionError(reason);
    phoneBridge = { ...phoneBridge, platform: failedPayload.platform, status: 'failed', source_mode: failedPayload.source_mode, last_error: reason, fallback_reason: reason };
    renderDashboardState();
    renderBluetoothCard();
    await publishBridgeConnection(failedPayload);
    await fetchState();
    return;
  }

  const service = getTransportService(detection);
  await resetRealConnectionSequence(service);
  phoneBridge = {
    ...phoneBridge,
    platform: detection.runtime_platform,
    status: 'connecting',
    source_mode: detection.selected_transport === 'web-bluetooth' ? 'BROWSER-DEV' : 'PHONE-LIVE',
    last_error: null,
    fallback_reason: null,
  };
  setLocalConnectionDiagnostics({
    adapter_discovery_started: true,
    adapter_connection_attempted: true,
  });
  setLastConnectionError(null);
  renderDashboardState();
  renderBluetoothCard();

  try {
    const permissionStatus = await ensureBluetoothPermissionForConnection(service, detection);
    if(['denied', 'blocked', 'restricted'].includes(permissionStatus)){
      throw new Error(`bluetooth-permission-${permissionStatus}`);
    }
    await publishBridgeConnection(normalizeConnectPayload({
      platform: detection.runtime_platform,
      status: 'connecting',
      permission_state: permissionStatus || phoneBridge.permission_state || 'unknown',
      source_mode: detection.selected_transport === 'web-bluetooth' ? 'BROWSER-DEV' : 'PHONE-LIVE',
      supports_native_bluetooth: detection.native_bridge_detected,
    }, detection));
    const rawConnectPayload = await service.connectToAdapter();
    const connectPayload = normalizeConnectPayload(rawConnectPayload, detection);
    if(connectPayload.status !== 'connected'){
      throw new Error(connectPayload.fallback_reason || 'transport-connect-failed');
    }
    setLocalConnectionDiagnostics({
      bluetooth_permission_status: normalizePermissionStatus(connectPayload.permission_state || localConnectionDiagnostics.bluetooth_permission_status),
      adapter_found: true,
      adapter_connected: true,
    });
    await ensureSession();
    await publishBridgeConnection(connectPayload);
    phoneBridge = { ...phoneBridge, platform: connectPayload.platform || detection.runtime_platform, status: 'connected', source_mode: connectPayload.source_mode, last_error: null, fallback_reason: null };
    transportLog('connect success', { source_mode: connectPayload.source_mode, adapter_name: connectPayload.adapter_name });
    await fetchState();
    if(transportServiceReady()){ startLivePolling(); }
  } catch (error) {
    const reason = error?.message || 'transport-connect-failed';
    const failedPayload = normalizeConnectPayload({ status:'failed', fallback_reason: reason }, detection);
    setLastConnectionError(reason);
    setLocalConnectionDiagnostics({ adapter_connected: false });
    phoneBridge = { ...phoneBridge, platform: failedPayload.platform, status: 'failed', source_mode: failedPayload.source_mode, last_error: reason, fallback_reason: reason };
    transportLog('connect failure', { reason });
    try {
      if(service && typeof service.disconnectAdapter === 'function'){
        await service.disconnectAdapter();
      }
    } catch (disconnectError) {
      console.debug('[transport]', { event: 'disconnect cleanup failed', error: disconnectError?.message || String(disconnectError) });
    }
    await publishBridgeConnection(failedPayload);
    await fetchState();
  }
}
async function syncHighlight(action,component,system,source='user'){ await fetch('/vehicle-visualization/highlight',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,component,system,source})}); }
async function explainSelected(){ const comp=document.getElementById('componentExplainBtn').dataset.component; if(!comp){return;} const res=await fetch(`/vehicle-visualization/explain?component=${encodeURIComponent(comp)}`); const data=await res.json(); alert(`${data.component.replace(/_/g,' ')}: ${data.explanation}`); }
function openAiWithPrompt(prompt){ window.location.href = `/dashboard/ai?prompt=${encodeURIComponent(prompt || '')}`; }
window.openAiWithPrompt = openAiWithPrompt;
document.getElementById('connectVehicleBtn').onclick = connectVehicle;
document.getElementById('startSessionBtn').onclick = createSession;
document.getElementById('stopSessionBtn').onclick = stopSession;
document.getElementById('startCaptureBtn').onclick = startCapture;
document.getElementById('stopCaptureBtn').onclick = stopCapture;
document.getElementById('tagEventBtn').onclick = tagEvent;
document.getElementById('reportsBtn').onclick = () => alert('Reports view is available in the report framework section.');
document.getElementById('liveGaugesBtn').onclick = () => window.location.href = '/dashboard/gauges';
document.getElementById('askAiBtn').onclick = () => openAiWithPrompt('');
document.getElementById('componentExplainBtn').onclick = explainSelected;
window.addEventListener('zeb-native-poll', handleNativePollEvent);
window.addEventListener('zeb-native-disconnected', (event) => { void handleNativeDisconnectEvent(event); });
setInterval(fetchState, 1500);
fetchState();
</script>
</body>
</html>
    """


@app.get('/dashboard/gauges', response_class=HTMLResponse)
def gauge_dashboard() -> str:
    return """
<!doctype html><html><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>Live Gauges</title>
<style>
body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#edf2f7;color:#0f172a;} .wrap{max-width:1024px;margin:0 auto;padding:14px;display:grid;gap:12px;} .card{background:#fff;border:1px solid #d4dde7;border-radius:14px;padding:14px;} .grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;} .gauge-grid{display:grid;gap:10px;} .gauge{border:1px solid #d4dde7;border-radius:12px;padding:10px;background:#fcfdff;} .tiny{font-size:.8rem;color:#475569;} button,select,input{width:100%;border-radius:12px;border:1px solid #d4dde7;padding:12px;} button{background:#0f766e;color:#fff;font-weight:700;} button.secondary{background:#fff;color:#0f172a;}
</style></head><body><div class="wrap"><div class="card"><h1 style="margin:0">Live Gauges</h1><div class="tiny">Dedicated 4-gauge page with side controls and 500ms live polling.</div></div>
<div class="card"><div class="grid2"><input id="presetName" value="Default" placeholder="Preset name" /><div class="grid2"><button id="savePresetBtn" class="secondary">Save Preset</button><button id="loadPresetBtn" class="secondary">Load Preset</button></div></div><div class="gauge-grid" id="gaugeGrid" style="margin-top:10px;"></div></div>
<div class="card"><div class="grid2"><button id="startPollingBtn">Start Live Polling</button><button id="stopPollingBtn" class="secondary">Stop Live Polling</button></div><button id="backBtn" class="secondary" style="margin-top:8px;">Back to Main Dashboard</button></div>
</div>
<script>
let state=null; let pollingHandle=null; let pollingInFlight=false; let phoneBridge={status:'disconnected'};
const SENSOR_TO_PID={rpm:'010C',coolant_temp:'0105',control_module_voltage:'0142',intake_air_temp:'010F',vehicle_speed:'010D',throttle_position:'0111'};
const GAUGE_SENSORS=['rpm','coolant_temp','control_module_voltage','vehicle_speed','throttle_position','intake_air_temp'];
const REQUIRED_NATIVE_GAUGE_BRIDGE_METHODS=['connectToAdapter','disconnectAdapter','getConnectionState','readPid','readVin','startPolling','stopPolling'];
let gauges=[0,1,2,3].map(i=>({slot:i+1,sensor:GAUGE_SENSORS[i]||'rpm',label:`Gauge ${i+1}`,min:0,max:8000,unit:'',warn:4500,critical:6000}));
function getMobileBluetoothService(){ const service=window.MobileBluetoothService || null; if(!service){ return null; } return REQUIRED_NATIVE_GAUGE_BRIDGE_METHODS.every((method)=>typeof service[method]==='function') ? service : null; }
async function readNativePid(sensor){
  const service = getMobileBluetoothService();
  if(!service || typeof service.readPid !== 'function'){ return null; }
  try {
    const nativeRead = await service.readPid(SENSOR_TO_PID[sensor]);
    if(!nativeRead){ return null; }
    const rawResponse = nativeRead.raw_response || nativeRead.rawResponse || null;
    if(!rawResponse){ return null; }
    return {
      command: SENSOR_TO_PID[sensor],
      pid_key: sensor,
      source_mode: nativeRead.source_mode || nativeRead.sourceMode || 'PHONE-LIVE',
      source_hint: nativeRead.source_hint || nativeRead.sourceHint || 'iso9141_2',
      raw_response: rawResponse,
      value: nativeRead.value ?? null,
      unit: nativeRead.unit ?? null,
      latency_ms: nativeRead.latency_ms ?? nativeRead.latencyMs ?? null,
      error: nativeRead.error ?? null,
      backend_status: nativeRead.backend_status || nativeRead.backendStatus || undefined,
      ts: nativeRead.ts || nativeRead.timestamp || undefined,
    };
  } catch (error) {
    phoneBridge = { ...phoneBridge, last_error: error?.message || 'native-read-failed' };
    return null;
  }
}
function gaugeEditor(idx,g){return `<div class="gauge"><h4>${g.label}</h4><div class="grid2"><select data-field="sensor" data-idx="${idx}">${GAUGE_SENSORS.map(s=>`<option value="${s}" ${g.sensor===s?'selected':''}>${s}</option>`).join('')}</select><input data-field="label" data-idx="${idx}" value="${g.label}" placeholder="Label" /><input data-field="unit" data-idx="${idx}" value="${g.unit}" placeholder="Unit" /><input data-field="warn" data-idx="${idx}" type="number" value="${g.warn}" placeholder="Warn" /><input data-field="critical" data-idx="${idx}" type="number" value="${g.critical}" placeholder="Critical" /></div><div class="tiny" id="gaugeLive${idx}">Disconnected</div></div>`;}
function renderGauges(){const grid=document.getElementById('gaugeGrid');grid.innerHTML=gauges.map((g,i)=>gaugeEditor(i,g)).join('');grid.querySelectorAll('input,select').forEach(el=>el.onchange=(e)=>{const i=Number(e.target.dataset.idx);const f=e.target.dataset.field;gauges[i][f]=e.target.type==='number'?Number(e.target.value):e.target.value;});const reads=(state&&state.recent_reads?state.recent_reads:[]).slice().reverse();gauges.forEach((g,i)=>{const found=reads.find(r=>r.pid_key===g.sensor||r.command===SENSOR_TO_PID[g.sensor]);document.getElementById(`gaugeLive${i}`).textContent=found?`${g.label}: ${found.value ?? found.raw_response} ${g.unit || found.unit || ''} [${found.source_mode}]`:`${g.label}: ${(phoneBridge.status==='connected')?'Waiting for 500ms polling':'Disconnected'}`;});}
async function fetchState(){const res=await fetch('/dashboard/state');state=await res.json();phoneBridge={...phoneBridge,...state.phone_bridge};renderGauges();syncGaugePolling();}

function renderAiAlerts(){
  const alerts=state.ai_alerts||[];
  const monitoring=state.ai_monitoring||{active:false,message:'Waiting for live monitoring.'};
  document.getElementById('aiAlertSummary').textContent=alerts.length?`${alerts.length} proactive alert(s) linked to this vehicle check.`:(monitoring.active?'Live monitoring active.':'No active alerts.');
  document.getElementById('aiAlertList').innerHTML=alerts.slice(-5).reverse().map(a=>`<div><b>${a.title}</b> (${a.confidence}) — ${a.explanation}. Next: ${a.suggested_next_step}</div>`).join('') || `<div>${monitoring.message}</div>`;
}
function renderTimeline(){
  const timeline=state.diagnostic_timeline||[];
  document.getElementById('timelineList').innerHTML=timeline.slice(-8).reverse().map(t=>`<div>${new Date(t.ts).toLocaleTimeString()} — ${t.title}${t.linked_ai_response_id?` <a href="/dashboard/ai" style="color:#0f766e">AI explanation</a>`:''}</div>`).join('') || '<div>No timeline events yet.</div>';
}

async function ensureSession(){if(state&&state.active_session)return state.active_session;await fetch('/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicle_id:'toyota_sienna_2006'})});await fetchState();return state.active_session;}
async function pollAllGaugesOnce(){if(pollingInFlight){return;}if((phoneBridge.status||'disconnected')!=='connected'){stopGaugePolling();return;}const service=getMobileBluetoothService();if(!service||typeof service.readPid!=='function'){await fetchState();return;}const active=await ensureSession();pollingInFlight=true;try{const nativeReads=await Promise.allSettled(gauges.map(g=>readNativePid(g.sensor)));const ingestJobs=nativeReads.filter(result=>result.status==='fulfilled'&&result.value).map(result=>fetch('/phone/bridge/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:active.session_id,vehicle_id:active.vehicle_id,...result.value,polling:true})}));if(ingestJobs.length){await Promise.allSettled(ingestJobs);}}finally{pollingInFlight=false;await fetchState();}}
function startGaugePolling(){if(pollingHandle)return;pollAllGaugesOnce();pollingHandle=setInterval(()=>{pollAllGaugesOnce();},500);} function stopGaugePolling(){if(pollingHandle){clearInterval(pollingHandle);pollingHandle=null;}}
function syncGaugePolling(){if((phoneBridge.status||'disconnected')==='connected'&&state?.active_session&&(phoneBridge.polling_state||'inactive')!=='inactive'){startGaugePolling();return;}stopGaugePolling();}
function savePreset(){const name=document.getElementById('presetName').value||'Default';localStorage.setItem(`gaugePreset:${name}`,JSON.stringify(gauges));}
function loadPreset(){const name=document.getElementById('presetName').value||'Default';const raw=localStorage.getItem(`gaugePreset:${name}`);if(raw){gauges=JSON.parse(raw);renderGauges();}}
window.addEventListener('zeb-native-disconnected', async (event)=>{const detail=event?.detail||{};const reason=detail.last_error||detail.fallback_reason||'adapter-disconnected';phoneBridge={...phoneBridge,...detail,status:'disconnected',last_error:reason,fallback_reason:reason};stopGaugePolling();try{await fetch('/phone/bridge/disconnect',{method:'POST'});}catch(error){console.debug('[transport]',{event:'gauge native disconnect sync failed',error:error?.message||String(error)});}await fetchState();});
document.getElementById('startPollingBtn').onclick=startGaugePolling; document.getElementById('stopPollingBtn').onclick=stopGaugePolling; document.getElementById('savePresetBtn').onclick=savePreset; document.getElementById('loadPresetBtn').onclick=loadPreset; document.getElementById('backBtn').onclick=()=>window.location.href='/dashboard';
setInterval(fetchState,1200); fetchState();
</script></body></html>
    """


@app.get('/dashboard/ai', response_class=HTMLResponse)
def ai_dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI Mechanic</title>
<style>
body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#edf2f7;color:#0f172a}
.wrap{max-width:980px;margin:0 auto;padding:14px;display:grid;gap:12px}
.card{background:#fff;border:1px solid #d4dde7;border-radius:14px;padding:14px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
input,button{width:100%;border-radius:12px;border:1px solid #d4dde7;padding:12px}
button{background:#0f766e;color:#fff;font-weight:700}
button.secondary{background:#fff;color:#0f172a}
.quick{display:flex;flex-wrap:wrap;gap:8px}
.quick button{width:auto}
.chat{max-height:330px;overflow:auto;display:grid;gap:8px}
.bubble{border:1px solid #d4dde7;border-radius:12px;padding:10px;background:#f8fafc}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#fee2e2;color:#991b1b;font-size:.75rem;font-weight:700}
.tiny{font-size:.85rem;color:#475569}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1 style="margin:0">AI Mechanic</h1>
    <div class="badge">READ-ONLY DIAGNOSTIC / ADVISORY</div>
    <div class="tiny" style="margin-top:8px">Voice-enabled assistant for safe live reads, DTC explanations, and memory-supported guidance. Unsafe controls are permanently blocked.</div>
  </div>

  <div class="card">
    <div class="row">
      <button id="micBtn">🎤 Start Listening</button>
      <button id="stopMicBtn" class="secondary">Stop Listening</button>
      <button id="speakBtn">🔊 Play Response</button>
      <button id="stopSpeakBtn" class="secondary">Stop Audio</button>
    </div>
    <div class="tiny" id="voiceState" style="margin-top:8px">Voice state: idle</div>
    <div class="tiny" id="transcriptPreview">Transcript preview: (none)</div>
  </div>

  <div class="card">
    <input id="question" placeholder="Speak or type your question" />
    <button id="sendBtn" style="margin-top:8px">Send</button>
    <div class="quick" style="margin-top:10px">
      <button class="secondary" onclick="setPrompt('What does this code mean?')">What does this code mean?</button>
      <button class="secondary" onclick="setPrompt('Is it safe to drive?')">Is it safe to drive?</button>
      <button class="secondary" onclick="setPrompt('What should I test next?')">What should I test next?</button>
      <button class="secondary" onclick="setPrompt('Explain this in simple language')">Explain this in simple language</button>
      <button class="secondary" onclick="setPrompt('What changed in live data?')">What changed in live data?</button>
      <button class="secondary" onclick="setPrompt('What should I watch right now?')">What should I watch right now?</button>
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0">Conversation</h3>
    <div class="tiny" id="aiLoading">AI status: idle</div><div id="aiResponse" class="bubble" style="margin-top:8px">Response will appear here.</div><div id="chat" class="chat" style="margin-top:8px"></div>
  </div>

  <div class="card"><button class="secondary" id="backBtn">Back to Main Dashboard</button></div>
</div>
<script>
let lastAnswer='';
let recog=null;
const synth=window.speechSynthesis;
function pushMsg(role,text){const el=document.createElement('div');el.className='bubble';el.innerHTML=`<b>${role}:</b> ${text}`;document.getElementById('chat').prepend(el);}
function setPrompt(v){document.getElementById('question').value=v;document.getElementById('transcriptPreview').textContent='Transcript preview: '+v;}
async function ask(){const q=document.getElementById('question').value.trim();if(!q){return;}document.getElementById('aiLoading').textContent='AI status: thinking...';pushMsg('You',q);const res=await fetch('/ai/mechanic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,source:'user'})});const data=await res.json();lastAnswer=data.answer;document.getElementById('aiResponse').textContent=data.answer;document.getElementById('aiLoading').textContent='AI status: response ready';if(data.visualization_hook){await fetch('/vehicle-visualization/highlight',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data.visualization_hook,source:'ai_mechanic'})});}pushMsg('AI Mechanic',data.answer+` [source: ${data.response_basis}]`);}
function speechAvailable(){return 'webkitSpeechRecognition' in window || 'SpeechRecognition' in window;}
function initSpeech(){const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){document.getElementById('voiceState').textContent='Voice state: unavailable, text fallback active';return;}recog=new SR();recog.lang='en-US';recog.interimResults=true;recog.onstart=()=>document.getElementById('voiceState').textContent='Voice state: listening';recog.onerror=()=>document.getElementById('voiceState').textContent='Voice state: transcription failed, retry or type';recog.onend=()=>document.getElementById('voiceState').textContent='Voice state: idle';recog.onresult=(e)=>{let t='';for(let i=e.resultIndex;i<e.results.length;i++){t+=e.results[i][0].transcript;}document.getElementById('question').value=t.trim();document.getElementById('transcriptPreview').textContent='Transcript preview: '+(t.trim()||'(none)');};}
function startListening(){if(!recog){const qp=new URLSearchParams(window.location.search).get('prompt'); if(qp){setPrompt(qp);} initSpeech();}if(recog){recog.start();}}
function stopListening(){if(recog){recog.stop();}}
function playAnswer(){if(!lastAnswer){return;}if(!synth){pushMsg('System','Voice output unavailable, using text transcript only.');return;}const u=new SpeechSynthesisUtterance(lastAnswer);synth.speak(u);} 
function stopAnswer(){if(synth){synth.cancel();}}

document.getElementById('sendBtn').onclick=ask;
document.getElementById('micBtn').onclick=startListening;
document.getElementById('stopMicBtn').onclick=stopListening;
document.getElementById('speakBtn').onclick=playAnswer;
document.getElementById('stopSpeakBtn').onclick=stopAnswer;
document.getElementById('backBtn').onclick=()=>window.location.href='/dashboard';
const qp=new URLSearchParams(window.location.search).get('prompt'); if(qp){setPrompt(qp);} initSpeech();
</script>
</body>
</html>
    """
