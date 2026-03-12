from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from sienna_diag.config import settings
from sienna_diag.models import (
    CommandLearningIngestRequest,
    CommandLearningRecord,
    EventTag,
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
)
from sienna_diag.obd.adapter import OBDLinkAdapter
from sienna_diag.preprocessing.pipeline import run_preprocessing
from sienna_diag.review.lmstudio import local_llama_review
from sienna_diag.review.openai_review import openai_second_opinion
from sienna_diag.safety_policy import SafetyPolicy
from sienna_diag.session_store import store


app = FastAPI(title="Zeb’s OBD AI", version="0.4.0")
adapter = OBDLinkAdapter()

SAFE_QUICK_READS = {
    "rpm": "010C",
    "coolant_temp": "0105",
    "vehicle_speed": "010D",
    "control_module_voltage": "0142",
    "vin": "0902",
    "stored_codes": "03",
    "pending_codes": "07",
}


LIVE_POLLING_SUPPORTED_PIDS = ["010C", "0105", "0142", "010F", "010D", "0111"]

PHONE_LIVE_PID_LABELS = {
    "010C": {"pid_key": "rpm", "unit": "rpm"},
    "0105": {"pid_key": "coolant_temp", "unit": "°C"},
    "0142": {"pid_key": "control_module_voltage", "unit": "V"},
    "010F": {"pid_key": "intake_air_temp", "unit": "°C"},
    "010D": {"pid_key": "vehicle_speed", "unit": "km/h"},
    "0111": {"pid_key": "throttle_position", "unit": "%"},
    "0902": {"pid_key": "vin", "unit": None},
}

phone_bridge_state = {
    "status": "disconnected",
    "adapter_name": "OBDLink MX+",
    "platform": "unknown",
    "permission_state": "unknown",
    "source_mode": "PHONE-LIVE",
    "supports_native_bluetooth": True,
    "fallback_reason": None,
    "last_error": None,
    "last_command": None,
    "last_response": None,
    "last_latency_ms": None,
    "backend_status": "idle",
    "last_vin_command": None,
    "last_vin_response": None,
    "vin_parse_status": "not-run",
    "vin": None,
    "updated_at": None,
    "polling_state": "inactive",
    "polling_interval_ms": 500,
    "last_replayed_command": None,
    "command_learning_status": "idle",
    "passive_can_capture_status": "not-available",
}

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

capture_lock = threading.Lock()
capture_stop_event = threading.Event()
capture_thread: threading.Thread | None = None
capture_status = "idle"
capture_preset = "cold_start_capture"


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
        return {"status": capture_status}


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "app_id": settings.app_id,
        "display_name": settings.app_display_name,
        "mode": "read-only",
        "adapter_mode": adapter.mode_status(),
        "connection_status": adapter.connection_status(),
        "phone_bridge": phone_bridge_state,
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
    return phone_bridge_state


@app.post("/phone/bridge/connect")
def phone_bridge_connect(payload: PhoneBridgeConnectPayload) -> dict:
    _touch_phone_bridge(status=payload.status, error=None)
    phone_bridge_state["platform"] = payload.platform
    phone_bridge_state["adapter_name"] = payload.adapter_name
    phone_bridge_state["permission_state"] = payload.permission_state or "unknown"
    phone_bridge_state["source_mode"] = payload.source_mode
    phone_bridge_state["supports_native_bluetooth"] = payload.supports_native_bluetooth
    phone_bridge_state["fallback_reason"] = payload.fallback_reason
    phone_bridge_state["backend_status"] = "phone-managed"
    phone_bridge_state["polling_state"] = "active" if payload.status == "connected" else "inactive"
    phone_bridge_state["command_learning_status"] = "active" if payload.status == "connected" else "idle"
    return phone_bridge_state


@app.post("/phone/bridge/disconnect")
def phone_bridge_disconnect() -> dict:
    _touch_phone_bridge(status="disconnected", error=None)
    phone_bridge_state["backend_status"] = "phone-managed"
    phone_bridge_state["polling_state"] = "inactive"
    return phone_bridge_state


@app.post("/phone/bridge/read")
def phone_bridge_read(payload: PhoneLiveReadPayload) -> dict:
    if payload.source_mode not in {"PHONE-LIVE", "BROWSER-DEV"}:
        raise HTTPException(status_code=400, detail="Phone bridge reads must be labeled PHONE-LIVE or BROWSER-DEV")

    try:
        session = _resolve_session_for_phone(payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    pid_meta = PHONE_LIVE_PID_LABELS.get(payload.command.upper())
    if pid_meta is None:
        raise HTTPException(status_code=400, detail="Unsupported PID for phone-live endpoint")

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
    phone_bridge_state["backend_status"] = payload.backend_status or "received"
    if payload.command.upper() == "0902":
        phone_bridge_state["last_vin_command"] = payload.command.upper()
        phone_bridge_state["last_vin_response"] = payload.raw_response
        parsed_vin = payload.value if isinstance(payload.value, str) and len(str(payload.value)) == 17 else _decode_mode09_vin(payload.raw_response)
        if parsed_vin:
            phone_bridge_state["vin_parse_status"] = "parsed"
            phone_bridge_state["vin"] = parsed_vin
            store.assign_session_vin(session.session_id, parsed_vin, "auto_vin")
        else:
            phone_bridge_state["vin_parse_status"] = "failed-manual-selection-required"
            store.assign_session_vin(session.session_id, None, "manual")
    _touch_phone_bridge(status="connected", error=payload.error)
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

    return {
        "status": "accepted",
        "session_id": session.session_id,
        "vehicle_id": session.vehicle_id,
        "mode": mode_label,
        "read": history_item.model_dump(),
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
    session = store.get_session(payload.session_id)
    approval = store.get_replay_approval(payload.session_id, payload.raw_command)
    if not approval or not approval.get("approved"):
        raise HTTPException(status_code=403, detail="Command replay requires manual approval")

    library_item = store.command_library.get(payload.raw_command.upper(), {})
    risk = library_item.get("approval_status", "manual-required")
    if risk == "manual-required" and not payload.confirm_risky:
        raise HTTPException(status_code=400, detail="Risky/unknown replay needs confirm_risky=true")

    replay_success = True
    record = _record_learning(
        session_id=session.session_id,
        vehicle_id=session.vehicle_id,
        source_type="replay",
        raw_command=payload.raw_command,
        raw_response="REPLAY-LOGGED-NO-ACTUATION",
        parsed_response={"note": "controlled replay log-only in read-only safety mode"},
        protocol=session.protocol,
        mode=phone_bridge_state.get("source_mode", "PHONE-LIVE"),
        before_state_snapshot=payload.before_state_snapshot,
        after_state_snapshot=payload.after_state_snapshot,
        tags=payload.tags,
        risk_classification="confirmed" if payload.confirm_risky else "safe",
        manually_approved_for_replay=True,
        replay_succeeded=replay_success,
        notes=payload.notes,
    )
    phone_bridge_state["last_replayed_command"] = payload.raw_command.upper()
    return {"status": "replay_logged", "record": record.model_dump()}

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

    return {
        "app_id": settings.app_id,
        "display_name": settings.app_display_name,
        "vehicles": [item.model_dump() for item in store.list_vehicles()],
        "active_session": active,
        "adapter_mode": adapter.mode_status(),
        "connection_status": adapter.connection_status(),
        "phone_bridge": phone_bridge_state,
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
    }


@app.post("/ai/mechanic")
def ai_mechanic(payload: dict) -> dict:
    question = str(payload.get("question") or "").strip()
    active = store.get_session(store.active_session_id).model_dump() if store.active_session_id else None
    reads = [item.model_dump() for item in store.get_reads(active["session_id"])] if active else []
    events = [item.model_dump() for item in store.get_events(active["session_id"])] if active else []
    latest_live_data = reads[-5:]
    context = {
        "current_vehicle": active.get("vehicle") if active else None,
        "vin": active.get("vin") if active else None,
        "current_mode": phone_bridge_state.get("source_mode"),
        "active_vehicle_check": active.get("session_id") if active else None,
        "dtcs": [r for r in reads if str(r.get("pid_key", "")).lower().startswith("dtc")],
        "latest_live_data": latest_live_data,
        "captured_events": events[-10:],
        "reports": ["Customer Summary", "Technician Detail", "AI Training Export"],
    }
    if not question:
        answer = "Please ask a question about your vehicle, active vehicle check, codes, live data, or next steps."
    else:
        answer = (
            f"For '{question}', I reviewed the current vehicle context. "
            f"Vehicle: {context['current_vehicle'] or 'Not selected yet'}. "
            f"VIN: {context['vin'] or 'Not available yet'}. "
            f"Active Vehicle Check: {context['active_vehicle_check'] or 'None'}. "
            f"I can help explain DTC meaning in plain English, safety-to-drive guidance, and recommended next tests "
            f"using live data and captured events from this check."
        )
    return {"answer": answer, "context": context}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Zeb’s OBD AI Dashboard</title>
  <style>
    :root { --bg:#edf2f7; --card:#fff; --ink:#0f172a; --muted:#475569; --line:#d4dde7; --primary:#0f766e; --danger:#c2410c; --ok:#15803d; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:"Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--ink); }
    .wrap { max-width:1024px; margin:0 auto; padding:14px; display:grid; gap:12px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; }
    .row { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
    .title { margin:0 0 10px; font-size:1.05rem; }
    .status-card { border-left:5px solid var(--primary); }
    .status-label { color:var(--muted); font-size:.82rem; text-transform:uppercase; letter-spacing:.02em; }
    .status-value { font-size:1rem; font-weight:700; margin-top:4px; word-break:break-word; }
    .btn-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    button, select, input { width:100%; border-radius:12px; border:1px solid var(--line); padding:14px; font-size:1rem; }
    button { background:var(--primary); border-color:var(--primary); color:#fff; font-weight:700; min-height:56px; }
    button.secondary { background:#fff; color:var(--ink); }
    button.danger { background:var(--danger); border-color:var(--danger); }
    .tiny { font-size:.8rem; color:var(--muted); }
    .vehicle-image { border:1px solid var(--line); border-radius:12px; min-height:220px; background:linear-gradient(135deg,#f8fafc,#e2e8f0); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; }
    .vehicle-image .emoji { font-size:3.2rem; }
    .ai-quick { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    .ai-quick button { width:auto; min-height:40px; padding:10px 12px; font-size:.9rem; }
    @media (max-width:640px) { .btn-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0;">Zeb’s OBD AI — Vehicle Dashboard</h1>
      <div class="tiny">Simple phone-first workflow for safe read-only diagnostics.</div>
    </div>

    <div class="row" id="statusCards"></div>

    <div class="card">
      <h2 class="title">Main Controls</h2>
      <label class="tiny" for="vehicleSelect">Current vehicle</label>
      <select id="vehicleSelect"></select>
      <div class="btn-grid" style="margin-top:10px;">
        <button id="connectVehicleBtn" class="secondary">Connect Vehicle</button>
        <button id="startSessionBtn">Start Vehicle Check</button>
        <button id="stopSessionBtn" class="danger">End Vehicle Check</button>
        <button id="startCaptureBtn">Start Capture</button>
        <button id="stopCaptureBtn" class="danger">Stop Capture</button>
        <button id="tagEventBtn" class="secondary">Tag Event</button>
        <button id="reportsBtn" class="secondary">Reports</button>
        <button id="liveGaugesBtn" class="secondary">Live Gauges</button>
        <button id="askAiBtn">Ask AI Mechanic</button>
      </div>
    </div>

    <div class="card">
      <h2 class="title">Current Vehicle Image</h2>
      <div class="vehicle-image">
        <div class="emoji">🚗</div>
        <div id="vehicleImageLabel" style="font-weight:700;">Vehicle placeholder</div>
        <div class="tiny">Placeholder area ready for real vehicle images.</div>
      </div>
    </div>

    <div class="card" id="bluetoothCard">
      <h2 class="title">OBDLINK BLUETOOTH</h2>
      <div class="status-value" id="btStatusText">Disconnected</div>
      <div class="tiny" id="btError">None</div>
    </div>

    <div class="card">
      <h2 class="title">AI Mechanic Quick Prompts</h2>
      <div class="ai-quick">
        <button class="secondary" onclick="openAiWithPrompt('What does this code mean?')">What does this code mean?</button>
        <button class="secondary" onclick="openAiWithPrompt('Is it safe to drive?')">Is it safe to drive?</button>
        <button class="secondary" onclick="openAiWithPrompt('What should I test next?')">What should I test next?</button>
        <button class="secondary" onclick="openAiWithPrompt('Explain this in simple language')">Explain this in simple language</button>
      </div>
    </div>
  </div>
<script>
let state = null;
let selectedVehicleId = 'toyota_sienna_2006';
let phoneBridge = { status: 'disconnected', source_mode: 'PHONE-LIVE' };
function card(label, value){ return `<div class="card status-card"><div class="status-label">${label}</div><div class="status-value">${value || '-'}</div></div>`; }
async function fetchState(){
  const res = await fetch('/dashboard/state');
  state = await res.json();
  phoneBridge = { ...phoneBridge, ...state.phone_bridge };
  renderStatusCards(); renderVehicles(); renderBluetoothCard();
}
function renderStatusCards(){
  const active = state.active_session;
  document.getElementById('statusCards').innerHTML = [
    card('Current mode', state.adapter_mode || 'MOCK'),
    card('Bluetooth state', phoneBridge.status || 'disconnected'),
    card('Active vehicle', active ? active.vehicle : selectedVehicleId),
    card('Active Vehicle Check', active ? active.session_id : 'None')
  ].join('');
}
function renderVehicles(){
  const select = document.getElementById('vehicleSelect'); select.innerHTML='';
  state.vehicles.forEach(v => {
    const opt=document.createElement('option'); opt.value=v.vehicle_id; opt.textContent=`${v.label} (${v.protocol_hint})`;
    if (v.vehicle_id===selectedVehicleId) opt.selected=true; select.appendChild(opt);
  });
  select.onchange=(e)=>{ selectedVehicleId=e.target.value; updateVehicleImageLabel(); };
  updateVehicleImageLabel();
}
function updateVehicleImageLabel(){
  const vehicle = state && state.vehicles ? state.vehicles.find(v => v.vehicle_id === selectedVehicleId) : null;
  document.getElementById('vehicleImageLabel').textContent = vehicle ? vehicle.label : selectedVehicleId;
}
function renderBluetoothCard(){
  const status = phoneBridge.status || 'disconnected';
  document.getElementById('btStatusText').textContent = status.charAt(0).toUpperCase() + status.slice(1);
  document.getElementById('btError').textContent = phoneBridge.last_error || 'No Bluetooth errors';
}
async function ensureSession(){ if (state && state.active_session) return state.active_session; await fetch('/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId})}); await fetchState(); return state.active_session; }
async function createSession(){ await ensureSession(); }
async function stopSession(){ await fetch('/sessions/active/stop',{method:'POST'}); await fetchState(); }
async function startCapture(){ await ensureSession(); await fetch('/capture/start',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId,preset:'cold_start_capture'})}); await fetchState(); }
async function stopCapture(){ await fetch('/capture/stop',{method:'POST'}); await fetchState(); }
async function tagEvent(){ if (!state.active_session) { alert('Vehicle Check missing: start Vehicle Check first.'); return; } await fetch('/capture/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:'idle'})}); await fetchState(); }
async function connectVehicle(){ await fetch('/phone/bridge/connect', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ platform:'unknown', adapter_name:'OBDLink MX+', status:'connected', source_mode:'PHONE-LIVE', supports_native_bluetooth:true }) }); await fetchState(); }
function openAiWithPrompt(prompt){ window.location.href = `/dashboard/ai?prompt=${encodeURIComponent(prompt || '')}`; }
document.getElementById('connectVehicleBtn').onclick = connectVehicle;
document.getElementById('startSessionBtn').onclick = createSession;
document.getElementById('stopSessionBtn').onclick = stopSession;
document.getElementById('startCaptureBtn').onclick = startCapture;
document.getElementById('stopCaptureBtn').onclick = stopCapture;
document.getElementById('tagEventBtn').onclick = tagEvent;
document.getElementById('reportsBtn').onclick = () => alert('Reports view is available in the report framework section.');
document.getElementById('liveGaugesBtn').onclick = () => window.location.href = '/dashboard/gauges';
document.getElementById('askAiBtn').onclick = () => openAiWithPrompt('');
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
let state=null; let pollingHandle=null; let phoneBridge={status:'disconnected'};
const SENSOR_TO_PID={rpm:'010C',coolant_temp:'0105',control_module_voltage:'0142',intake_air_temp:'010F',vehicle_speed:'010D',throttle_position:'0111'};
const GAUGE_SENSORS=['rpm','coolant_temp','control_module_voltage','vehicle_speed','throttle_position','intake_air_temp'];
let gauges=[0,1,2,3].map(i=>({slot:i+1,sensor:GAUGE_SENSORS[i]||'rpm',label:`Gauge ${i+1}`,min:0,max:8000,unit:'',warn:4500,critical:6000}));
function gaugeEditor(idx,g){return `<div class="gauge"><h4>${g.label}</h4><div class="grid2"><select data-field="sensor" data-idx="${idx}">${GAUGE_SENSORS.map(s=>`<option value="${s}" ${g.sensor===s?'selected':''}>${s}</option>`).join('')}</select><input data-field="label" data-idx="${idx}" value="${g.label}" placeholder="Label" /><input data-field="unit" data-idx="${idx}" value="${g.unit}" placeholder="Unit" /><input data-field="warn" data-idx="${idx}" type="number" value="${g.warn}" placeholder="Warn" /><input data-field="critical" data-idx="${idx}" type="number" value="${g.critical}" placeholder="Critical" /></div><div class="tiny" id="gaugeLive${idx}">Disconnected</div></div>`;}
function renderGauges(){const grid=document.getElementById('gaugeGrid');grid.innerHTML=gauges.map((g,i)=>gaugeEditor(i,g)).join('');grid.querySelectorAll('input,select').forEach(el=>el.onchange=(e)=>{const i=Number(e.target.dataset.idx);const f=e.target.dataset.field;gauges[i][f]=e.target.type==='number'?Number(e.target.value):e.target.value;});const reads=(state&&state.recent_reads?state.recent_reads:[]).slice().reverse();gauges.forEach((g,i)=>{const found=reads.find(r=>r.pid_key===g.sensor||r.command===SENSOR_TO_PID[g.sensor]);document.getElementById(`gaugeLive${i}`).textContent=found?`${g.label}: ${found.value ?? found.raw_response} ${g.unit || found.unit || ''} [${found.source_mode}]`:`${g.label}: ${(phoneBridge.status==='connected')?'Waiting for 500ms polling':'Disconnected'}`;});}
async function fetchState(){const res=await fetch('/dashboard/state');state=await res.json();phoneBridge={...phoneBridge,...state.phone_bridge};renderGauges();}
async function ensureSession(){if(state&&state.active_session)return state.active_session;await fetch('/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicle_id:'toyota_sienna_2006'})});await fetchState();return state.active_session;}
async function pollAllGaugesOnce(){if((phoneBridge.status||'disconnected')!=='connected'){stopGaugePolling();return;}const active=await ensureSession();const jobs=gauges.map(g=>fetch('/phone/bridge/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:active.session_id,vehicle_id:active.vehicle_id,command:SENSOR_TO_PID[g.sensor],pid_key:g.sensor,source_mode:'PHONE-LIVE',source_hint:'iso9141_2',polling:true,raw_response:'PHONE-NATIVE'})}));await Promise.allSettled(jobs);await fetchState();}
function startGaugePolling(){if(pollingHandle)return;pollingHandle=setInterval(()=>{pollAllGaugesOnce();},500);} function stopGaugePolling(){if(pollingHandle){clearInterval(pollingHandle);pollingHandle=null;}}
function savePreset(){const name=document.getElementById('presetName').value||'Default';localStorage.setItem(`gaugePreset:${name}`,JSON.stringify(gauges));}
function loadPreset(){const name=document.getElementById('presetName').value||'Default';const raw=localStorage.getItem(`gaugePreset:${name}`);if(raw){gauges=JSON.parse(raw);renderGauges();}}
document.getElementById('startPollingBtn').onclick=startGaugePolling; document.getElementById('stopPollingBtn').onclick=stopGaugePolling; document.getElementById('savePresetBtn').onclick=savePreset; document.getElementById('loadPresetBtn').onclick=loadPreset; document.getElementById('backBtn').onclick=()=>window.location.href='/dashboard';
setInterval(fetchState,1200); fetchState();
</script></body></html>
    """


@app.get('/dashboard/ai', response_class=HTMLResponse)
def ai_dashboard() -> str:
    return """
<!doctype html><html><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>Ask AI Mechanic</title>
<style>body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#edf2f7;color:#0f172a}.wrap{max-width:900px;margin:0 auto;padding:14px;display:grid;gap:12px}.card{background:#fff;border:1px solid #d4dde7;border-radius:14px;padding:14px}textarea,input,button{width:100%;border-radius:12px;border:1px solid #d4dde7;padding:12px}button{background:#0f766e;color:#fff;font-weight:700}.secondary{background:#fff;color:#0f172a}.quick{display:flex;flex-wrap:wrap;gap:8px}.quick button{width:auto}</style>
</head><body><div class="wrap"><div class="card"><h1 style="margin:0">Ask AI Mechanic</h1><div id="ctx" style="font-size:.85rem;color:#475569;margin-top:8px">Loading current vehicle context...</div></div>
<div class="card"><input id="question" placeholder="Ask about your current vehicle, DTCs, live data, or next steps" /><button id="sendBtn" style="margin-top:8px">Send</button><div class="quick" style="margin-top:8px"><button class="secondary" onclick="setPrompt('What does this code mean?')">What does this code mean?</button><button class="secondary" onclick="setPrompt('Is it safe to drive?')">Is it safe to drive?</button><button class="secondary" onclick="setPrompt('What should I test next?')">What should I test next?</button><button class="secondary" onclick="setPrompt('Explain this in simple language')">Explain this in simple language</button></div></div>
<div class="card"><h3 style="margin-top:0">AI Mechanic Answer</h3><div id="answer">Ask a question to get a context-aware answer.</div></div>
<div class="card"><button class="secondary" id="backBtn">Back to Main Dashboard</button></div></div>
<script>
function setPrompt(v){document.getElementById('question').value=v;}
async function loadContext(){const res=await fetch('/dashboard/state');const state=await res.json();const active=state.active_session;document.getElementById('ctx').textContent=`Vehicle: ${active?active.vehicle:'Not selected'} | VIN: ${active&&active.vin?active.vin:'Not available'} | Mode: ${state.adapter_mode || 'MOCK'} | Active Vehicle Check: ${active?active.session_id:'None'}`;}
async function ask(){const q=document.getElementById('question').value;const res=await fetch('/ai/mechanic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});const data=await res.json();document.getElementById('answer').textContent=data.answer;}
const params=new URLSearchParams(window.location.search);const prompt=params.get('prompt');if(prompt){setPrompt(prompt);}
document.getElementById('sendBtn').onclick=ask;document.getElementById('backBtn').onclick=()=>window.location.href='/dashboard';loadContext();
</script></body></html>
    """
