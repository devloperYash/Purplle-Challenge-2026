from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal
from datetime import datetime
import uuid


VALID_EVENT_TYPES = Literal[
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
]


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEventIn(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: VALID_EVENT_TYPES
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"timestamp must be ISO-8601 UTC: {v}")
        return v

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError(f"event_id must be a valid UUID: {v}")
        return v

    @model_validator(mode="after")
    def check_zone_required(self):
        zone_required = {"ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"}
        if self.event_type in zone_required and not self.zone_id:
            raise ValueError(f"zone_id is required for event_type={self.event_type}")
        return self


class IngestRequest(BaseModel):
    events: list[StoreEventIn] = Field(max_length=500)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicates: int
    errors: list[dict]


class ZoneDwellStat(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    window_start: str
    window_end: str
    unique_visitors: int
    total_entries: int
    converted_visitors: int
    conversion_rate: float
    avg_dwell_seconds: float
    zone_dwell: list[ZoneDwellStat]
    current_queue_depth: int
    abandonment_rate: float
    data_confidence: str


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    session_window_hours: int


class HeatmapZone(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_count: int
    avg_dwell_seconds: float
    normalized_score: float


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone]
    data_confidence: str


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: str
    severity: Literal["INFO", "WARN", "CRITICAL"]
    description: str
    suggested_action: str
    detected_at: str
    zone_id: Optional[str] = None
    metric_value: Optional[float] = None
    threshold: Optional[float] = None


class AnomalyResponse(BaseModel):
    store_id: str
    active_anomalies: list[Anomaly]
    checked_at: str


class StoreHealth(BaseModel):
    store_id: str
    status: Literal["OK", "DEGRADED", "DOWN"]
    last_event_at: Optional[str]
    lag_seconds: Optional[float]
    feed_warning: Optional[str]


class HealthResponse(BaseModel):
    service: str
    status: str
    uptime_seconds: float
    database: str
    stores: list[StoreHealth]
    checked_at: str
