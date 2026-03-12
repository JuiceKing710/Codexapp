from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


SourceMode = Literal["MOCK", "PHONE-LIVE", "LOCAL-HARDWARE", "BROWSER-DEV"]


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    vehicle_id: str = "toyota_sienna_2006"
    vehicle: str = "2006 Toyota Sienna"
    protocol: str = "ISO_9141_2"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["active", "closed"] = "active"
    assignment_source: Literal["auto_vin", "manual"] = "manual"
    vin: str | None = None


class SessionCreateRequest(BaseModel):
    vehicle_id: str | None = None


class VehicleProfile(BaseModel):
    vehicle_id: str
    label: str
    protocol_hint: str
    notes: str | None = None


class EventTag(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tag: str
    note: str | None = None


class OBDReadRequest(BaseModel):
    session_id: str | None = None
    command: str
    source_hint: Literal["iso9141_2", "can_capture"] = "iso9141_2"


class OBDReadResponse(BaseModel):
    session_id: str
    command: str
    raw_response: str
    parsed: dict


class ReadHistoryItem(BaseModel):
    read_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    vehicle: str
    command: str
    raw_response: str
    source_mode: SourceMode = "MOCK"
    pid_key: str | None = None
    value: float | str | None = None
    unit: str | None = None
    raw_command: str | None = None
    vehicle_id: str | None = None
    parsed_value: float | str | None = None
    polling: bool = False
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PhoneLiveReadPayload(BaseModel):
    session_id: str | None = None
    vehicle_id: str | None = None
    command: str
    raw_response: str
    pid_key: str
    value: float | str | None = None
    unit: str | None = None
    source_mode: Literal["PHONE-LIVE", "BROWSER-DEV"] = "PHONE-LIVE"
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_hint: Literal["iso9141_2", "can_capture"] = "iso9141_2"
    latency_ms: int | None = None
    backend_status: str | None = None
    error: str | None = None
    polling: bool = False


class PhoneBridgeConnectPayload(BaseModel):
    platform: Literal["ios", "android", "ipad", "browser", "unknown"] = "unknown"
    adapter_name: str = "OBDLink MX+"
    status: Literal["connecting", "connected", "failed"] = "connecting"
    permission_state: str | None = None
    source_mode: Literal["PHONE-LIVE", "BROWSER-DEV"] = "PHONE-LIVE"
    supports_native_bluetooth: bool = True
    fallback_reason: str | None = None


class CaptureStartRequest(BaseModel):
    vehicle_id: str | None = None
    preset: str = "cold_start_capture"


class CaptureTagRequest(BaseModel):
    tag: str
    note: str | None = None


class ReviewRequest(BaseModel):
    session_id: str
    summary: str


class PreprocessRequest(BaseModel):
    input_path: str
    output_dir: str


class OpenAISecondOpinion(BaseModel):
    risk_level: Literal["low", "medium", "high"]
    key_findings: list[str]
    recommended_next_read_only_steps: list[str]
    prohibited_actions_confirmed: list[str]


RiskClassification = Literal["safe", "unknown", "risky", "confirmed"]
CommandSourceType = Literal["can_passive", "obd_request_response", "replay"]


class CommandLearningRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    vehicle_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_type: CommandSourceType
    raw_command: str
    raw_response: str | None = None
    parsed_response: dict[str, Any] | None = None
    protocol: str | None = None
    mode: SourceMode
    before_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    after_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    confidence_score: float | None = None
    risk_classification: RiskClassification = "unknown"
    manually_approved_for_replay: bool = False
    replay_succeeded: bool | None = None
    notes: str | None = None


class CommandLearningIngestRequest(BaseModel):
    session_id: str | None = None
    vehicle_id: str | None = None
    source_type: CommandSourceType
    raw_command: str
    raw_response: str | None = None
    parsed_response: dict[str, Any] | None = None
    protocol: str | None = None
    mode: SourceMode = "PHONE-LIVE"
    before_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    after_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    confidence_score: float | None = None
    risk_classification: RiskClassification = "unknown"
    manually_approved_for_replay: bool = False
    replay_succeeded: bool | None = None
    notes: str | None = None


class ReplayApprovalRequest(BaseModel):
    session_id: str
    raw_command: str
    approve: bool
    notes: str | None = None


class ReplayExecuteRequest(BaseModel):
    session_id: str
    raw_command: str
    confirm_risky: bool = False
    before_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    after_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None


TimelineEventType = Literal[
    "vehicle_connected",
    "vehicle_disconnected",
    "vin_detected",
    "manual_vehicle_selected",
    "vehicle_check_started",
    "vehicle_check_stopped",
    "capture_started",
    "capture_stopped",
    "dtc_detected",
    "user_tag_event",
    "sensor_change",
    "ai_alert_created",
    "ai_advice_generated",
    "replay_approval_action",
    "connection_issue",
]


class DiagnosticTimelineEvent(BaseModel):
    timeline_event_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    event_type: TimelineEventType
    title: str
    detail: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Literal["system", "user", "ai"] = "system"
    related_codes: list[str] = Field(default_factory=list)
    related_sensors: list[str] = Field(default_factory=list)
    linked_ai_response_id: str | None = None
    linked_ai_alert_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AIAlertRecord(BaseModel):
    alert_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    vehicle_id: str
    title: str
    explanation: str
    trigger_reason: str
    confidence: Literal["monitor only", "suspected", "likely", "high confidence"]
    suggested_next_step: str
    related_sensors: list[str] = Field(default_factory=list)
    related_codes: list[str] = Field(default_factory=list)
    proactive: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AIResponseRecord(BaseModel):
    response_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    vehicle_id: str
    question: str
    answer: str
    response_basis: Literal["live_data", "stored_history", "general_knowledge"]
    used_live_data: bool = False
    proactive: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    context_summary: dict[str, Any] = Field(default_factory=dict)


class AIMechanicQuestionRequest(BaseModel):
    question: str = ""
    memory_updates: dict[str, Any] = Field(default_factory=dict)
    source: Literal["user", "system"] = "user"
