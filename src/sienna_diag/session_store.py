from __future__ import annotations

from collections import defaultdict

from sienna_diag.models import EventTag, ReadHistoryItem, Session, VehicleProfile


class SessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.events_by_session: dict[str, list[EventTag]] = defaultdict(list)
        self.reads_by_session: dict[str, list[ReadHistoryItem]] = defaultdict(list)
        self.active_session_id: str | None = None
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
        self.active_session_id = None
        return session

    def add_event(self, event: EventTag) -> EventTag:
        if event.session_id not in self.sessions:
            raise KeyError(event.session_id)
        self.events_by_session[event.session_id].append(event)
        return event

    def get_events(self, session_id: str) -> list[EventTag]:
        return self.events_by_session.get(session_id, [])

    def add_read(self, item: ReadHistoryItem) -> ReadHistoryItem:
        if item.session_id not in self.sessions:
            raise KeyError(item.session_id)
        history = self.reads_by_session[item.session_id]
        history.append(item)
        self.reads_by_session[item.session_id] = history[-20:]
        return item

    def get_reads(self, session_id: str) -> list[ReadHistoryItem]:
        return self.reads_by_session.get(session_id, [])


store = SessionStore()
