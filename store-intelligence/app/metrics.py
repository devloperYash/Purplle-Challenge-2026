import logging
from datetime import datetime, timezone, timedelta
from database import db_conn
from models import MetricsResponse, ZoneDwellStat

logger = logging.getLogger(__name__)

# POS correlation window: visitor in billing zone within 5 minutes before a transaction
POS_CORRELATION_WINDOW_MINUTES = 5


def get_store_metrics(store_id: str, window_hours: int = 9999) -> MetricsResponse:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    ws = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    we = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    with db_conn() as conn:
        # Unique customer visitors — aligned with relaxed funnel Stage 1: Entered Store
        unique_visitors = conn.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = ?
              AND event_type IN ('ENTRY', 'ZONE_ENTER', 'ZONE_DWELL')
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()["cnt"]

        total_entries = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM events
            WHERE store_id = ?
              AND event_type IN ('ENTRY', 'REENTRY')
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()["cnt"]

        # NAYA — time based conversion, cross-camera safe
        # Har unique non-staff visitor jo BILLING zone mein gaya = converted
        converted_visitors = conn.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = ?
              AND zone_id = 'BILLING'
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()["cnt"]

        # Unique visitors = max of ENTRY count or all unique visitors seen
        # Cap conversion at unique_visitors to avoid >100%
        converted_visitors = min(converted_visitors, unique_visitors)

        # Also try POS correlation if POS data exists in the same window
        billing_sessions = conn.execute(
            """
            SELECT DISTINCT visitor_id, timestamp
            FROM events
            WHERE store_id = ?
              AND zone_id = 'BILLING'
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchall()

        # Fetch ALL POS transactions (not time-filtered) for cross-correlation
        pos_transactions = conn.execute(
            """
            SELECT timestamp, basket_value
            FROM pos_transactions
            WHERE store_id = ?
            ORDER BY timestamp
            """,
            (store_id,)
        ).fetchall()

        if pos_transactions:
            pos_converted = _count_conversions(billing_sessions, pos_transactions)
            # Use POS-based count if higher (more accurate when data aligns)
            if pos_converted > converted_visitors:
                converted_visitors = pos_converted

        # Conversion rate cap at 100%
        conversion_rate = min(
            (converted_visitors / unique_visitors) if unique_visitors > 0 else 0.0,
            1.0
        )

        # Average dwell across all non-entry zones
        avg_dwell_row = conn.execute(
            """
            SELECT AVG(dwell_ms) as avg_dwell
            FROM events
            WHERE store_id = ?
              AND event_type IN ('ZONE_DWELL', 'ZONE_EXIT')
              AND dwell_ms > 0
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()
        avg_dwell_ms = avg_dwell_row["avg_dwell"] or 0.0

        # Per-zone dwell stats
        zone_rows = conn.execute(
            """
            SELECT zone_id,
                   AVG(dwell_ms) as avg_dwell,
                   COUNT(DISTINCT visitor_id) as visit_count
            FROM events
            WHERE store_id = ?
              AND zone_id IS NOT NULL
              AND dwell_ms > 0
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            GROUP BY zone_id
            ORDER BY avg_dwell DESC
            """,
            (store_id, ws, we)
        ).fetchall()

        zone_dwell = [
            ZoneDwellStat(
                zone_id=row["zone_id"],
                avg_dwell_seconds=round((row["avg_dwell"] or 0) / 1000, 1),
                visit_count=row["visit_count"]
            )
            for row in zone_rows
        ]

        # Current billing queue depth
        recent_cutoff = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        billing_joins = conn.execute(
            """
            SELECT queue_depth FROM events
            WHERE store_id = ?
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (store_id, recent_cutoff)
        ).fetchone()
        current_queue_depth = billing_joins["queue_depth"] if billing_joins and billing_joins["queue_depth"] else 0

        # Abandonment rate
        abandons = conn.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id = ?
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()["cnt"]

        queue_joins = conn.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id = ?
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            """,
            (store_id, ws, we)
        ).fetchone()["cnt"]

        abandonment_rate = (abandons / queue_joins) if queue_joins > 0 else 0.0

        data_confidence = "HIGH" if unique_visitors >= 20 else ("MEDIUM" if unique_visitors >= 5 else "LOW")

    return MetricsResponse(
        store_id=store_id,
        window_start=ws,
        window_end=we,
        unique_visitors=unique_visitors,
        total_entries=total_entries,
        converted_visitors=converted_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_seconds=round(avg_dwell_ms / 1000, 1),
        zone_dwell=zone_dwell,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        data_confidence=data_confidence,
    )


def _count_conversions(billing_sessions, pos_transactions) -> int:
    """
    Count unique visitors who converted.
    A visitor converts if they were in the billing zone within 5 minutes before a POS transaction.
    Each POS transaction can convert at most one visitor to avoid double counting.
    """
    converted = set()
    window = POS_CORRELATION_WINDOW_MINUTES * 60

    for txn in pos_transactions:
        txn_ts = _parse_ts(txn["timestamp"])
        for session in billing_sessions:
            if session["visitor_id"] in converted:
                continue
            billing_ts = _parse_ts(session["timestamp"])
            # Visitor must have been in billing zone in the window before the transaction
            if 0 <= (txn_ts - billing_ts).total_seconds() <= window:
                converted.add(session["visitor_id"])
                break

    return len(converted)


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
