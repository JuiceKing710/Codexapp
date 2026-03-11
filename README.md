# Zeb’s OBD AI (`Zebs_obdAi`)

Read-only Python prototype for multi-vehicle diagnostics and mapping with strict safety controls.

## Safety scope (hard requirements)

This app supports standard read-only OBD requests only.

- Allowed OBD modes: `01`, `02`, `03`, `07`, `09`
- Blocked: control commands, bidirectional actions, replay, security access, programming, coding, tuning, write routines
- Adapter setup is restricted to documented baseline commands only (`ATZ`, `ATE0`, `ATL0`, `ATS0`, `ATH0`, `ATSP3`)
- No undocumented control commands
- Default assumption remains: DLC3 ECM diagnostics are ISO 9141-2 request/response unless source clearly indicates CAN capture

## Vehicle profiles

Included vehicle profiles:

- `2006 Toyota Sienna` (`toyota_sienna_2006`) - protocol hint `ISO_9141_2`
- `2018 Honda Pilot` (`honda_pilot_2018`) - protocol hint `CAN_11_500`
- `2002 Mercedes CLK320` (`mercedes_clk320_2002`) - protocol hint `ISO_9141_2`

Each session is linked to the selected vehicle and inherits its protocol hint.

## Project structure

- `src/sienna_diag/api/main.py`: FastAPI app, dashboard UI, vehicles/sessions/reads/review endpoints
- `src/sienna_diag/session_store.py`: in-memory vehicle profiles, sessions, active session, event tags, read history
- `src/sienna_diag/safety_policy.py`: read-only allow/block policy
- `src/sienna_diag/obd/adapter.py`: OBDLink adapter wrapper with mode/connection status
- `src/sienna_diag/preprocessing/pipeline.py`: log preprocess + summary generation
- `src/sienna_diag/review/lmstudio.py`: local LM Studio review hook
- `src/sienna_diag/review/openai_review.py`: OpenAI structured JSON second opinion
- `.env.example`: app and runtime configuration
- `docs/AGENTS_WORKFLOW.md`: AGENTS.md-aware workflow notes

## Dashboard improvements (Phase 1)

`/dashboard` now includes a phone-first UI for normal workflow (no manual JSON input):

- Large touch-friendly controls:
  - Connect Vehicle
  - Start Session
  - Stop Session (Phase 1 UI hook)
  - Read RPM
  - Read Coolant Temp
  - Start Capture
  - Stop Capture
  - Tag Event
  - Reports
- One-tap service calls:
  - Quick RPM
  - Quick Coolant Temp
- Visible status cards:
  - Current mode (`MOCK`, `PHONE-LIVE`, `LOCAL-HARDWARE`)
  - Active vehicle
  - Active session
  - Capture status
  - Last successful read
- 4-gauge configuration page:
  - Four adjustable gauges
  - Per-gauge sensor, label, min/max, unit, warning, and critical thresholds
  - Local preset save/load and switching
- Three-tier report framework hooks:
  - Customer Summary
  - Technician Detail
  - AI Training Export
- Developer/debug panel with explicit source classification so mock and live reads are visibly separated

## API compatibility and additions

Existing routes remain available:

- `POST /sessions`
- `GET /sessions/{session_id}`
- `POST /sessions/{session_id}/events`
- `POST /obd/read`
- `POST /review/local`
- `POST /review/openai`
- `POST /preprocess`
- `GET /dashboard`

New routes:

- `GET /vehicles`
- `GET /sessions`
- `GET /sessions/active`
- `POST /sessions/active/stop`
- `GET /sessions/{session_id}/reads`
- `POST /obd/read/quick/{read_key}`
- `GET /dashboard/state`
- `POST /capture/start`
- `POST /capture/stop`
- `POST /capture/tag`

## Safe learning capture workflow

Learning capture is read-only only. It does not support control commands, replay, security access, programming, tuning, or any bidirectional actions.

Polling groups used by capture:

- Fast group every ~1 second: `010C`, `0111`, `0142`, `010D`
- Slower group every ~4 seconds: `0105`, `010F`, `03`, `07`, `0902`

Each reading stores:

- UTC timestamp
- selected/active vehicle
- session ID
- command
- raw response

## Run instructions

1. Create and activate a virtual environment.
```bash
python3 -m venv .venv
source .venv/bin/activate
```
2. Install package in editable mode.
```bash
pip install -e .
```
3. Configure environment.
```bash
cp .env.example .env
```
4. Keep safe default mode:
- `ENABLE_HARDWARE=false`
5. Start server.
```bash
uvicorn sienna_diag.api.main:app --reload --host 127.0.0.1 --port 8000
```
6. Open dashboard.
- [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)

## Quick API examples

Create active session for 2018 Honda Pilot:

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"vehicle_id":"honda_pilot_2018"}'
```

Run read-only RPM request (uses explicit session or active session):

```bash
curl -X POST http://127.0.0.1:8000/obd/read \
  -H 'Content-Type: application/json' \
  -d '{"command":"010C","source_hint":"iso9141_2"}'
```

Run quick safe VIN read on active session:

```bash
curl -X POST http://127.0.0.1:8000/obd/read/quick/vin
```


## Phase 2 phone-live bridge notes

Phase 2 adds a production-safe phone bridge contract where the phone performs Bluetooth I/O and submits structured PHONE-LIVE reads to the backend.

New phone bridge routes:

- `GET /phone/bridge/state`
- `POST /phone/bridge/connect`
- `POST /phone/bridge/disconnect`
- `POST /phone/bridge/read`

`/phone/bridge/read` accepts read metadata including command, raw response, PID key, value, unit, source mode, timestamp, latency, and backend status. Reads are session-linked and stored with explicit source labels.

### Manual phone test flow (live RPM + coolant temp)

1. Start the backend and open `/dashboard` on a phone browser.
2. Tap **Connect Vehicle**. Confirm OBDLink Bluetooth state shows `connected`.
3. If no active session is present, the dashboard auto-creates one for the selected vehicle.
4. Start the vehicle and tap **Read RPM**.
5. Verify debug panel updates for:
   - `last_raw_pid_command` = `010C`
   - `last_raw_pid_response` populated
   - mode/source = `PHONE-LIVE`
6. Tap **Read Coolant Temp**.
7. Verify debug panel updates for:
   - `last_raw_pid_command` = `0105`
   - `last_raw_pid_response` populated
   - mode/source = `PHONE-LIVE`
8. Confirm gauge cards display current value, timestamp, and source label.
9. To test disconnect handling, call `POST /phone/bridge/disconnect` and confirm dashboard shows disconnected/offline state.
