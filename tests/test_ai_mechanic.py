from fastapi.testclient import TestClient

from sienna_diag.api.main import app, store


client = TestClient(app)


def _reset_state() -> None:
    store.sessions.clear()
    store.events_by_session.clear()
    store.reads_by_session.clear()
    store.learning_records_by_session.clear()
    store.active_session_id = None


def test_replay_execution_is_blocked() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")

    response = client.post(
        "/learning/replay/execute",
        json={"session_id": session.session_id, "raw_command": "010C", "confirm_risky": True},
    )

    assert response.status_code == 403
    assert "permanently blocked" in response.json()["detail"]


def test_ai_mechanic_includes_memory_and_guardrails() -> None:
    _reset_state()
    session = store.create_session(vehicle_id="toyota_sienna_2006")
    store.update_vehicle_memory(session.vehicle_id, {"user_symptoms": ["rough idle"], "prior_vehicle_checks": ["baseline check"]})

    response = client.post("/ai/mechanic", json={"question": "Is it safe to drive?"})

    assert response.status_code == 200
    data = response.json()
    assert data["read_only_enforced"] is True
    assert "memory" in data["context"]
    assert "blocked" in data["context"]["safety_guardrails"]
