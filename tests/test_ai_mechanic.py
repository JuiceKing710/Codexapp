from fastapi.testclient import TestClient

from sienna_diag.api.main import _new_phone_bridge_state, app, phone_bridge_state, store


client = TestClient(app)


def _reset_state() -> None:
    store.sessions.clear()
    store.events_by_session.clear()
    store.reads_by_session.clear()
    store.learning_records_by_session.clear()
    store.ai_alerts_by_session.clear()
    store.ai_responses_by_session.clear()
    store.timeline_by_session.clear()
    store.guided_diagnosis_plans_by_session.clear()
    store.guided_diagnosis_results_by_session.clear()
    store.active_session_id = None
    store.last_ai_request = None
    store.last_ai_response_timestamp = None
    phone_bridge_state.clear()
    phone_bridge_state.update(_new_phone_bridge_state())


def test_connect_success_starts_polling_and_shows_waiting_live_read_state() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.post(
        "/phone/bridge/connect",
        json={
            "platform": "android",
            "adapter_name": "OBDLink MX+",
            "status": "connected",
            "source_mode": "PHONE-LIVE",
            "supports_native_bluetooth": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["polling_active"] is True
    assert payload["polling_state"] == "starting"
    assert payload["current_mode"] == "CONNECTED_WAITING_LIVE_READ"
    assert payload["first_live_read_received"] is False

    dashboard = client.get("/dashboard/state")
    assert dashboard.status_code == 200
    data = dashboard.json()
    assert data["current_mode"] == "CONNECTED_WAITING_LIVE_READ"
    assert data["ai_monitoring"]["status"] == "waiting_for_first_live_read"

    timeline = client.get(f"/diagnostic-timeline/{session.session_id}")
    assert timeline.status_code == 200
    event_types = [item["event_type"] for item in timeline.json()["timeline"]]
    assert "vehicle_connected" in event_types
    assert "polling_started" in event_types


def test_first_live_read_flips_mode_and_activates_ai_monitoring() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")
    client.post(
        "/phone/bridge/connect",
        json={
            "platform": "ios",
            "adapter_name": "OBDLink MX+",
            "status": "connected",
            "source_mode": "PHONE-LIVE",
            "supports_native_bluetooth": True,
        },
    )

    response = client.post(
        "/phone/bridge/read",
        json={
            "session_id": session.session_id,
            "vehicle_id": session.vehicle_id,
            "command": "010C",
            "pid_key": "rpm",
            "value": 820,
            "unit": "rpm",
            "source_mode": "PHONE-LIVE",
            "raw_response": "41 0C 0C D0",
            "polling": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend_acceptance_status"] == "accepted"
    assert payload["current_mode"] == "PHONE-LIVE"
    assert payload["first_live_read_received"] is True
    assert payload["live_monitoring_active"] is True

    dashboard = client.get("/dashboard/state")
    assert dashboard.status_code == 200
    data = dashboard.json()
    assert data["current_mode"] == "PHONE-LIVE"
    assert data["phone_bridge"]["first_live_read_received"] is True
    assert data["phone_bridge"]["backend_acceptance_status"] == "accepted"
    assert data["ai_monitoring"]["active"] is True
    assert data["ai_monitoring"]["message"] == "Live monitoring active, no alerts detected"

    read = data["recent_reads"][-1]
    assert read["session_id"] == session.session_id
    assert read["vehicle_id"] == session.vehicle_id
    assert read["pid_key"] == "rpm"
    assert read["parsed_value"] == 820
    assert read["raw_command"] == "010C"
    assert read["raw_response"] == "41 0C 0C D0"
    assert read["source_mode"] == "PHONE-LIVE"
    assert read["polling"] is True

    timeline = client.get(f"/diagnostic-timeline/{session.session_id}")
    assert timeline.status_code == 200
    event_types = [item["event_type"] for item in timeline.json()["timeline"]]
    assert "first_live_read_received" in event_types
    assert "live_monitoring_active" in event_types


def test_replay_execution_is_blocked() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.post(
        "/learning/replay/execute",
        json={"session_id": session.session_id, "raw_command": "010C", "confirm_risky": True},
    )

    assert response.status_code == 403
    assert "permanently blocked" in response.json()["detail"]


def test_ai_mechanic_includes_memory_guardrails_and_timeline() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")
    store.update_vehicle_memory(session.vehicle_id, {"user_symptoms": ["rough idle"], "prior_vehicle_checks": ["baseline check"]})

    response = client.post("/ai/mechanic", json={"question": "Is it safe to drive?"})

    assert response.status_code == 200
    data = response.json()
    assert data["read_only_enforced"] is True
    assert "safety_guardrails" in data["context"]
    assert "blocked" in data["context"]["safety_guardrails"]
    assert data["timeline_event_id"]
    assert data["debug"]["request_kind"] == "user-requested"


def test_proactive_alerts_generated_from_live_data() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")

    client.post(
        "/phone/bridge/read",
        json={
            "session_id": session.session_id,
            "vehicle_id": session.vehicle_id,
            "command": "0105",
            "pid_key": "coolant_temp",
            "value": 112,
            "unit": "°C",
            "source_mode": "PHONE-LIVE",
            "raw_response": "PHONE-NATIVE",
            "polling": True,
        },
    )

    response = client.post("/ai/mechanic", json={"question": "What changed in live data?"})
    assert response.status_code == 200
    data = response.json()
    alerts = client.get(f"/ai/alerts/{session.session_id}")
    assert alerts.status_code == 200
    assert any("Coolant" in item["title"] for item in alerts.json()["alerts"])

    timeline = client.get(f"/diagnostic-timeline/{session.session_id}")
    assert timeline.status_code == 200
    assert any(item["event_type"] == "ai_alert_created" for item in timeline.json()["timeline"])


def test_guided_diagnosis_plan_and_result_are_logged() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")

    run_response = client.post(
        "/ai/guided-diagnosis/run",
        json={"session_id": session.session_id, "symptom": "rough idle"},
    )

    assert run_response.status_code == 200
    plan = run_response.json()["plan"]
    assert plan["recommended_tests"]

    result_response = client.post(
        "/ai/guided-diagnosis/result",
        json={"session_id": session.session_id, "step_id": "gd-1", "observed_result": "RPM stable at 780"},
    )
    assert result_response.status_code == 200
    assert result_response.json()["updated_plan"]["dynamic_tree_state"] == "updated_after_result"


def test_dashboard_state_includes_vehicle_intelligence_core_snapshot() -> None:
    _reset_state()
    store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.get("/dashboard/state")
    assert response.status_code == 200
    data = response.json()
    assert "vehicle_intelligence_core" in data
    assert "diagnostic_state_engine" in data["vehicle_intelligence_core"]
    assert "vehicle_health_score" in data


def test_ingest_rejection_updates_bridge_debug_state() -> None:
    _reset_state()
    store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.post(
        "/phone/bridge/read",
        json={
            "command": "9999",
            "pid_key": "unknown",
            "source_mode": "PHONE-LIVE",
            "raw_response": "NO DATA",
            "polling": True,
        },
    )

    assert response.status_code == 400

    bridge_state = client.get("/phone/bridge/state")
    assert bridge_state.status_code == 200
    data = bridge_state.json()
    assert data["last_ingest_status"] == "unsupported-pid"
    assert data["backend_acceptance_status"] == "rejected"
    assert "Unsupported PID" in data["last_ingest_error"]

def test_ai_mechanic_returns_visualization_hook_for_component_requests() -> None:
    _reset_state()
    store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.post(
        "/ai/mechanic",
        json={
            "question": "Show me the thermostat",
            "memory_updates": {"highlight_component": "thermostat", "system": "cooling"},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["visualization_hook"]["action"] == "highlight_component"
    assert data["visualization_hook"]["component"] == "thermostat"
    assert data["visualization_hook"]["system"] == "cooling_system"


def test_vehicle_visualization_highlight_and_explain_endpoints() -> None:
    _reset_state()

    highlight = client.post(
        "/vehicle-visualization/highlight",
        json={"action": "highlight_component", "component": "Thermostat", "system": "cooling", "source": "user"},
    )
    assert highlight.status_code == 200
    payload = highlight.json()["highlight"]
    assert payload["component"] == "thermostat"
    assert payload["system"] == "cooling_system"

    explain = client.get("/vehicle-visualization/explain", params={"component": "thermostat"})
    assert explain.status_code == 200
    assert "coolant" in explain.json()["explanation"].lower()
