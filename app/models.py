"""
Pydantic event models — designed to accept the actual sample_events.jsonl schema
from the Purplle dataset, which differs slightly from the PS spec.
Both the PS-spec schema and the actual observed schema are supported.
"""
from __future__ import annotations
import uuid
from typing import Optional, Any
from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from enum import Enum


class EventType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"
    ZONE_ENTER = "ZONE_ENTER"       # PS-spec variant
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    QUEUE_COMPLETED = "queue_completed"
    QUEUE_ABANDONED = "queue_abandoned"
    REENTRY = "REENTRY"


class RawEvent(BaseModel):
    """Flexible intake model that accepts both schema variants."""
    model_config = {"extra": "allow"}

    # Shared required
    event_type: str

    # entry/exit schema (sample_events.jsonl actual)
    id_token: Optional[str] = None
    store_code: Optional[str] = None
    camera_id: Optional[str] = None
    event_timestamp: Optional[str] = None
    is_staff: Optional[bool] = False
    gender_pred: Optional[str] = None
    age_pred: Optional[int] = None
    age_bucket: Optional[str] = None
    is_face_hidden: Optional[bool] = False
    group_id: Optional[str] = None
    group_size: Optional[int] = None

    # zone schema
    track_id: Optional[int] = None
    store_id: Optional[str] = None
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    zone_type: Optional[str] = None
    is_revenue_zone: Optional[str] = None
    event_time: Optional[str] = None
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None

    # queue schema
    queue_event_id: Optional[str] = None
    queue_join_ts: Optional[str] = None
    queue_served_ts: Optional[str] = None
    queue_exit_ts: Optional[str] = None
    wait_seconds: Optional[int] = None
    queue_position_at_join: Optional[int] = None
    abandoned: Optional[bool] = None

    # PS-spec fields (optional, for future compatibility)
    event_id: Optional[str] = None
    visitor_id: Optional[str] = None
    dwell_ms: Optional[int] = None
    confidence: Optional[float] = None

    @model_validator(mode="after")
    def normalise(self) -> "RawEvent":
        # Resolve store identifier
        if not self.store_id and self.store_code:
            self.store_id = self.store_code
        # Resolve timestamp
        if not self.event_time and self.event_timestamp:
            self.event_time = self.event_timestamp
        if not self.event_time and self.queue_join_ts:
            self.event_time = self.queue_join_ts
        # Resolve gender/age
        if not self.gender and self.gender_pred:
            self.gender = self.gender_pred
        if not self.age and self.age_pred:
            self.age = self.age_pred
        # Ensure event_id
        if not self.event_id:
            self.event_id = self.queue_event_id or str(uuid.uuid4())
        return self


class IngestRequest(BaseModel):
    events: list[RawEvent] = Field(..., max_length=500)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: list[dict[str, Any]] = []


class POSTransaction(BaseModel):
    order_id: str
    order_date: Optional[str] = None
    order_time: Optional[str] = None
    store_id: str
    product_id: Optional[str] = None
    brand_name: Optional[str] = None
    total_amount: float

    @model_validator(mode="after")
    def build_ts(self) -> "POSTransaction":
        if not hasattr(self, "order_ts") or not self.order_ts:
            d = self.order_date or "2026-01-01"
            t = self.order_time or "00:00:00"
            object.__setattr__(self, "order_ts", f"{d}T{t}")
        return self

    order_ts: Optional[str] = None
