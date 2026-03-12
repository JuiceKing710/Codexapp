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
    :root { --bg:#edf2f7; --card:#fff; --ink:#0f172a; --muted:#475569; --line:#d4dde7; --primary:#0f766e; --primary-dark:#0b5a55; --danger:#c2410c; --warn:#b45309; --ok:#15803d; }
    * { box-sizing: border-box; }
    body { margin:0; font-family:"Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--ink); }
    .wrap { max-width: 1024px; margin:0 auto; padding:14px; display:grid; gap:12px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; }
    .row { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
    .title { margin:0 0 10px; font-size:1.05rem; }
    .status-card { border-left:5px solid var(--primary); }
    .status-label { color:var(--muted); font-size:.82rem; text-transform:uppercase; letter-spacing:.02em; }
    .status-value { font-size:1rem; font-weight:700; margin-top:4px; word-break:break-word; }
    .btn-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    button, select, input { width:100%; border-radius:12px; border:1px solid var(--line); padding:14px; font-size:1rem; }
    button { background:var(--primary); border-color:var(--primary); color:#fff; font-weight:700; min-height:56px; }
    button:active { transform:scale(0.99); }
    button:disabled { opacity:0.5; cursor:not-allowed; transform:none; }
    button.secondary { background:#fff; color:var(--ink); border-color:var(--line); }
    button.danger { background:var(--danger); border-color:var(--danger); }
    .pill { display:inline-block; border-radius:999px; padding:6px 10px; font-size:.8rem; font-weight:800; color:#fff; }
    .pill.mock { background:#64748b; } .pill.phone-live { background:#2563eb; } .pill.local-hardware { background:#15803d; }
    .pill.connected { background:var(--ok); }
    .pill.connecting { background:#2563eb; }
    .pill.failed { background:var(--danger); }
    .pill.disconnected { background:#64748b; }
    .pill.idle { background:#64748b; }
    .spinner { display:inline-block; width:14px; height:14px; border:2px solid rgba(255,255,255,0.5); border-top-color:#fff; border-radius:50%; animation:spin 0.8s linear infinite; margin-right:8px; vertical-align:-2px; }
    .spinner.dark { border-color:rgba(15,23,42,0.2); border-top-color:#0f172a; }
    .connection-meta { display:grid; gap:6px; margin-top:10px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .tiny { font-size:.8rem; color:var(--muted); }
    .gauge-grid { display:grid; gap:10px; grid-template-columns:1fr; }
    .gauge { border:1px solid var(--line); border-radius:12px; padding:10px; background:#fcfdff; }
    .gauge h4 { margin:0 0 8px; }
    .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    pre { margin:0; background:#0b1220; color:#dbeafe; border-radius:10px; padding:10px; overflow:auto; font-size:.78rem; max-height:220px; }
    @media (max-width: 640px) { .btn-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0;">Zeb’s OBD AI — Phase 1 Dashboard</h1>
      <div class="tiny">Phone-first workflow for safe read-only diagnostics. Mock/live source is always visible.</div>
    </div>

    <div class="row" id="statusCards"></div>

    <div class="card">
      <h2 class="title">Main Controls</h2>
      <label class="tiny" for="vehicleSelect">Active vehicle profile</label>
      <select id="vehicleSelect"></select>
      <div class="btn-grid" style="margin-top:10px;">
        <button id="connectVehicleBtn" class="secondary">Connect Vehicle</button>
        <button id="startSessionBtn">Start Session</button>
        <button id="stopSessionBtn" class="danger">Stop Session</button>
        <button id="readRpmBtn">Read RPM</button>
        <button id="readCoolantBtn">Read Coolant Temp</button>
        <button id="startCaptureBtn">Start Capture</button>
        <button id="stopCaptureBtn" class="danger">Stop Capture</button>
        <button id="tagEventBtn" class="secondary">Tag Event</button>
        <button id="reportsBtn" class="secondary">Reports</button>
      </div>
      <div class="btn-grid" style="margin-top:10px;">
        <button id="quickRpmBtn">Quick RPM</button>
        <button id="quickCoolantBtn">Quick Coolant Temp</button>
      </div>
    </div>

    <div class="card" id="bluetoothCard">
      <h2 class="title">OBDLINK BLUETOOTH</h2>
      <div class="status-value" id="btStatusText">Disconnected</div>
      <div class="connection-meta tiny">
        <div><strong>Adapter:</strong> <span id="btAdapterName">-</span></div>
        <div><strong>Last attempt:</strong> <span id="btLastAttempt">None</span></div>
        <div><strong>Error:</strong> <span id="btError">None</span></div>
      </div>
    </div>

    <div class="card" id="gaugePage">
      <h2 class="title">4-Gauge Presets</h2>
      <div class="grid2">
        <input id="presetName" placeholder="Preset name" value="Default" />
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          <button id="savePresetBtn" class="secondary">Save Preset</button>
          <button id="loadPresetBtn" class="secondary">Load Preset</button>
        </div>
      </div>
      <div class="tiny" style="margin-top:8px;">Each gauge supports sensor assignment, label, range, unit, warning and critical thresholds.</div>
      <div class="gauge-grid" id="gaugeGrid" style="margin-top:10px;"></div>
    </div>

    <div class="card">
      <h2 class="title">Reports Framework (Phase 1 Hooks)</h2>
      <div id="reportHooks" class="row"></div>
    </div>

    <div class="card">
      <h2 class="title">Developer / Debug Panel</h2>
      <div class="tiny">Read source classification: MOCK, PHONE-LIVE, LOCAL-HARDWARE, or BROWSER-DEV. Connection states: disconnected, connecting, connected, failed.</div>
      <pre id="debugPanel">Loading...</pre>
    </div>
  </div>

<script>
let state = null;
let selectedVehicleId = "toyota_sienna_2006";
let pollingHandle = null;
let phoneBridge = { status: 'disconnected', adapter_name: 'OBDLink MX+', last_error: null, backend_status: 'idle', source_mode: 'PHONE-LIVE', polling_state: 'inactive', polling_interval_ms: 500 };
const SENSOR_TO_PID = { rpm:'010C', coolant_temp:'0105', control_module_voltage:'0142', intake_air_temp:'010F', vehicle_speed:'010D', throttle_position:'0111', vin:'0902' };
const GAUGE_SENSORS = ['rpm','coolant_temp','control_module_voltage','vehicle_speed','throttle_position','intake_air_temp'];
let gauges = [0,1,2,3].map(i => ({slot:i+1, sensor:GAUGE_SENSORS[i] || 'rpm', label:`Gauge ${i+1}`, min:0, max:8000, unit:'', warn:4500, critical:6000}));

const MobileBluetoothService = {
  async connectToAdapter() {
    const payload = { platform: detectPlatform(), adapter_name: 'OBDLink MX+', status: 'connected', permission_state: detectPlatform().startsWith('i') ? 'ios-managed' : 'android-managed', source_mode: 'PHONE-LIVE', supports_native_bluetooth: true };
    await fetch('/phone/bridge/connect', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    await fetchState();
    startGaugePolling();
  },
  async disconnectAdapter() { await fetch('/phone/bridge/disconnect', { method:'POST' }); stopGaugePolling(); await fetchState(); },
  async readPid(pid, pidKey, polling=false) {
    if ((phoneBridge.status || 'disconnected') !== 'connected') throw new Error('OBDLink disconnected/offline.');
    const active = await ensureSession();
    const started = Date.now();
    const payload = { session_id:active.session_id, vehicle_id:active.vehicle_id, command:pid, raw_response:`PHONE-NATIVE:${pid}:awaiting-native-payload`, pid_key:pidKey, value:null, unit:null, source_mode:'PHONE-LIVE', source_hint:'iso9141_2', latency_ms:Date.now()-started, backend_status:'submitted-from-phone-ui', polling };
    await fetch('/phone/bridge/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  },
  async readVin() { await this.readPid('0902', 'vin', false); },
  getConnectionState() { return phoneBridge.status || 'disconnected'; },
};

function detectPlatform(){ const ua = navigator.userAgent || ''; if (/iPad/i.test(ua)) return 'ipad'; if (/iPhone|iPod/i.test(ua)) return 'ios'; if (/Android/i.test(ua)) return 'android'; return 'unknown'; }
function modeCss(mode){ return String(mode||'').toLowerCase().replace(/\s+/g,'-'); }
function card(label, value){ return `<div class="card status-card"><div class="status-label">${label}</div><div class="status-value">${value || '-'}</div></div>`; }
function renderStatusCards(){
  const mode = state.adapter_mode || 'MOCK';
  const bridgeState = phoneBridge.status || 'disconnected';
  const active = state.active_session;
  const lastRead = state.last_successful_read ? `${state.last_successful_read.pid_key || state.last_successful_read.command}=${state.last_successful_read.value ?? state.last_successful_read.raw_response}` : 'None';
  document.getElementById('statusCards').innerHTML = [
    card('Current mode', `<span class="pill ${modeCss(mode)}">${mode}</span>`),
    card('Bluetooth state', `<span class="pill ${modeCss(bridgeState)}">${bridgeState}</span>`),
    card('Polling', `${phoneBridge.polling_state} @ ${phoneBridge.polling_interval_ms}ms`),
    card('Source badge', `<span class="pill ${modeCss(phoneBridge.source_mode || 'mock')}">${phoneBridge.source_mode || 'MOCK'}</span>`),
    card('Active vehicle', active ? active.vehicle : selectedVehicleId),
    card('Active session', active ? active.session_id : 'None'),
    card('Last successful read', lastRead)
  ].join('');
}
function setReadButtonsEnabled(enabled){ ['readRpmBtn','readCoolantBtn','quickRpmBtn','quickCoolantBtn'].forEach(id => { const btn = document.getElementById(id); if (btn) btn.disabled = !enabled; }); }
function renderBluetoothCard(){ const status = phoneBridge.status || 'disconnected'; document.getElementById('btStatusText').textContent = status.charAt(0).toUpperCase() + status.slice(1); document.getElementById('btAdapterName').textContent = phoneBridge.adapter_name || 'Not found'; document.getElementById('btLastAttempt').textContent = phoneBridge.updated_at || 'None'; document.getElementById('btError').textContent = phoneBridge.last_error || 'None'; setReadButtonsEnabled(status === 'connected'); document.getElementById('connectVehicleBtn').textContent = status === 'connected' ? 'Reconnect Vehicle' : 'Connect Vehicle'; }
function renderVehicles(){ const select = document.getElementById('vehicleSelect'); select.innerHTML=''; state.vehicles.forEach(v=>{ const opt=document.createElement('option'); opt.value=v.vehicle_id; opt.textContent=`${v.label} (${v.protocol_hint})`; if(v.vehicle_id===selectedVehicleId) opt.selected=true; select.appendChild(opt); }); select.onchange=(e)=>selectedVehicleId=e.target.value; }
function gaugeEditor(idx,g){ return `<div class="gauge"><h4>${g.label}</h4><div class="grid2"><select data-field="sensor" data-idx="${idx}">${GAUGE_SENSORS.map(s=>`<option value="${s}" ${g.sensor===s?'selected':''}>${s}</option>`).join('')}</select><input data-field="label" data-idx="${idx}" value="${g.label}" placeholder="Label" /><input data-field="min" data-idx="${idx}" type="number" value="${g.min}" placeholder="Min" /><input data-field="max" data-idx="${idx}" type="number" value="${g.max}" placeholder="Max" /><input data-field="unit" data-idx="${idx}" value="${g.unit}" placeholder="Unit" /><input data-field="warn" data-idx="${idx}" type="number" value="${g.warn}" placeholder="Warning" /><input data-field="critical" data-idx="${idx}" type="number" value="${g.critical}" placeholder="Critical" /></div><div class="tiny" id="gaugeLive${idx}">Disconnected</div></div>`; }
function renderGauges(){ const grid=document.getElementById('gaugeGrid'); grid.innerHTML=gauges.map((g,i)=>gaugeEditor(i,g)).join(''); grid.querySelectorAll('input,select').forEach(el=>{el.onchange=(e)=>{const idx=Number(e.target.dataset.idx); const field=e.target.dataset.field; const val=e.target.type==='number'?Number(e.target.value):e.target.value; gauges[idx][field]=val;};}); const reads=(state.recent_reads||[]).slice().reverse(); gauges.forEach((g,i)=>{ const found=reads.find(r=>r.pid_key===g.sensor||r.command===SENSOR_TO_PID[g.sensor]); document.getElementById(`gaugeLive${i}`).textContent=found?`${g.label}: ${found.value ?? found.raw_response} ${g.unit || found.unit || ''} [${found.source_mode}]`:`${g.label}: ${phoneBridge.status === 'connected' ? 'Waiting for 500ms polling' : 'Disconnected'}`; }); }
function renderReports(){ document.getElementById('reportHooks').innerHTML = state.report_tiers.map(t=>`<div class="card"><div style="font-weight:700;">${t.label}</div><div class="tiny" style="margin:6px 0 10px;">${t.description}</div><button class="secondary" onclick="reportHook('${t.id}')">Open ${t.label}</button></div>`).join(''); }
function renderDebug(){ document.getElementById('debugPanel').textContent = JSON.stringify({ platform:phoneBridge.platform, bluetooth_connection_state:phoneBridge.status, polling_active:phoneBridge.polling_state, last_pid_command:phoneBridge.last_command, last_pid_response:phoneBridge.last_response, last_vin_command:phoneBridge.last_vin_command, vin_parse_status:phoneBridge.vin_parse_status, mode_badge:phoneBridge.source_mode || (phoneBridge.status === 'connected' ? 'PHONE-LIVE' : 'MOCK'), last_error:phoneBridge.last_error, latency_ms:phoneBridge.last_latency_ms, last_replayed_command:phoneBridge.last_replayed_command, command_learning_status:phoneBridge.command_learning_status, passive_can_capture_status:phoneBridge.passive_can_capture_status }, null, 2); }
async function fetchState(){ const res = await fetch('/dashboard/state'); state = await res.json(); phoneBridge = { ...phoneBridge, ...state.phone_bridge }; renderStatusCards(); renderVehicles(); renderBluetoothCard(); renderGauges(); renderReports(); renderDebug(); }
async function ensureSession(){ if (state && state.active_session) return state.active_session; await fetch('/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId})}); await fetchState(); return state.active_session; }
async function createSession(){ await ensureSession(); }
async function stopSession(){ stopGaugePolling(); await fetch('/sessions/active/stop',{method:'POST'}); await fetchState(); }
async function startCapture(){ await ensureSession(); await fetch('/capture/start',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId,preset:'cold_start_capture'})}); await fetchState(); }
async function stopCapture(){ await fetch('/capture/stop',{method:'POST'}); await fetchState(); }
async function tagEvent(){ if (!state.active_session) { alert('Session missing: create/resume session first.'); return; } await fetch('/capture/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:'idle'})}); await fetchState(); }
async function connectVehicle(){ try { await MobileBluetoothService.connectToAdapter(); await MobileBluetoothService.readVin(); await fetchState(); } catch (err) { phoneBridge.status='failed'; phoneBridge.last_error = err && err.message ? err.message : String(err); renderBluetoothCard(); renderDebug(); } }
async function sendPhoneLiveRead(sensorKey){ try { await MobileBluetoothService.readPid(SENSOR_TO_PID[sensorKey], sensorKey, false); await fetchState(); } catch(err) { alert(err.message || String(err)); } }
async function pollAllGaugesOnce(){ if ((phoneBridge.status || 'disconnected') !== 'connected') { stopGaugePolling(); return; } const jobs = gauges.map(g => MobileBluetoothService.readPid(SENSOR_TO_PID[g.sensor], g.sensor, true)); await Promise.allSettled(jobs); await fetchState(); }
function startGaugePolling(){ if (pollingHandle) return; phoneBridge.polling_state='active'; pollingHandle = setInterval(() => { pollAllGaugesOnce(); }, 500); }
function stopGaugePolling(){ if (pollingHandle) { clearInterval(pollingHandle); pollingHandle = null; } phoneBridge.polling_state='inactive'; }
function reportHook(id){ alert(`Phase 2 hook remains: ${id}`); }
function savePreset(){ const name=document.getElementById('presetName').value||'Default'; localStorage.setItem(`gaugePreset:${name}`, JSON.stringify(gauges)); }
function loadPreset(){ const name=document.getElementById('presetName').value||'Default'; const raw=localStorage.getItem(`gaugePreset:${name}`); if(raw){gauges=JSON.parse(raw); renderGauges();} }
document.getElementById('connectVehicleBtn').onclick = connectVehicle;
document.getElementById('startSessionBtn').onclick = createSession;
document.getElementById('stopSessionBtn').onclick = stopSession;
document.getElementById('readRpmBtn').onclick = () => sendPhoneLiveRead('rpm');
document.getElementById('readCoolantBtn').onclick = () => sendPhoneLiveRead('coolant_temp');
document.getElementById('startCaptureBtn').onclick = startCapture;
document.getElementById('stopCaptureBtn').onclick = stopCapture;
document.getElementById('tagEventBtn').onclick = tagEvent;
document.getElementById('reportsBtn').onclick = () => document.getElementById('reportHooks').scrollIntoView({behavior:'smooth'});
document.getElementById('quickRpmBtn').onclick = () => sendPhoneLiveRead('rpm');
document.getElementById('quickCoolantBtn').onclick = () => sendPhoneLiveRead('coolant_temp');
document.getElementById('savePresetBtn').onclick = savePreset;
document.getElementById('loadPresetBtn').onclick = loadPreset;
setReadButtonsEnabled(false);
setInterval(fetchState, 1500);
fetchState();
</script>
</body>
</html>
    """
