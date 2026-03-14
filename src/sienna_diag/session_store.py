from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone

from sienna_diag.models import (
    AIAlertRecord,
    AIResponseRecord,
    CommandLearningRecord,
    DiagnosticTimelineEvent,
    EventTag,
    ReadHistoryItem,
    Session,
    User,
    UserProfile,
    VehicleProfile,
)


active_user_context: ContextVar[str] = ContextVar("active_user_context", default="demo")


def set_active_user_context(user_id: str) -> None:
    active_user_context.set(user_id)


class SessionStore:
    def __init__(self) -> None:
        self.sessions_by_user: dict[str, dict[str, Session]] = defaultdict(dict)
        self.events_by_session: dict[str, list[EventTag]] = defaultdict(list)
        self.reads_by_session: dict[str, list[ReadHistoryItem]] = defaultdict(list)
        self.active_session_by_user: dict[str, str | None] = defaultdict(lambda: None)
        self.learning_records_by_session: dict[str, list[CommandLearningRecord]] = defaultdict(list)
        self.command_library_by_user: dict[str, dict[str, dict]] = defaultdict(dict)
        self.replay_approvals: dict[tuple[str, str], dict] = {}
        self.ai_memory_by_user_vehicle: dict[tuple[str, str], dict] = defaultdict(dict)
        self.ai_alerts_by_session: dict[str, list[AIAlertRecord]] = defaultdict(list)
        self.ai_responses_by_session: dict[str, list[AIResponseRecord]] = defaultdict(list)
        self.timeline_by_session: dict[str, list[DiagnosticTimelineEvent]] = defaultdict(list)
        self.guided_diagnosis_plans_by_session: dict[str, dict] = {}
        self.guided_diagnosis_results_by_session: dict[str, list[dict]] = defaultdict(list)
        self.last_ai_request_by_user: dict[str, dict] = {}
        self.last_ai_response_timestamp_by_user: dict[str, str] = {}
        self.users_by_email: dict[str, dict] = {}
        self.users_by_id: dict[str, User] = {}
        self.user_profiles: dict[str, UserProfile] = {}

        self.app_settings_by_user: dict[str, dict] = defaultdict(dict)
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
            "component_mappings": {
                "coolant_temp": "ECT sensor and thermostat behavior",
                "control_module_voltage": "Battery, alternator, and charging circuit",
            },
        }
        self.vehicles_by_user: dict[str, dict[str, VehicleProfile]] = defaultdict(dict)
        self._seed_demo_user_data()

    def _default_user_id(self, user_id: str | None) -> str:
        return user_id or active_user_context.get()

    def _seed_demo_user_data(self) -> None:
        demo_user = User(user_id="demo", email="demo@local", display_name="Demo Tester")
        self.users_by_id[demo_user.user_id] = demo_user
        self.user_profiles[demo_user.user_id] = UserProfile(user_id=demo_user.user_id)
        self.app_settings_by_user[demo_user.user_id] = self._default_app_settings()
        self._seed_vehicles_for_user(demo_user.user_id)

    def _default_app_settings(self) -> dict:
        return {
            "units": "metric",
            "polling_preferences": {"interval_ms": 500, "auto_start_after_connect": True},
            "alert_preferences": {"proactive_alerts": True, "severity_threshold": "suspected"},
            "theme": "futuristic-dark",
            "adapter_settings": {"preferred_adapter": "OBDLink MX+", "reconnect_enabled": True},
            "voice_options": {"speech_output": True, "speech_input": True},
            "ai_behavior_controls": {"confidence_display": True, "explain_reasoning": True},
        }

    def _seed_vehicles_for_user(self, user_id: str) -> None:
        if self.vehicles_by_user[user_id]:
            return
        self.vehicles_by_user[user_id] = {
            "toyota_sienna_2006": VehicleProfile(
                vehicle_id="toyota_sienna_2006",
                user_id=user_id,
                label="2006 Toyota Sienna",
                protocol_hint="ISO_9141_2",
                notes="Default DLC3 ECM hint is ISO 9141-2 request/response.",
            ),
            "honda_pilot_2018": VehicleProfile(
                vehicle_id="honda_pilot_2018",
                user_id=user_id,
                label="2018 Honda Pilot",
                protocol_hint="CAN_11_500",
                notes="Use CAN capture source when data comes from CAN logs.",
            ),
            "mercedes_clk320_2002": VehicleProfile(
                vehicle_id="mercedes_clk320_2002",
                user_id=user_id,
                label="2002 Mercedes CLK320",
                protocol_hint="ISO_9141_2",
            ),
        }

    def register_user(self, email: str, password_hash: str, display_name: str) -> User:
        key = email.strip().lower()
        if key in self.users_by_email:
            raise ValueError("email_already_exists")
        now = datetime.now(timezone.utc)
        user = User(email=key, display_name=display_name, created_at=now, updated_at=now)
        self.users_by_email[key] = {"user_id": user.user_id, "password_hash": password_hash}
        self.users_by_id[user.user_id] = user
        self.user_profiles[user.user_id] = UserProfile(user_id=user.user_id)
        self.app_settings_by_user[user.user_id] = self._default_app_settings()
        self._seed_vehicles_for_user(user.user_id)
        return user

    def authenticate_user(self, email: str, password_hash: str) -> User | None:
        key = email.strip().lower()
        account = self.users_by_email.get(key)
        if not account or account["password_hash"] != password_hash:
            return None
        return self.users_by_id.get(account["user_id"])

    def get_user(self, user_id: str) -> User:
        user = self.users_by_id.get(user_id)
        if not user:
            raise KeyError(user_id)
        return user

    def get_user_profile(self, user_id: str) -> UserProfile:
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserProfile(user_id=user_id)
        return self.user_profiles[user_id]

    def update_user_profile(self, user_id: str, updates: dict) -> tuple[User, UserProfile]:
        user = self.get_user(user_id)
        profile = self.get_user_profile(user_id)
        now = datetime.now(timezone.utc)
        if updates.get("display_name"):
            user.display_name = updates["display_name"]
            user.updated_at = now
            self.users_by_id[user_id] = user
        if "profile_image" in updates and updates["profile_image"] is not None:
            profile.profile_image = updates["profile_image"]
        if updates.get("preferred_theme"):
            profile.preferred_theme = updates["preferred_theme"]
        if updates.get("preferred_unit_system"):
            profile.preferred_unit_system = updates["preferred_unit_system"]
        profile.updated_at = now
        self.user_profiles[user_id] = profile
        return user, profile

    def list_vehicles(self, user_id: str | None = None) -> list[VehicleProfile]:
        uid = self._default_user_id(user_id)
        self._seed_vehicles_for_user(uid)
        return list(self.vehicles_by_user[uid].values())

    def create_vehicle(self, user_id: str, vehicle: VehicleProfile) -> VehicleProfile:
        self._seed_vehicles_for_user(user_id)
        self.vehicles_by_user[user_id][vehicle.vehicle_id] = vehicle
        return vehicle

    def create_session(self, vehicle_id: str | None = None, user_id: str | None = None) -> Session:
        uid = self._default_user_id(user_id)
        self._seed_vehicles_for_user(uid)
        selected_vehicle_id = vehicle_id or "toyota_sienna_2006"
        if selected_vehicle_id not in self.vehicles_by_user[uid]:
            raise KeyError(selected_vehicle_id)

        profile = self.vehicles_by_user[uid][selected_vehicle_id]
        session = Session(user_id=uid, vehicle_id=profile.vehicle_id, vehicle=profile.label, protocol=profile.protocol_hint)
        self.sessions_by_user[uid][session.session_id] = session
        self.active_session_by_user[uid] = session.session_id
        self.add_timeline_event(
            DiagnosticTimelineEvent(
                user_id=uid,
                session_id=session.session_id,
                event_type="vehicle_check_started",
                title="Vehicle check started",
                detail=f"Vehicle check started for {session.vehicle}.",
            ),
            user_id=uid,
        )
        return session

    def assign_session_vin(self, session_id: str, vin: str | None, assignment_source: str, user_id: str | None = None) -> Session:
        session = self.get_session(session_id, user_id=user_id)
        session.vin = vin
        session.assignment_source = assignment_source
        self.sessions_by_user[session.user_id or self._default_user_id(user_id)][session_id] = session
        return session

    def get_session(self, session_id: str, user_id: str | None = None) -> Session:
        uid = self._default_user_id(user_id)
        if session_id not in self.sessions_by_user[uid]:
            raise KeyError(session_id)
        return self.sessions_by_user[uid][session_id]

    def get_active_session(self, user_id: str | None = None) -> Session:
        uid = self._default_user_id(user_id)
        active = self.active_session_by_user.get(uid)
        if active is None:
            raise KeyError("No active session")
        return self.get_session(active, user_id=uid)

    def close_active_session(self, user_id: str | None = None) -> Session | None:
        uid = self._default_user_id(user_id)
        active = self.active_session_by_user.get(uid)
        if active is None:
            return None
        session = self.get_session(active, user_id=uid)
        self.add_timeline_event(DiagnosticTimelineEvent(user_id=uid, session_id=session.session_id, event_type="vehicle_check_stopped", title="Vehicle check stopped", detail=f"Vehicle check stopped for {session.vehicle}."), user_id=uid)
        self.active_session_by_user[uid] = None
        return session

    def list_sessions(self, user_id: str | None = None) -> list[Session]:
        uid = self._default_user_id(user_id)
        return list(self.sessions_by_user[uid].values())

    def get_active_session_id(self, user_id: str | None = None) -> str | None:
        uid = self._default_user_id(user_id)
        return self.active_session_by_user.get(uid)

    @property
    def active_session_id(self) -> str | None:
        return self.get_active_session_id()

    @active_session_id.setter
    def active_session_id(self, value: str | None) -> None:
        self.active_session_by_user[self._default_user_id(None)] = value

    @property
    def sessions(self) -> dict[str, Session]:
        return self.sessions_by_user[self._default_user_id(None)]

    def add_event(self, event: EventTag, user_id: str | None = None) -> EventTag:
        session = self.get_session(event.session_id, user_id=user_id)
        event.user_id = session.user_id
        self.events_by_session[event.session_id].append(event)
        self.add_timeline_event(DiagnosticTimelineEvent(user_id=session.user_id, session_id=event.session_id, event_type="user_tag_event", title="User tag added", detail=f"Tag: {event.tag}. {event.note or ''}".strip(), source="user", metadata={"event_id": event.event_id}), user_id=session.user_id)
        return event

    def get_events(self, session_id: str, user_id: str | None = None) -> list[EventTag]:
        self.get_session(session_id, user_id=user_id)
        return self.events_by_session.get(session_id, [])

    def add_read(self, item: ReadHistoryItem, user_id: str | None = None) -> ReadHistoryItem:
        session = self.get_session(item.session_id, user_id=user_id)
        item.user_id = session.user_id
        history = self.reads_by_session[item.session_id]
        history.append(item)
        self.reads_by_session[item.session_id] = history[-200:]
        return item

    def get_reads(self, session_id: str, user_id: str | None = None) -> list[ReadHistoryItem]:
        self.get_session(session_id, user_id=user_id)
        return self.reads_by_session.get(session_id, [])

    def add_learning_record(self, item: CommandLearningRecord, user_id: str | None = None) -> CommandLearningRecord:
        session = self.get_session(item.session_id, user_id=user_id)
        item.user_id = session.user_id
        history = self.learning_records_by_session[item.session_id]
        history.append(item)
        self.learning_records_by_session[item.session_id] = history[-300:]
        key = item.raw_command.upper()
        library = self.command_library_by_user[session.user_id or "demo"]
        existing = library.get(key, {"command_identity": key, "observed_behavior": []})
        if item.parsed_response is not None:
            existing["observed_behavior"].append(item.parsed_response)
        library[key] = existing
        return item

    def get_learning_records(self, session_id: str, user_id: str | None = None) -> list[CommandLearningRecord]:
        self.get_session(session_id, user_id=user_id)
        return self.learning_records_by_session.get(session_id, [])

    def set_replay_approval(self, session_id: str, raw_command: str, approved: bool, notes: str | None = None, user_id: str | None = None) -> dict:
        self.get_session(session_id, user_id=user_id)
        key = (session_id, raw_command.upper())
        self.replay_approvals[key] = {"approved": approved, "notes": notes}
        return self.replay_approvals[key]

    def get_replay_approval(self, session_id: str, raw_command: str, user_id: str | None = None) -> dict | None:
        self.get_session(session_id, user_id=user_id)
        return self.replay_approvals.get((session_id, raw_command.upper()))

    def get_vehicle_memory(self, vehicle_id: str, user_id: str | None = None) -> dict:
        uid = self._default_user_id(user_id)
        key = (uid, vehicle_id)
        memory = self.ai_memory_by_user_vehicle.get(key)
        if memory:
            return memory
        self._seed_vehicles_for_user(uid)
        return {
            "vehicle_profile": self.vehicles_by_user[uid].get(vehicle_id).model_dump() if vehicle_id in self.vehicles_by_user[uid] else None,
            "vin": None,
            "vehicle_metadata": {},
            "module_inventory": [],
            "supported_pids": [],
            "supported_commands": [],
            "baseline_sensor_ranges": {},
            "prior_vehicle_checks": [],
            "dtc_history": [],
            "repair_history": [],
            "ai_alerts": [],
            "guided_test_history": [],
            "recurring_issues": [],
            "sensor_patterns": [],
            "user_symptoms": [],
            "notes": [],
            "repair_outcomes": [],
            "unresolved_issues": [],
            "false_positives": [],
            "confidence_tags": [],
            "change_tracking": {
                "changed": [],
                "returned": [],
                "worsened": [],
                "improved": [],
            },
            "last_updated": None,
        }

    def update_vehicle_memory(self, vehicle_id: str, updates: dict, user_id: str | None = None) -> dict:
        uid = self._default_user_id(user_id)
        memory = self.get_vehicle_memory(vehicle_id, user_id=uid)
        for key, value in updates.items():
            if value is None:
                continue
            if isinstance(memory.get(key), list) and isinstance(value, list):
                memory[key].extend(value)
                memory[key] = memory[key][-100:]
            else:
                memory[key] = value
        memory["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.ai_memory_by_user_vehicle[(uid, vehicle_id)] = memory
        return memory

    def add_ai_alert(self, item: AIAlertRecord, user_id: str | None = None) -> AIAlertRecord:
        session = self.get_session(item.session_id, user_id=user_id)
        item.user_id = session.user_id
        history = self.ai_alerts_by_session[item.session_id]
        history.append(item)
        self.ai_alerts_by_session[item.session_id] = history[-200:]
        return item

    def get_ai_alerts(self, session_id: str, user_id: str | None = None) -> list[AIAlertRecord]:
        self.get_session(session_id, user_id=user_id)
        return self.ai_alerts_by_session.get(session_id, [])

    def add_ai_response(self, item: AIResponseRecord, user_id: str | None = None) -> AIResponseRecord:
        session = self.get_session(item.session_id, user_id=user_id)
        item.user_id = session.user_id
        history = self.ai_responses_by_session[item.session_id]
        history.append(item)
        self.ai_responses_by_session[item.session_id] = history[-300:]
        self.last_ai_response_timestamp_by_user[session.user_id or "demo"] = item.created_at.isoformat()
        return item

    def get_ai_responses(self, session_id: str, user_id: str | None = None) -> list[AIResponseRecord]:
        self.get_session(session_id, user_id=user_id)
        return self.ai_responses_by_session.get(session_id, [])

    def add_timeline_event(self, item: DiagnosticTimelineEvent, user_id: str | None = None) -> DiagnosticTimelineEvent:
        self.get_session(item.session_id, user_id=user_id)
        history = self.timeline_by_session[item.session_id]
        history.append(item)
        self.timeline_by_session[item.session_id] = history[-400:]
        return item

    def get_timeline_events(self, session_id: str, user_id: str | None = None) -> list[DiagnosticTimelineEvent]:
        self.get_session(session_id, user_id=user_id)
        return self.timeline_by_session.get(session_id, [])

    def set_guided_diagnosis_plan(self, session_id: str, plan: dict, user_id: str | None = None) -> dict:
        self.get_session(session_id, user_id=user_id)
        self.guided_diagnosis_plans_by_session[session_id] = plan
        return plan

    def get_guided_diagnosis_plan(self, session_id: str, user_id: str | None = None) -> dict | None:
        self.get_session(session_id, user_id=user_id)
        return self.guided_diagnosis_plans_by_session.get(session_id)

    def add_guided_diagnosis_result(self, session_id: str, result: dict, user_id: str | None = None) -> dict:
        self.get_session(session_id, user_id=user_id)
        history = self.guided_diagnosis_results_by_session[session_id]
        history.append(result)
        self.guided_diagnosis_results_by_session[session_id] = history[-150:]
        return result

    def get_guided_diagnosis_results(self, session_id: str, user_id: str | None = None) -> list[dict]:
        self.get_session(session_id, user_id=user_id)
        return self.guided_diagnosis_results_by_session.get(session_id, [])


    def get_app_settings(self, user_id: str | None = None) -> dict:
        uid = self._default_user_id(user_id)
        if uid not in self.app_settings_by_user or not self.app_settings_by_user[uid]:
            self.app_settings_by_user[uid] = self._default_app_settings()
        return self.app_settings_by_user[uid]

    def update_app_settings(self, updates: dict, user_id: str | None = None) -> dict:
        uid = self._default_user_id(user_id)
        settings = self.get_app_settings(user_id=uid)
        for key, value in updates.items():
            if value is None:
                continue
            if isinstance(settings.get(key), dict) and isinstance(value, dict):
                settings[key].update(value)
            else:
                settings[key] = value
        return settings



    @property
    def command_library(self) -> dict[str, dict]:
        return self.command_library_by_user[self._default_user_id(None)]

    @property
    def last_ai_request(self) -> dict | None:
        return self.last_ai_request_by_user.get(self._default_user_id(None))

    @last_ai_request.setter
    def last_ai_request(self, value: dict | None) -> None:
        uid = self._default_user_id(None)
        if value is None:
            self.last_ai_request_by_user.pop(uid, None)
        else:
            self.last_ai_request_by_user[uid] = value

    @property
    def last_ai_response_timestamp(self) -> str | None:
        return self.last_ai_response_timestamp_by_user.get(self._default_user_id(None))

    @last_ai_response_timestamp.setter
    def last_ai_response_timestamp(self, value: str | None) -> None:
        uid = self._default_user_id(None)
        if value is None:
            self.last_ai_response_timestamp_by_user.pop(uid, None)
        else:
            self.last_ai_response_timestamp_by_user[uid] = value


store = SessionStore()
