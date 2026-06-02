import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from database import db_conn
from models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)


def get_conversion_funnel(store_id: str, window_hours: int = 9999) -> FunnelResponse:
    """
    Build a session-based conversion funnel.
    Unit of analysis is a visitor session, not raw events.
    Re-entries don't create new unique sessions for the same visitor.

    Stages:
      1. Entered store
      2. Visited at least one product zone
      3. Entered billing queue (or billing zone)
      4. Completed a purchase (POS correlation)
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    ws = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    we = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    with db_conn() as conn:
        # All customer events in window
        rows = conn.execute(
            """
            SELECT visitor_id, event_type, zone_id, timestamp
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND timestamp BETWEEN ? AND ?
            ORDER BY visitor_id, timestamp
            """,
            (store_id, ws, we)
        ).fetchall()

        pos_rows = conn.execute(
            """
            SELECT timestamp FROM pos_transactions
            WHERE store_id = ?
            ORDER BY timestamp
            """,
            (store_id,)
        ).fetchall()
        pos_timestamps = [_parse_ts(r["timestamp"]) for r in pos_rows]

    # Group events by visitor — deduplicated by visitor_id (not by track)
    visitor_events: dict[str, list] = defaultdict(list)
    for row in rows:
        if row["event_type"] not in ("REENTRY",):
            visitor_events[row["visitor_id"]].append(dict(row))

    product_zones = {
        "SKINCARE_PREMIUM", "MAKEUP_MASS", "HAIRCARE_MASS",
        "MAKEUP_UNIT", "FRAGRANCE", "NAIL_UNIT", "MENS_CARE"
    }

    stage_1_entered = set()
    stage_2_zone_visit = set()
    stage_3_billing = set()
    stage_4_purchased = set()

    billing_zone_timestamps: dict[str, list[datetime]] = defaultdict(list)

    for vid, events in visitor_events.items():
        event_types = {e["event_type"] for e in events}
        zone_ids = {e["zone_id"] for e in events if e["zone_id"]}

        # Stage 1: Entered store
        # NAYA — koi bhi visitor jo store mein dikha, counted:
        has_entry = (
            "ENTRY" in event_types or
            "ZONE_ENTER" in event_types or
            "ZONE_DWELL" in event_types
        )

        if has_entry:
            stage_1_entered.add(vid)

            # NAYA Stage 2 — any product zone visit:
            product_zone_visited = any(
                e["zone_id"] in product_zones
                for e in events
                if e["zone_id"]
            )
            if product_zone_visited:
                stage_2_zone_visit.add(vid)

            if "BILLING" in zone_ids or "BILLING_QUEUE_JOIN" in event_types:
                stage_3_billing.add(vid)

                for e in events:
                    if e["zone_id"] == "BILLING" or e["event_type"] == "BILLING_QUEUE_JOIN":
                        billing_zone_timestamps[vid].append(_parse_ts(e["timestamp"]))


    # Stage 4: POS correlation if data available, else use billing stage as proxy
    # (POS CSV timestamps may be historical and not match live event timestamps)
    if pos_timestamps:
        used_pos = set()
        for vid, ts_list in billing_zone_timestamps.items():
            for billing_ts in ts_list:
                for i, pos_ts in enumerate(pos_timestamps):
                    if i in used_pos:
                        continue
                    delta = (pos_ts - billing_ts).total_seconds()
                    if 0 <= delta <= 300:  # 5 minute window
                        stage_4_purchased.add(vid)
                        used_pos.add(i)
                        break
        # If POS correlation yields 0 (timestamp mismatch), fall back to billing proxy
        if not stage_4_purchased:
            stage_4_purchased = stage_3_billing.copy()
    else:
        # No POS data at all — use billing stage as purchase proxy
        stage_4_purchased = stage_3_billing.copy()


    # Build funnel stages with drop-off
    n1 = len(stage_1_entered)
    n2 = len(stage_2_zone_visit)
    n3 = len(stage_3_billing)
    n4 = len(stage_4_purchased)

    def drop_off(current, previous):
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 1)

    stages = [
        FunnelStage(stage="ENTRY", count=n1, drop_off_pct=0.0),
        FunnelStage(stage="ZONE_VISIT", count=n2, drop_off_pct=drop_off(n2, n1)),
        FunnelStage(stage="BILLING_QUEUE", count=n3, drop_off_pct=drop_off(n3, n2)),
        FunnelStage(stage="REACHED_BILLING", count=n4, drop_off_pct=drop_off(n4, n3)),
    ]

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        session_window_hours=window_hours,
    )


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
