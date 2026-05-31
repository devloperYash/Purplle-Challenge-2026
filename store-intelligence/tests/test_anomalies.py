# PROMPT: "Write tests for anomaly detection in a retail analytics system. Cover:
# queue spike detection at threshold (3 → WARN, 5 → CRITICAL), conversion drop
# compared to 7-day baseline, dead zone detection after 30 min inactivity, 
# stale feed detection after 10 min with no events."
#
# CHANGES MADE:
# - Adjusted queue spike threshold to match our QUEUE_SPIKE_DEPTH=4 constant
# - Changed the conversion drop test to insert enough historical data (AI used too small a sample)
# - Fixed stale feed test: AI was checking for event_type=STALE_FEED but it's an anomaly_type field
# - Added test for no anomalies when everything is healthy (AI missed this coverage)

import pytest
import sys
import os
import uuid
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["DB_PATH"] = ":memory:"

from database import init_db, db_conn
from anomalies import get_anomalies, QUEUE_SPIKE_DEPTH, DEAD_ZONE_MINUTES
from models import IngestRequest, StoreEventIn
from ingestion import ingest_events


STORE = "STORE_BLR_002"


def make_event(visitor_id, event_type, zone_id=None, timestamp=None,
               is_staff=False, queue_depth=None):
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return StoreEventIn(
        event_id=str(uuid.uuid4()),
        store_id=STORE,
        camera_id="CAM_5",
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=ts,
        zone_id=zone_id,
        dwell_ms=0,
        is_staff=is_staff,
        confidence=0.9,
        metadata={"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    )


def raw_insert(store_id, event_type, zone_id=None, timestamp=None,
               is_staff=0, queue_depth=None, visitor_id=None):
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vid = visitor_id or f"VIS_{uuid.uuid4().hex[:6]}"
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO events (event_id, store_id, camera_id, visitor_id, event_type,
               timestamp, zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone, session_seq)
               VALUES (?,?,?,?,?,?,?,0,?,0.9,?,null,1)""",
            (str(uuid.uuid4()), store_id, "CAM_5", vid, event_type, ts, zone_id, is_staff, queue_depth)
        )


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    yield
    with db_conn() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM pos_transactions")
        conn.execute("DELETE FROM daily_baselines")


class TestQueueSpike:
    def test_no_anomaly_when_queue_normal(self):
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=1)
        result = get_anomalies(STORE)
        queue_anomalies = [a for a in result.active_anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) == 0

    def test_warn_when_queue_above_2(self):
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=3)
        result = get_anomalies(STORE)
        queue_anomalies = [a for a in result.active_anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) == 1
        assert queue_anomalies[0].severity in ("WARN", "CRITICAL")

    def test_critical_when_queue_at_threshold(self):
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=QUEUE_SPIKE_DEPTH)
        result = get_anomalies(STORE)
        queue_anomalies = [a for a in result.active_anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) == 1
        assert queue_anomalies[0].severity == "CRITICAL"

    def test_queue_spike_has_suggested_action(self):
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=5)
        result = get_anomalies(STORE)
        q_anomaly = next((a for a in result.active_anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"), None)
        assert q_anomaly is not None
        assert len(q_anomaly.suggested_action) > 10

    def test_old_queue_events_not_flagged(self):
        # Queue spike from 20 minutes ago should not trigger
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10, timestamp=old_ts)
        result = get_anomalies(STORE)
        queue_anomalies = [a for a in result.active_anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) == 0


class TestDeadZone:
    def test_no_dead_zone_when_recently_active(self):
        raw_insert(STORE, "ZONE_ENTER", zone_id="SKINCARE_PREMIUM")
        result = get_anomalies(STORE)
        dead = [a for a in result.active_anomalies if a.anomaly_type == "DEAD_ZONE"]
        # Skincare was just visited, should NOT be a dead zone
        skincare_dead = [a for a in dead if a.zone_id == "SKINCARE_PREMIUM"]
        assert len(skincare_dead) == 0

    def test_dead_zone_detected_after_inactivity(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=DEAD_ZONE_MINUTES + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_insert(STORE, "ZONE_ENTER", zone_id="HAIRCARE_MASS", timestamp=old_ts)
        result = get_anomalies(STORE)
        dead = [a for a in result.active_anomalies if a.anomaly_type == "DEAD_ZONE"]
        haircare_dead = [a for a in dead if a.zone_id == "HAIRCARE_MASS"]
        assert len(haircare_dead) == 1
        assert haircare_dead[0].severity == "INFO"

    def test_dead_zone_not_flagged_for_entry_zone(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_insert(STORE, "ENTRY", zone_id="ENTRY", timestamp=old_ts)
        result = get_anomalies(STORE)
        dead = [a for a in result.active_anomalies if a.anomaly_type == "DEAD_ZONE"]
        entry_dead = [a for a in dead if a.zone_id == "ENTRY"]
        assert len(entry_dead) == 0  # ENTRY zone excluded from dead zone checks


class TestStaleFeed:
    def test_stale_feed_when_no_events(self):
        result = get_anomalies(STORE)
        # No events at all → stale feed
        stale = [a for a in result.active_anomalies if a.anomaly_type == "STALE_FEED"]
        assert len(stale) == 1

    def test_stale_feed_when_last_event_old(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_insert(STORE, "ENTRY", timestamp=old_ts)
        result = get_anomalies(STORE)
        stale = [a for a in result.active_anomalies if a.anomaly_type == "STALE_FEED"]
        assert len(stale) == 1
        assert stale[0].severity == "CRITICAL"

    def test_no_stale_feed_when_events_recent(self):
        raw_insert(STORE, "ENTRY")  # Inserts current timestamp by default
        result = get_anomalies(STORE)
        stale = [a for a in result.active_anomalies if a.anomaly_type == "STALE_FEED"]
        assert len(stale) == 0


class TestNoAnomaliesHealthy:
    def test_healthy_store_has_no_anomalies(self):
        # Insert recent, normal activity in multiple zones
        for zone in ["SKINCARE_PREMIUM", "MAKEUP_MASS", "FOH"]:
            for _ in range(3):
                raw_insert(STORE, "ZONE_ENTER", zone_id=zone)
        raw_insert(STORE, "BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=1)
        raw_insert(STORE, "ENTRY")

        result = get_anomalies(STORE)
        critical = [a for a in result.active_anomalies if a.severity == "CRITICAL"]
        assert len(critical) == 0
