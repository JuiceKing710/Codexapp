from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
)
from sienna_diag.obd.adapter import OBDLinkAdapter
from sienna_diag.preprocessing.pipeline import run_preprocessing
from sienna_diag.review.lmstudio import local_llama_review
from sienna_diag.review.openai_review import openai_second_opinion
from sienna_diag.safety_policy import SafetyPolicy
from sienna_diag.session_store import store
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
    if store.active_session_id and payload.status == "connected":
        store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=store.active_session_id,
            event_type="vehicle_connected",
            title="Vehicle connected",
            detail=f"{payload.adapter_name} connected through hybrid mobile app.",
        ))
    return phone_bridge_state


@app.post("/phone/bridge/disconnect")
def phone_bridge_disconnect() -> dict:
    _touch_phone_bridge(status="disconnected", error=None)
    phone_bridge_state["backend_status"] = "phone-managed"
    phone_bridge_state["polling_state"] = "inactive"
    if store.active_session_id:
        store.add_timeline_event(DiagnosticTimelineEvent(
            session_id=store.active_session_id,
            event_type="vehicle_disconnected",
            title="Vehicle disconnected",
            detail="Phone-managed Bluetooth disconnected.",
        ))
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


def _vehicle_intelligence_core_snapshot(active: dict | None, reads: list[dict], events: list[dict], alerts: list[dict]) -> dict:
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
            "reconnect_state": phone_bridge_state.get("status"),
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
    core_snapshot = _vehicle_intelligence_core_snapshot(active, reads, events, alerts)

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
        "vehicle_image": _resolve_vehicle_image(active.get("vehicle_id") if active else None),
        "ai_alerts": alerts,
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


def _build_ai_context(active: dict | None, reads: list[dict], events: list[dict], memory: dict) -> dict:
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
        "current_mode": phone_bridge_state.get("source_mode"),
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
    }


def _run_proactive_monitoring(session_id: str, vehicle_id: str, context: dict) -> list[dict]:
    alerts: list[AIAlertRecord] = []
    gauge = context.get("current_4_gauge_values", {})

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

    for alert in alerts:
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
    return [a.model_dump() for a in alerts]


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
        }

    reads = [item.model_dump() for item in store.get_reads(active["session_id"])]
    events = [item.model_dump() for item in store.get_events(active["session_id"])]

    vehicle_id = active.get("vehicle_id")
    memory = store.get_vehicle_memory(vehicle_id)
    if active.get("vin"):
        memory_updates.setdefault("vin", active["vin"])
    if memory_updates:
        memory = store.update_vehicle_memory(vehicle_id, memory_updates)

    context = _build_ai_context(active=active, reads=reads, events=events, memory=memory)
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
    .vehicle-image { border:1px solid var(--line); border-radius:12px; min-height:220px; background:linear-gradient(135deg,#f8fafc,#e2e8f0); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; padding:10px; }
    .ai-quick { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    .ai-quick button { width:auto; min-height:40px; padding:10px 12px; font-size:.9rem; }
    @media (max-width:640px) { .btn-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0;">Zeb’s OBD AI — Vehicle Dashboard</h1>
      <div class="tiny">Simple phone-first workflow for safe read-only diagnostics. AI Mechanic is advisory only: no control commands, no actuations, no code clearing.</div>
    </div>

    <div class="row" id="statusCards"></div>

    <div class="card">
      <h2 class="title">Main Controls</h2>
      <label class="tiny" for="vehicleSelect">Current Vehicle</label>
      <select id="vehicleSelect"></select>
      <h2 class="title" style="margin-top:14px;">Current Vehicle Image</h2>
      <div class="vehicle-image">
        <img id="vehicleImageAsset" alt="Current vehicle image" style="width:100%;max-width:560px;border-radius:10px;border:1px solid var(--line);" />
        <div id="vehicleImageLabel" style="font-weight:700;">Vehicle placeholder</div>
        <div class="tiny" id="vehicleImageSource">Loading vehicle source…</div>
      </div>
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

    <div class="card" id="bluetoothCard">
      <h2 class="title">OBDLINK BLUETOOTH</h2>
      <div class="status-value" id="btStatusText">Disconnected</div>
      <div class="tiny" id="btError">None</div>
    </div>

    <div class="card">
      <h2 class="title">AI Alerts (Proactive)</h2>
      <div class="tiny" id="aiAlertSummary">No active alerts.</div>
      <div id="aiAlertList" class="tiny" style="margin-top:8px;display:grid;gap:6px"></div>
    </div>

    <div class="card">
      <h2 class="title">Diagnostic Timeline</h2>
      <div id="timelineList" class="tiny" style="display:grid;gap:6px"></div>
    </div>

    <div class="card">
      <h2 class="title">AI Mechanic Quick Prompts</h2>
      <div class="ai-quick">
        <button class="secondary" onclick="openAiWithPrompt('What does this code mean?')">What does this code mean?</button>
        <button class="secondary" onclick="openAiWithPrompt('Is it safe to drive?')">Is it safe to drive?</button>
        <button class="secondary" onclick="openAiWithPrompt('What should I test next?')">What should I test next?</button>
        <button class="secondary" onclick="openAiWithPrompt('Explain this in simple language')">Explain this in simple language</button>
        <button class="secondary" onclick="openAiWithPrompt('What changed in live data?')">What changed in live data?</button>
        <button class="secondary" onclick="openAiWithPrompt('What should I watch right now?')">What should I watch right now?</button>
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
  renderStatusCards(); renderVehicles(); renderBluetoothCard(); renderAiAlerts(); renderTimeline();
}
function renderStatusCards(){
  const active = state.active_session;
  document.getElementById('statusCards').innerHTML = [
    card('Current mode', state.adapter_mode || 'MOCK'),
    card('Bluetooth state', phoneBridge.status || 'disconnected'),
    card('Active vehicle', active ? active.vehicle : selectedVehicleId),
    card('Active Vehicle Check', active ? active.session_id : 'None'),
    card('AI Alerts', (state.ai_alerts||[]).length ? `${state.ai_alerts.length} active` : 'None')
  ].join('');
}
function renderVehicles(){
  const select = document.getElementById('vehicleSelect'); select.innerHTML='';
  state.vehicles.forEach(v => {
    const opt=document.createElement('option'); opt.value=v.vehicle_id; opt.textContent=`${v.label} (${v.protocol_hint})`;
    if (v.vehicle_id===selectedVehicleId) opt.selected=true; select.appendChild(opt);
  });
  select.onchange=async (e)=>{ selectedVehicleId=e.target.value; await updateVehicleImage(); };
  updateVehicleImage();
}
async function updateVehicleImage(){
  const res = await fetch(`/vehicle-image/current?manual_vehicle_id=${encodeURIComponent(selectedVehicleId)}`);
  const image = await res.json();
  const trim = image.trim ? ` ${image.trim}` : '';
  const label = `${image.year || ''} ${image.make} ${image.model}${trim}`.replace(/\s+/g,' ').trim();
  document.getElementById('vehicleImageLabel').textContent = label || 'Generic vehicle';
  const sourceMap = {
    auto_vin: 'Auto VIN detection result',
    manual_selection: 'Manual vehicle selection fallback',
    active_session_vehicle: 'Active session vehicle fallback',
    closest_supported: 'Closest supported vehicle image fallback',
    generic_placeholder: 'Generic placeholder image'
  };
  document.getElementById('vehicleImageSource').textContent = sourceMap[image.resolved_from] || image.resolved_from;
  const img = document.getElementById('vehicleImageAsset');
  img.src = image.image_asset_path || image.fallback_image_asset_path;
  img.onerror = () => { img.src = image.fallback_image_asset_path; };
}
function renderBluetoothCard(){
  const status = phoneBridge.status || 'disconnected';
  document.getElementById('btStatusText').textContent = status.charAt(0).toUpperCase() + status.slice(1);
  document.getElementById('btError').textContent = phoneBridge.last_error || 'No Bluetooth errors';
}

function renderAiAlerts(){
  const alerts=state.ai_alerts||[];
  document.getElementById('aiAlertSummary').textContent=alerts.length?`${alerts.length} proactive alert(s) linked to this vehicle check.`:'No active alerts.';
  document.getElementById('aiAlertList').innerHTML=alerts.slice(-5).reverse().map(a=>`<div><b>${a.title}</b> (${a.confidence}) — ${a.explanation}. Next: ${a.suggested_next_step}</div>`).join('') || '<div>Waiting for live monitoring.</div>';
}
function renderTimeline(){
  const timeline=state.diagnostic_timeline||[];
  document.getElementById('timelineList').innerHTML=timeline.slice(-8).reverse().map(t=>`<div>${new Date(t.ts).toLocaleTimeString()} — ${t.title}${t.linked_ai_response_id?` <a href="/dashboard/ai" style="color:#0f766e">AI explanation</a>`:''}</div>`).join('') || '<div>No timeline events yet.</div>';
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

function renderAiAlerts(){
  const alerts=state.ai_alerts||[];
  document.getElementById('aiAlertSummary').textContent=alerts.length?`${alerts.length} proactive alert(s) linked to this vehicle check.`:'No active alerts.';
  document.getElementById('aiAlertList').innerHTML=alerts.slice(-5).reverse().map(a=>`<div><b>${a.title}</b> (${a.confidence}) — ${a.explanation}. Next: ${a.suggested_next_step}</div>`).join('') || '<div>Waiting for live monitoring.</div>';
}
function renderTimeline(){
  const timeline=state.diagnostic_timeline||[];
  document.getElementById('timelineList').innerHTML=timeline.slice(-8).reverse().map(t=>`<div>${new Date(t.ts).toLocaleTimeString()} — ${t.title}${t.linked_ai_response_id?` <a href="/dashboard/ai" style="color:#0f766e">AI explanation</a>`:''}</div>`).join('') || '<div>No timeline events yet.</div>';
}

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
async function ask(){const q=document.getElementById('question').value.trim();if(!q){return;}document.getElementById('aiLoading').textContent='AI status: thinking...';pushMsg('You',q);const res=await fetch('/ai/mechanic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,source:'user'})});const data=await res.json();lastAnswer=data.answer;document.getElementById('aiResponse').textContent=data.answer;document.getElementById('aiLoading').textContent='AI status: response ready';pushMsg('AI Mechanic',data.answer+` [source: ${data.response_basis}]`);}
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
