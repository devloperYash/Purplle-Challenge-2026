import logging
import uuid
from datetime import datetime, timezone, timedelta
from database import db_conn
from models import AnomalyResponse, Anomaly

logger = logging.getLogger(__name__)

# Thresholds
QUEUE_SPIKE_DEPTH = 4           # Queue depth above this is CRITICAL
CONVERSION_DROP_THRESHOLD = 0.3 # 30% drop from baseline is WARN
DEAD_ZONE_MINUTES = 30          # No visits in 30 min = dead zone anomaly
STALE_FEED_MINUTES = 10         # No events in 10 min = stale feed


def get_anomalies(store_id: str) -> AnomalyResponse:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    anomalies = []

    with db_conn() as conn:
        anomalies += _check_queue_spike(conn, store_id, now, now_str)
        anomalies += _check_conversion_drop(conn, store_id, now, now_str)
        anomalies += _check_dead_zones(conn, store_id, now, now_str)
        anomalies += _check_stale_feed(conn, store_id, now, now_str)

    return AnomalyResponse(
        store_id=store_id,
        active_anomalies=anomalies,
        checked_at=now_str,
    )


def _check_queue_spike(conn, store_id, now, now_str) -> list[Anomaly]:
    recent_cutoff = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        """
        SELECT MAX(queue_depth) as max_depth FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= ?
        """,
        (store_id, recent_cutoff)
    ).fetchone()

    if not row or not row["max_depth"]:
        return []

    depth = row["max_depth"]
    if depth < 2:
        return []

    severity = "CRITICAL" if depth >= QUEUE_SPIKE_DEPTH else "WARN"
    return [Anomaly(
        anomaly_id=str(uuid.uuid4()),
        anomaly_type="BILLING_QUEUE_SPIKE",
        severity=severity,
        description=f"Billing queue depth reached {depth} in the last 10 minutes.",
        suggested_action="Open an additional checkout counter or redirect staff to billing.",
        detected_at=now_str,
        zone_id="BILLING",
        metric_value=float(depth),
        threshold=float(QUEUE_SPIKE_DEPTH),
    )]


def _check_conversion_drop(conn, store_id, now, now_str) -> list[Anomaly]:
    today_start = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    today_entries = conn.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = ? AND event_type = 'ENTRY' AND is_staff = 0
          AND timestamp >= ?
        """,
        (store_id, today_start)
    ).fetchone()[0]

    if today_entries < 5:
        return []

    today_billing = conn.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = ? AND zone_id = 'BILLING' AND is_staff = 0
          AND timestamp >= ?
        """,
        (store_id, today_start)
    ).fetchone()[0]

    today_rate = today_billing / today_entries if today_entries > 0 else 0.0

    # Compare against 7-day baseline if available
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hist_entries = conn.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = ? AND event_type = 'ENTRY' AND is_staff = 0
          AND timestamp BETWEEN ? AND ?
        """,
        (store_id, seven_days_ago, today_start)
    ).fetchone()[0]

    hist_billing = conn.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = ? AND zone_id = 'BILLING' AND is_staff = 0
          AND timestamp BETWEEN ? AND ?
        """,
        (store_id, seven_days_ago, today_start)
    ).fetchone()[0]

    if hist_entries < 10:
        # Not enough historical data for comparison
        return []

    baseline_rate = hist_billing / hist_entries
    if baseline_rate == 0:
        return []

    drop = (baseline_rate - today_rate) / baseline_rate
    if drop < CONVERSION_DROP_THRESHOLD:
        return []

    severity = "CRITICAL" if drop > 0.5 else "WARN"
    return [Anomaly(
        anomaly_id=str(uuid.uuid4()),
        anomaly_type="CONVERSION_DROP",
        severity=severity,
        description=f"Conversion rate {today_rate:.1%} is {drop:.1%} below 7-day average {baseline_rate:.1%}.",
        suggested_action="Check staff availability, review promotions, inspect billing area for friction.",
        detected_at=now_str,
        metric_value=round(today_rate, 4),
        threshold=round(baseline_rate, 4),
    )]


def _check_dead_zones(conn, store_id, now, now_str) -> list[Anomaly]:
    cutoff = (now - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Get zones that had visits before but haven't seen any recently
    all_zones = conn.execute(
        """
        SELECT DISTINCT zone_id FROM events
        WHERE store_id = ? AND zone_id IS NOT NULL AND is_staff = 0
          AND zone_id NOT IN ('ENTRY', 'BILLING', 'ASSIST')
        """,
        (store_id,)
    ).fetchall()

    dead_zones = []
    for row in all_zones:
        zone_id = row["zone_id"]
        recent = conn.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE store_id = ? AND zone_id = ? AND is_staff = 0
              AND timestamp >= ?
            """,
            (store_id, zone_id, cutoff)
        ).fetchone()[0]

        if recent == 0:
            dead_zones.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type="DEAD_ZONE",
                severity="INFO",
                description=f"No customer activity in zone {zone_id} for {DEAD_ZONE_MINUTES}+ minutes.",
                suggested_action=f"Check camera feed for {zone_id}, or consider relocating staff to engage customers.",
                detected_at=now_str,
                zone_id=zone_id,
                metric_value=float(DEAD_ZONE_MINUTES),
            ))

    return dead_zones


def _check_stale_feed(conn, store_id, now, now_str) -> list[Anomaly]:
    cutoff = (now - timedelta(minutes=STALE_FEED_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE store_id = ? AND timestamp >= ?
        """,
        (store_id, cutoff)
    ).fetchone()[0]

    if recent > 0:
        return []

    last_event = conn.execute(
        """
        SELECT MAX(timestamp) as last_ts FROM events WHERE store_id = ?
        """,
        (store_id,)
    ).fetchone()["last_ts"]

    return [Anomaly(
        anomaly_id=str(uuid.uuid4()),
        anomaly_type="STALE_FEED",
        severity="CRITICAL",
        description=f"No events received for {STALE_FEED_MINUTES}+ minutes. Last event: {last_event or 'never'}.",
        suggested_action="Check detection pipeline is running. Inspect camera connections for store.",
        detected_at=now_str,
        zone_id=None,
    )]
