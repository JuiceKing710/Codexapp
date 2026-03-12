from fastapi.testclient import TestClient

from sienna_diag.api.main import app, store


client = TestClient(app)


def _reset_state() -> None:
    store.sessions.clear()
    store.events_by_session.clear()
    store.reads_by_session.clear()
    store.learning_records_by_session.clear()
    store.ai_alerts_by_session.clear()
    store.ai_responses_by_session.clear()
    store.timeline_by_session.clear()
    store.active_session_id = None
    store.last_ai_request = None
    store.last_ai_response_timestamp = None


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
    assert data["proactive_alerts"]
    assert any("Coolant" in item["title"] for item in data["proactive_alerts"])

    timeline = client.get(f"/diagnostic-timeline/{session.session_id}")
    assert timeline.status_code == 200
    assert any(item["event_type"] == "ai_alert_created" for item in timeline.json()["timeline"])
