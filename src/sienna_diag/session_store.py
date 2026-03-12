from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sienna_diag.models import (
    AIAlertRecord,
    AIResponseRecord,
    CommandLearningRecord,
    DiagnosticTimelineEvent,
    EventTag,
    ReadHistoryItem,
    Session,
    VehicleProfile,
)


class SessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.events_by_session: dict[str, list[EventTag]] = defaultdict(list)
        self.reads_by_session: dict[str, list[ReadHistoryItem]] = defaultdict(list)
        self.active_session_id: str | None = None
        self.learning_records_by_session: dict[str, list[CommandLearningRecord]] = defaultdict(list)
        self.command_library: dict[str, dict] = {}
        self.replay_approvals: dict[tuple[str, str], dict] = {}
        self.ai_memory_by_vehicle: dict[str, dict] = defaultdict(dict)
        self.ai_alerts_by_session: dict[str, list[AIAlertRecord]] = defaultdict(list)
        self.ai_responses_by_session: dict[str, list[AIResponseRecord]] = defaultdict(list)
        self.timeline_by_session: dict[str, list[DiagnosticTimelineEvent]] = defaultdict(list)
        self.last_ai_request: dict | None = None
        self.last_ai_response_timestamp: str | None = None
        self.knowledge_library: dict[str, dict] = {
            "dtc_definitions": {
                "P0300": "Random/multiple cylinder misfire detected.",
                "P0420": "Catalyst system efficiency below threshold (bank 1).",
                "P0171": "System too lean (bank 1).",
            },
            "pid_definitions": {
                "010C": "Engine RPM",
                "0105": "Engine coolant temperature",
                "010D": "Vehicle speed",
                "0111": "Throttle position",
                "0142": "Control module voltage",
                "010F": "Intake air temperature",
            },
            "vehicle_specific_notes": {
                "toyota_sienna_2006": [
                    "Default DLC3/ECM diagnostics should start as ISO 9141-2 request/response.",
                ]
            },
            "component_mappings": {
                "coolant_temp": "ECT sensor and thermostat behavior",
                "control_module_voltage": "Battery, alternator, and charging circuit",
            },
            "successful_diagnosis_paths": [],
            "repair_observations": [],
            "training_export_knowledge": [],
        }
        self.vehicles: dict[str, VehicleProfile] = {
            "toyota_sienna_2006": VehicleProfile(
                vehicle_id="toyota_sienna_2006",
                label="2006 Toyota Sienna",
                protocol_hint="ISO_9141_2",
                notes="Default DLC3 ECM hint is ISO 9141-2 request/response.",
            ),
            "honda_pilot_2018": VehicleProfile(
                vehicle_id="honda_pilot_2018",
                label="2018 Honda Pilot",
                protocol_hint="CAN_11_500",
                notes="Use CAN capture source when data comes from CAN logs.",
            ),
            "mercedes_clk320_2002": VehicleProfile(
                vehicle_id="mercedes_clk320_2002",
                label="2002 Mercedes CLK320",
                protocol_hint="ISO_9141_2",
            ),
        }

    def list_vehicles(self) -> list[VehicleProfile]:
        return list(self.vehicles.values())

    def create_session(self, vehicle_id: str | None = None) -> Session:
        selected_vehicle_id = vehicle_id or "toyota_sienna_2006"
        if selected_vehicle_id not in self.vehicles:
            raise KeyError(selected_vehicle_id)

        profile = self.vehicles[selected_vehicle_id]
        session = Session(vehicle_id=profile.vehicle_id, vehicle=profile.label, protocol=profile.protocol_hint)
        self.sessions[session.session_id] = session
        self.active_session_id = session.session_id
        self.add_timeline_event(
            DiagnosticTimelineEvent(
                session_id=session.session_id,
                event_type="vehicle_check_started",
                title="Vehicle check started",
                detail=f"Vehicle check started for {session.vehicle}.",
            )
        )
        return session

    def assign_session_vin(self, session_id: str, vin: str | None, assignment_source: str) -> Session:
        session = self.get_session(session_id)
        session.vin = vin
        session.assignment_source = assignment_source
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]

    def get_active_session(self) -> Session:
        if self.active_session_id is None:
            raise KeyError("No active session")
        return self.get_session(self.active_session_id)

    def close_active_session(self) -> Session | None:
        if self.active_session_id is None:
            return None
        session = self.get_session(self.active_session_id)
        self.add_timeline_event(
            DiagnosticTimelineEvent(
                session_id=session.session_id,
                event_type="vehicle_check_stopped",
                title="Vehicle check stopped",
                detail=f"Vehicle check stopped for {session.vehicle}.",
            )
        )
        self.active_session_id = None
        return session

    def add_event(self, event: EventTag) -> EventTag:
        if event.session_id not in self.sessions:
            raise KeyError(event.session_id)
        self.events_by_session[event.session_id].append(event)
        self.add_timeline_event(
            DiagnosticTimelineEvent(
                session_id=event.session_id,
                event_type="user_tag_event",
                title="User tag added",
                detail=f"Tag: {event.tag}. {event.note or ''}".strip(),
                source="user",
                metadata={"event_id": event.event_id},
            )
        )
        return event

    def get_events(self, session_id: str) -> list[EventTag]:
        return self.events_by_session.get(session_id, [])

    def add_read(self, item: ReadHistoryItem) -> ReadHistoryItem:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.reads_by_session[item.session_id]
        history.append(item)
        self.reads_by_session[item.session_id] = history[-200:]

        if item.command in {"03", "07"}:
            self.add_timeline_event(
                DiagnosticTimelineEvent(
                    session_id=item.session_id,
                    event_type="dtc_detected",
                    title="DTC data updated",
                    detail=f"{item.command} returned {item.raw_response}",
                    related_codes=[str(item.value)] if item.value else [],
                    metadata={"read_id": item.read_id},
                )
            )
        return item

    def get_reads(self, session_id: str) -> list[ReadHistoryItem]:
        return self.reads_by_session.get(session_id, [])

    def add_learning_record(self, item: CommandLearningRecord) -> CommandLearningRecord:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.learning_records_by_session[item.session_id]
        history.append(item)
        self.learning_records_by_session[item.session_id] = history[-300:]

        key = item.raw_command.upper()
        existing = self.command_library.get(
            key,
            {
                "command_identity": key,
                "observed_behavior": [],
                "affected_module": None,
                "associated_dtcs": [],
                "observed_state_changes": [],
                "confidence_score": None,
                "approval_status": "manual-required",
                "replay_history": [],
                "notes": [],
            },
        )
        if item.notes:
            existing["notes"].append(item.notes)
        if item.parsed_response is not None:
            existing["observed_behavior"].append(item.parsed_response)
        if item.after_state_snapshot:
            existing["observed_state_changes"].append(item.after_state_snapshot)
        if item.confidence_score is not None:
            existing["confidence_score"] = item.confidence_score
        if item.manually_approved_for_replay:
            existing["approval_status"] = "approved"
        if item.source_type == "replay":
            existing["replay_history"].append(
                {
                    "timestamp": item.timestamp.isoformat(),
                    "succeeded": item.replay_succeeded,
                    "session_id": item.session_id,
                }
            )
        self.command_library[key] = existing
        return item

    def get_learning_records(self, session_id: str) -> list[CommandLearningRecord]:
        return self.learning_records_by_session.get(session_id, [])

    def set_replay_approval(self, session_id: str, raw_command: str, approved: bool, notes: str | None = None) -> dict:
        key = (session_id, raw_command.upper())
        self.replay_approvals[key] = {"approved": approved, "notes": notes}
        return self.replay_approvals[key]

    def get_replay_approval(self, session_id: str, raw_command: str) -> dict | None:
        return self.replay_approvals.get((session_id, raw_command.upper()))

    def get_vehicle_memory(self, vehicle_id: str) -> dict:
        memory = self.ai_memory_by_vehicle.get(vehicle_id)
        if memory:
            return memory
        return {
            "vehicle_profile": self.vehicles.get(vehicle_id).model_dump() if vehicle_id in self.vehicles else None,
            "vin": None,
            "prior_vehicle_checks": [],
            "dtc_history": [],
            "sensor_patterns": [],
            "user_symptoms": [],
            "notes": [],
            "repair_outcomes": [],
            "unresolved_issues": [],
            "false_positives": [],
            "confidence_tags": [],
            "last_updated": None,
        }

    def update_vehicle_memory(self, vehicle_id: str, updates: dict) -> dict:
        memory = self.get_vehicle_memory(vehicle_id)
        for key, value in updates.items():
            if value is None:
                continue
            if isinstance(memory.get(key), list) and isinstance(value, list):
                memory[key].extend(value)
                memory[key] = memory[key][-100:]
            else:
                memory[key] = value
        memory["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.ai_memory_by_vehicle[vehicle_id] = memory
        return memory

    def add_ai_alert(self, item: AIAlertRecord) -> AIAlertRecord:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.ai_alerts_by_session[item.session_id]
        history.append(item)
        self.ai_alerts_by_session[item.session_id] = history[-200:]
        return item

    def get_ai_alerts(self, session_id: str) -> list[AIAlertRecord]:
        return self.ai_alerts_by_session.get(session_id, [])

    def add_ai_response(self, item: AIResponseRecord) -> AIResponseRecord:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.ai_responses_by_session[item.session_id]
        history.append(item)
        self.ai_responses_by_session[item.session_id] = history[-300:]
        self.last_ai_response_timestamp = item.created_at.isoformat()
        return item

    def get_ai_responses(self, session_id: str) -> list[AIResponseRecord]:
        return self.ai_responses_by_session.get(session_id, [])

    def add_timeline_event(self, item: DiagnosticTimelineEvent) -> DiagnosticTimelineEvent:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.timeline_by_session[item.session_id]
        history.append(item)
        self.timeline_by_session[item.session_id] = history[-400:]
        return item

    def get_timeline_events(self, session_id: str) -> list[DiagnosticTimelineEvent]:
        return self.timeline_by_session.get(session_id, [])


store = SessionStore()
