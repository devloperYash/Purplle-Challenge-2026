import uuid
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


@dataclass
class StoreEvent:
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    is_staff: bool
    confidence: float
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    metadata: EventMetadata = field(default_factory=EventMetadata)

    def __post_init__(self):
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event_type: {self.event_type}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be 0.0-1.0, got {self.confidence}")
        self.is_staff = bool(self.is_staff)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = asdict(self.metadata)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


class EventEmitter:
    def __init__(self, output_path: str, store_id: str):
        self.output_path = Path(output_path)
        self.store_id = store_id
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.output_path, "a", buffering=1)
        self._count = 0

    def emit(self, event: StoreEvent) -> None:
        line = event.to_json()
        self._file.write(line + "\n")
        self._count += 1
        if self._count % 50 == 0:
            logger.info(f"Emitted {self._count} events so far")

    def close(self):
        self._file.flush()
        self._file.close()
        logger.info(f"Done. Total events written: {self._count}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def make_timestamp(clip_start: datetime, frame_offset_seconds: float) -> str:
    from datetime import timedelta
    ts = clip_start + timedelta(seconds=frame_offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_events_from_file(path: str) -> list[dict]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping bad line: {e}")
    return events
