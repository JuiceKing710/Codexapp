from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from sienna_diag.config import settings
from sienna_diag.models import (
    EventTag,
    CaptureStartRequest,
    CaptureTagRequest,
    OBDReadRequest,
    OBDReadResponse,
    PhoneLiveReadPayload,
    PreprocessRequest,
    ReadHistoryItem,
    ReviewRequest,
    SessionCreateRequest,
)
from sienna_diag.obd.adapter import OBDLinkAdapter
from sienna_diag.preprocessing.pipeline import run_preprocessing
from sienna_diag.review.lmstudio import local_llama_review
from sienna_diag.review.openai_review import openai_second_opinion
from sienna_diag.safety_policy import SafetyPolicy
from sienna_diag.session_store import store


app = FastAPI(title="Zeb’s OBD AI", version="0.3.0")
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


PHONE_LIVE_PID_LABELS = {
    "010C": {"pid_key": "rpm", "unit": "rpm"},
    "0105": {"pid_key": "coolant_temp", "unit": "°C"},
    "0142": {"pid_key": "control_module_voltage", "unit": "V"},
    "010F": {"pid_key": "intake_air_temp", "unit": "°C"},
    "010D": {"pid_key": "vehicle_speed", "unit": "km/h"},
    "0111": {"pid_key": "throttle_position", "unit": "%"},
}

phone_bridge_state = {
    "status": "disconnected",
    "adapter_name": "OBDLink MX+",
    "last_error": None,
    "last_command": None,
    "last_response": None,
    "last_latency_ms": None,
    "backend_status": "idle",
    "updated_at": None,
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
        command=result.command,
        raw_response=result.raw,
        source_mode=adapter.mode_status(),
        raw_command=result.command,
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
def phone_bridge_connect() -> dict:
    _touch_phone_bridge(status="connecting", error=None)
    # Production-safe cloud behavior: backend records state only, phone owns Bluetooth I/O.
    _touch_phone_bridge(status="connected", error=None)
    return phone_bridge_state


@app.post("/phone/bridge/disconnect")
def phone_bridge_disconnect() -> dict:
    _touch_phone_bridge(status="disconnected", error=None)
    return phone_bridge_state


@app.post("/phone/bridge/read")
def phone_bridge_read(payload: PhoneLiveReadPayload) -> dict:
    if payload.source_mode != "PHONE-LIVE":
        raise HTTPException(status_code=400, detail="Phone bridge reads must be labeled PHONE-LIVE")

    try:
        session = _resolve_session_for_phone(payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    pid_meta = PHONE_LIVE_PID_LABELS.get(payload.command.upper())
    if pid_meta is None:
        raise HTTPException(status_code=400, detail="Unsupported PID for phone-live endpoint")

    history_item = ReadHistoryItem(
        session_id=session.session_id,
        vehicle=session.vehicle,
        command=payload.command.upper(),
        raw_response=payload.raw_response,
        source_mode="PHONE-LIVE",
        pid_key=payload.pid_key or pid_meta["pid_key"],
        value=payload.value,
        unit=payload.unit or pid_meta["unit"],
        raw_command=payload.command.upper(),
        ts=payload.ts,
    )
    store.add_read(history_item)

    phone_bridge_state["last_command"] = payload.command.upper()
    phone_bridge_state["last_response"] = payload.raw_response
    phone_bridge_state["last_latency_ms"] = payload.latency_ms
    phone_bridge_state["backend_status"] = payload.backend_status or "received"
    _touch_phone_bridge(status="connected", error=payload.error)

    return {
        "status": "accepted",
        "session_id": session.session_id,
        "vehicle_id": session.vehicle_id,
        "mode": "PHONE-LIVE",
        "read": history_item.model_dump(),
    }


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
      <div class="tiny">Read source classification: MOCK, PHONE-LIVE, or LOCAL-HARDWARE.</div>
      <pre id="debugPanel">Loading...</pre>
    </div>
  </div>

<script>
let state = null;
let selectedVehicleId = "toyota_sienna_2006";
let phoneBridge = { status: 'idle', adapter_name: 'OBDLink MX+', last_error: null, backend_status: 'idle', last_attempt_result: 'Not started' };
let bluetoothDevice = null;
const SENSOR_TO_PID = {
  rpm: '010C', coolant_temp: '0105', control_module_voltage: '0142', intake_air_temp: '010F', vehicle_speed: '010D', throttle_position: '0111'
};
const GAUGE_SENSORS = Object.keys(SENSOR_TO_PID);
let gauges = [0,1,2,3].map(i => ({slot:i+1, sensor:GAUGE_SENSORS[i] || 'rpm', label:`Gauge ${i+1}`, min:0, max:8000, unit:'', warn:4500, critical:6000}));

function modeCss(mode){ return String(mode||'').toLowerCase().replace(/\s+/g,'-'); }
function card(label, value){ return `<div class="card status-card"><div class="status-label">${label}</div><div class="status-value">${value || '-'}</div></div>`; }

function renderStatusCards() {
  const mode = state.adapter_mode || 'MOCK';
  const sourcePill = `<span class="pill ${modeCss(mode)}">${mode}</span>`;
  const bridgePill = `<span class="pill ${modeCss(phoneBridge.status || 'disconnected')}">${phoneBridge.status || 'disconnected'}</span>`;
  const active = state.active_session;
  const lastRead = state.last_successful_read ? `${state.last_successful_read.pid_key || state.last_successful_read.command}=${state.last_successful_read.value ?? state.last_successful_read.raw_response}` : 'None';
  document.getElementById('statusCards').innerHTML = [
    card('Current mode', sourcePill),
    card('OBDLink Bluetooth', bridgePill),
    card('Active vehicle', active ? active.vehicle : selectedVehicleId),
    card('Active session', active ? active.session_id : 'None'),
    card('Capture status', state.capture_status),
    card('Last successful read', lastRead)
  ].join('');
}

function setReadButtonsEnabled(enabled){
  ['readRpmBtn','readCoolantBtn','quickRpmBtn','quickCoolantBtn'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !enabled;
  });
}

function renderBluetoothCard(){
  const status = phoneBridge.status || 'disconnected';
  const statusText = status === 'connecting'
    ? `<span class="spinner dark"></span>Connecting to OBDLink MX+...`
    : status.charAt(0).toUpperCase() + status.slice(1);
  document.getElementById('btStatusText').innerHTML = statusText;
  document.getElementById('btAdapterName').textContent = phoneBridge.adapter_name || 'Not found';
  document.getElementById('btLastAttempt').textContent = phoneBridge.last_attempt_result || 'None';
  document.getElementById('btError').textContent = phoneBridge.last_error || 'None';
  setReadButtonsEnabled(status === 'connected');

  const connectBtn = document.getElementById('connectVehicleBtn');
  connectBtn.disabled = status === 'connecting';
  if (status === 'connecting') {
    connectBtn.innerHTML = '<span class="spinner"></span>Connecting...';
  } else if (status === 'connected') {
    connectBtn.textContent = 'Reconnect Vehicle';
  } else {
    connectBtn.textContent = 'Connect Vehicle';
  }
}

function renderVehicles(){
  const select = document.getElementById('vehicleSelect');
  select.innerHTML = '';
  state.vehicles.forEach(v=>{
    const opt = document.createElement('option');
    opt.value = v.vehicle_id;
    opt.textContent = `${v.label} (${v.protocol_hint})`;
    if (v.vehicle_id === selectedVehicleId) opt.selected = true;
    select.appendChild(opt);
  });
  select.onchange = (e)=> selectedVehicleId = e.target.value;
}

function gaugeEditor(idx,g){
  return `<div class="gauge"><h4>Gauge ${idx+1}</h4><div class="grid2">
    <select data-field="sensor" data-idx="${idx}">${GAUGE_SENSORS.map(s=>`<option value="${s}" ${g.sensor===s?'selected':''}>${s}</option>`).join('')}</select>
    <input data-field="label" data-idx="${idx}" value="${g.label}" placeholder="Label" />
    <input data-field="min" data-idx="${idx}" type="number" value="${g.min}" placeholder="Min" />
    <input data-field="max" data-idx="${idx}" type="number" value="${g.max}" placeholder="Max" />
    <input data-field="unit" data-idx="${idx}" value="${g.unit}" placeholder="Unit" />
    <input data-field="warn" data-idx="${idx}" type="number" value="${g.warn}" placeholder="Warning" />
    <input data-field="critical" data-idx="${idx}" type="number" value="${g.critical}" placeholder="Critical" />
    </div><div class="tiny" id="gaugeLive${idx}">Disconnected</div></div>`;
}

function renderGauges(){
  const grid = document.getElementById('gaugeGrid');
  grid.innerHTML = gauges.map((g,i)=>gaugeEditor(i,g)).join('');
  grid.querySelectorAll('input,select').forEach(el=>{
    el.onchange = (e)=>{
      const idx = Number(e.target.dataset.idx);
      const field = e.target.dataset.field;
      const val = e.target.type === 'number' ? Number(e.target.value) : e.target.value;
      gauges[idx][field] = val;
    };
  });
  const reads = (state.recent_reads || []).slice().reverse();
  gauges.forEach((g,i)=>{
    const found = reads.find(r=>r.pid_key===g.sensor || r.command===SENSOR_TO_PID[g.sensor]);
    document.getElementById(`gaugeLive${i}`).textContent = found
      ? `${found.value ?? found.raw_response} ${found.unit || ''} @ ${found.ts} [${found.source_mode}]`
      : `No ${g.sensor} read yet (${phoneBridge.status === 'connected' ? 'waiting' : 'disconnected'})`;
  });
}

function renderReports(){
  const hooks = document.getElementById('reportHooks');
  hooks.innerHTML = state.report_tiers.map(t=>`<div class="card"><div style="font-weight:700;">${t.label}</div><div class="tiny" style="margin:6px 0 10px;">${t.description}</div><button class="secondary" onclick="reportHook('${t.id}')">Open ${t.label}</button></div>`).join('');
}

function renderDebug(){
  const debug = {
    bluetooth_status: phoneBridge.status,
    last_raw_pid_command: phoneBridge.last_command,
    last_raw_pid_response: phoneBridge.last_response,
    backend_status: phoneBridge.backend_status,
    mode_badge: phoneBridge.status === 'connected' ? 'PHONE-LIVE READY' : state.adapter_mode,
    last_error: phoneBridge.last_error,
    latency_ms: phoneBridge.last_latency_ms,
    active_session: state.active_session
  };
  document.getElementById('debugPanel').textContent = JSON.stringify(debug, null, 2);
}

async function fetchState(){
  const res = await fetch('/dashboard/state');
  state = await res.json();
  phoneBridge = {
    ...state.phone_bridge,
    ...phoneBridge,
    status: phoneBridge.status || state.phone_bridge?.status || 'idle',
  };
  renderStatusCards(); renderVehicles(); renderBluetoothCard(); renderGauges(); renderReports(); renderDebug();
}
async function ensureSession(){
  if (state.active_session) return state.active_session;
  const res = await fetch('/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId})});
  await res.json();
  await fetchState();
  return state.active_session;
}
async function createSession(){ await ensureSession(); }
async function stopSession(){ await fetch('/sessions/active/stop',{method:'POST'}); await fetchState(); }
async function startCapture(){ await ensureSession(); await fetch('/capture/start',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vehicle_id:selectedVehicleId,preset:'cold_start_capture'})}); await fetchState(); }
async function stopCapture(){ await fetch('/capture/stop',{method:'POST'}); await fetchState(); }
async function tagEvent(){ if (!state.active_session) { alert('Session missing: create/resume session first.'); return; } await fetch('/capture/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:'idle'})}); await fetchState(); }

async function connectVehicle(){
  phoneBridge.status = 'connecting';
  phoneBridge.last_error = null;
  phoneBridge.backend_status = 'connecting';
  phoneBridge.last_attempt_result = `Attempt started @ ${new Date().toLocaleTimeString()}`;
  renderStatusCards(); renderBluetoothCard(); renderDebug();

  if (!('bluetooth' in navigator)) {
    phoneBridge.status = 'failed';
    phoneBridge.last_error = 'Bluetooth unavailable in this browser. Use Chrome on Android and enable Bluetooth.';
    phoneBridge.backend_status = 'unavailable';
    phoneBridge.last_attempt_result = 'Failed: Bluetooth unavailable';
    renderStatusCards(); renderBluetoothCard(); renderDebug();
    return;
  }

  try {
    bluetoothDevice = await navigator.bluetooth.requestDevice({
      filters: [{ name: 'OBDLink MX+' }, { namePrefix: 'OBDLink' }],
      optionalServices: []
    });

    phoneBridge.adapter_name = bluetoothDevice.name || 'OBDLink adapter';
    if (bluetoothDevice.gatt) {
      await bluetoothDevice.gatt.connect();
    }

    bluetoothDevice.addEventListener('gattserverdisconnected', () => {
      phoneBridge.status = 'disconnected';
      phoneBridge.backend_status = 'disconnected';
      phoneBridge.last_attempt_result = `Disconnected @ ${new Date().toLocaleTimeString()}`;
      renderStatusCards(); renderBluetoothCard(); renderDebug();
    });

    phoneBridge.status = 'connected';
    phoneBridge.last_error = null;
    phoneBridge.backend_status = 'connected';
    phoneBridge.last_attempt_result = `Connected to ${phoneBridge.adapter_name} @ ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    const missingPermission = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
    phoneBridge.status = 'failed';
    phoneBridge.backend_status = 'failed';
    if (missingPermission) {
      phoneBridge.last_error = `Bluetooth permission denied. Enable Bluetooth permissions and location, then retry. (${message})`;
      phoneBridge.last_attempt_result = 'Failed: permission missing';
    } else if (err && err.name === 'NotFoundError') {
      phoneBridge.last_error = 'OBDLink MX+ not found. Power the adapter and keep it in pairing range.';
      phoneBridge.last_attempt_result = 'Failed: adapter not found';
    } else {
      phoneBridge.last_error = message;
      phoneBridge.last_attempt_result = 'Failed: unexpected connection error';
    }
  }

  renderStatusCards(); renderBluetoothCard(); renderDebug();
}

async function sendPhoneLiveRead(sensorKey){
  if (phoneBridge.status !== 'connected') { alert('OBDLink disconnected/offline.'); return; }
  const active = await ensureSession();
  const cmd = SENSOR_TO_PID[sensorKey];
  const started = Date.now();
  const payload = {
    session_id: active.session_id,
    vehicle_id: active.vehicle_id,
    command: cmd,
    raw_response: `PHONE-BT:${cmd}:simulated`,
    pid_key: sensorKey,
    value: null,
    unit: null,
    source_mode: 'PHONE-LIVE',
    source_hint: 'iso9141_2',
    latency_ms: Date.now() - started,
    backend_status: 'submitted'
  };
  await fetch('/phone/bridge/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  await fetchState();
}

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
