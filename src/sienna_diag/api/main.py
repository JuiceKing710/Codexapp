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


app = FastAPI(title="Zeb’s OBD AI", version="0.2.0")
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

    return {
        "app_id": settings.app_id,
        "display_name": settings.app_display_name,
        "vehicles": [item.model_dump() for item in store.list_vehicles()],
        "active_session": active,
        "adapter_mode": adapter.mode_status(),
        "connection_status": adapter.connection_status(),
        "capture_status": capture_status,
        "capture_preset": capture_preset,
        "capture_presets": CAPTURE_PRESETS,
        "event_tags": SAFE_EVENT_TAGS,
        "quick_reads": SAFE_QUICK_READS,
        "recent_reads": reads[-40:],
        "recent_events": events[-40:],
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
    :root {
      --bg: #f3f6fa;
      --card: #ffffff;
      --ink: #102235;
      --muted: #5a6c81;
      --line: #d7e1eb;
      --accent: #0f6db4;
      --accent-dark: #0b548a;
      --danger: #b42318;
      --ok: #117a3c;
      --warn: #9a6a00;
      --badge-idle: #6b7280;
      --badge-recording: #117a3c;
      --badge-stopped: #9a6a00;
    }
    body { margin: 0; font-family: "Avenir Next", "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
    .row { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .title { margin: 0 0 10px; font-size: 19px; }
    .label { color: var(--muted); font-size: 13px; margin-bottom: 6px; display: block; font-weight: 600; }
    .kv { display: grid; grid-template-columns: 150px 1fr; gap: 6px; font-size: 14px; }
    select, button { border-radius: 10px; border: 1px solid var(--line); padding: 12px 14px; font-size: 14px; }
    button { background: var(--accent); color: white; border-color: var(--accent); cursor: pointer; font-weight: 700; }
    button:hover { background: var(--accent-dark); }
    button.danger { background: var(--danger); border-color: var(--danger); }
    button.wide { width: 100%; }
    .btn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
    .badge { display: inline-block; padding: 6px 12px; border-radius: 999px; color: white; font-weight: 700; font-size: 12px; text-transform: uppercase; }
    .badge.idle { background: var(--badge-idle); }
    .badge.recording { background: var(--badge-recording); }
    .badge.stopped { background: var(--badge-stopped); }
    .status-ok { color: var(--ok); font-weight: 700; }
    .status-warn { color: var(--warn); font-weight: 700; }
    pre { background: #0d1b2a; color: #dbe8f6; border-radius: 10px; padding: 12px; overflow: auto; font-size: 12px; min-height: 160px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 style="margin:0;">Zeb’s OBD AI</h1>
      <div style="margin-top:4px;color:#5b6b7f;">Safe read-only diagnostics and learning capture dashboard</div>
    </div>

    <div class="row">
      <div class="card">
        <h2 class="title">Vehicles</h2>
        <label class="label" for="vehicleSelect">Select vehicle profile</label>
        <select id="vehicleSelect" style="width:100%;"></select>
        <div style="margin-top:10px;"><button id="createSessionBtn" class="wide">Create Active Session</button></div>
      </div>
      <div class="card">
        <h2 class="title">Capture Presets</h2>
        <label class="label" for="presetSelect">Choose preset</label>
        <select id="presetSelect" style="width:100%;"></select>
      </div>
      <div class="card">
        <h2 class="title">Capture Controls</h2>
        <div style="display:grid;grid-template-columns:1fr;gap:10px;">
          <button id="startLearningBtn">Start Learning Session</button>
          <button id="stopLearningBtn" class="danger">Stop Learning Session</button>
        </div>
        <div style="margin-top:12px;">
          <span class="label">Capture Status</span>
          <span id="captureStatusBadge" class="badge idle">idle</span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2 class="title">Session Status</h2>
      <div class="kv">
        <div>Selected Vehicle</div><div id="selectedVehicle">-</div>
        <div>Active Session</div><div id="activeSession">-</div>
        <div>Protocol</div><div id="protocol">-</div>
        <div>Mode</div><div id="mode">-</div>
        <div>Connection</div><div id="connection">-</div>
      </div>
    </div>

    <div class="card">
      <h2 class="title">Quick Safe Reads</h2>
      <div class="btn-grid" id="quickReadButtons"></div>
    </div>

    <div class="card">
      <h2 class="title">Tag Event</h2>
      <div class="btn-grid" id="eventTagButtons"></div>
    </div>

    <div class="row">
      <div class="card">
        <h2 class="title">Latest Response</h2>
        <pre id="latestResponse">No reads yet.</pre>
      </div>
      <div class="card">
        <h2 class="title">Recent Recorded Data</h2>
        <table>
          <thead><tr><th>Time (UTC)</th><th>Command</th><th>Vehicle</th><th>Raw</th></tr></thead>
          <tbody id="historyRows"><tr><td colspan="4">No captured reads yet</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2 class="title">Recent Tagged Events</h2>
      <table>
        <thead><tr><th>Time (UTC)</th><th>Tag</th><th>Note</th></tr></thead>
        <tbody id="eventRows"><tr><td colspan="3">No tagged events yet</td></tr></tbody>
      </table>
    </div>
  </div>

<script>
let state = null;
let selectedVehicleId = "toyota_sienna_2006";
let selectedPreset = "cold_start_capture";

async function fetchState() {
  const res = await fetch('/dashboard/state');
  state = await res.json();
  render();
}

function renderVehicles() {
  const select = document.getElementById('vehicleSelect');
  select.innerHTML = '';
  state.vehicles.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v.vehicle_id;
    opt.textContent = `${v.label} (${v.protocol_hint})`;
    if (v.vehicle_id === selectedVehicleId) opt.selected = true;
    select.appendChild(opt);
  });
  select.onchange = (e) => { selectedVehicleId = e.target.value; renderSessionArea(); };
}

function renderPresets() {
  const select = document.getElementById('presetSelect');
  select.innerHTML = '';
  Object.keys(state.capture_presets).forEach(key => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = state.capture_presets[key];
    if (key === selectedPreset) opt.selected = true;
    select.appendChild(opt);
  });
  selectedPreset = state.capture_preset || selectedPreset;
  select.value = selectedPreset;
  select.onchange = (e) => { selectedPreset = e.target.value; };
}

function renderSessionArea() {
  const active = state.active_session;
  const selected = state.vehicles.find(v => v.vehicle_id === selectedVehicleId);
  document.getElementById('selectedVehicle').textContent = selected ? `${selected.label} (${selected.protocol_hint})` : '-';
  document.getElementById('activeSession').textContent = active ? active.session_id : 'None';
  document.getElementById('protocol').textContent = active ? active.protocol : '-';
  document.getElementById('mode').textContent = state.adapter_mode;
  const conn = document.getElementById('connection');
  conn.textContent = state.connection_status;
  conn.className = state.connection_status.includes('connected') || state.connection_status.includes('ready') ? 'status-ok' : 'status-warn';

  const badge = document.getElementById('captureStatusBadge');
  badge.textContent = state.capture_status;
  badge.className = `badge ${state.capture_status}`;
}

function renderQuickButtons() {
  const labels = {
    rpm: 'Read RPM',
    coolant_temp: 'Read Coolant Temp',
    vehicle_speed: 'Read Vehicle Speed',
    control_module_voltage: 'Read Control Module Voltage',
    vin: 'Read VIN',
    stored_codes: 'Read Stored Codes',
    pending_codes: 'Read Pending Codes'
  };
  const container = document.getElementById('quickReadButtons');
  container.innerHTML = '';
  Object.keys(state.quick_reads).forEach(key => {
    const btn = document.createElement('button');
    btn.textContent = labels[key] || key;
    btn.onclick = () => runQuickRead(key);
    container.appendChild(btn);
  });
}

function renderEventButtons() {
  const container = document.getElementById('eventTagButtons');
  container.innerHTML = '';
  state.event_tags.forEach(tag => {
    const btn = document.createElement('button');
    btn.textContent = tag;
    btn.onclick = () => tagEvent(tag);
    container.appendChild(btn);
  });
}

function renderHistory() {
  const rows = document.getElementById('historyRows');
  if (!state.recent_reads.length) {
    rows.innerHTML = '<tr><td colspan="4">No captured reads yet</td></tr>';
    return;
  }
  rows.innerHTML = state.recent_reads.slice().reverse().map(item =>
    `<tr><td>${item.ts}</td><td>${item.command}</td><td>${item.vehicle}</td><td>${item.raw_response}</td></tr>`
  ).join('');
}

function renderEvents() {
  const rows = document.getElementById('eventRows');
  if (!state.recent_events.length) {
    rows.innerHTML = '<tr><td colspan="3">No tagged events yet</td></tr>';
    return;
  }
  rows.innerHTML = state.recent_events.slice().reverse().map(item =>
    `<tr><td>${item.ts}</td><td>${item.tag}</td><td>${item.note || ''}</td></tr>`
  ).join('');
}

function render() {
  renderVehicles();
  renderPresets();
  renderSessionArea();
  renderQuickButtons();
  renderEventButtons();
  renderHistory();
  renderEvents();
}

async function createSession() {
  const res = await fetch('/sessions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({vehicle_id: selectedVehicleId})
  });
  const payload = await res.json();
  document.getElementById('latestResponse').textContent = JSON.stringify(payload, null, 2);
  await fetchState();
}

async function runQuickRead(key) {
  const res = await fetch(`/obd/read/quick/${key}`, { method: 'POST' });
  const payload = await res.json();
  document.getElementById('latestResponse').textContent = JSON.stringify(payload, null, 2);
  await fetchState();
}

async function startLearning() {
  const res = await fetch('/capture/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({vehicle_id: selectedVehicleId, preset: selectedPreset})
  });
  const payload = await res.json();
  document.getElementById('latestResponse').textContent = JSON.stringify(payload, null, 2);
  await fetchState();
}

async function stopLearning() {
  const res = await fetch('/capture/stop', { method: 'POST' });
  const payload = await res.json();
  document.getElementById('latestResponse').textContent = JSON.stringify(payload, null, 2);
  await fetchState();
}

async function tagEvent(tag) {
  const res = await fetch('/capture/tag', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tag})
  });
  const payload = await res.json();
  document.getElementById('latestResponse').textContent = JSON.stringify(payload, null, 2);
  await fetchState();
}

document.getElementById('createSessionBtn').onclick = createSession;
document.getElementById('startLearningBtn').onclick = startLearning;
document.getElementById('stopLearningBtn').onclick = stopLearning;
setInterval(fetchState, 1500);
fetchState();
</script>
</body>
</html>
    """
