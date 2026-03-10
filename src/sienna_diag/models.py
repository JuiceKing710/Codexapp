from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    vehicle_id: str = "toyota_sienna_2006"
    vehicle: str = "2006 Toyota Sienna"
    protocol: str = "ISO_9141_2"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["active", "closed"] = "active"


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
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
