import logging
import time
from datetime import datetime, timezone, timedelta

from database import db_conn
from models import HealthResponse, StoreHealth

logger = logging.getLogger(__name__)

_startup_time = time.monotonic()

STALE_FEED_MINUTES = 10


def get_health() -> HealthResponse:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    uptime = time.monotonic() - _startup_time

    db_status = "ok"
    store_healths = []

    try:
        with db_conn() as conn:
            stores = conn.execute(
                "SELECT DISTINCT store_id FROM events"
            ).fetchall()

            for row in stores:
                sid = row["store_id"]
                last_row = conn.execute(
                    """
                    SELECT MAX(timestamp) as last_ts FROM events
                    WHERE store_id = ?
                    """,
                    (sid,)
                ).fetchone()

                last_ts_str = last_row["last_ts"] if last_row else None
                lag_seconds = None
                feed_warning = None
                status = "OK"

                if last_ts_str:
                    last_ts = datetime.strptime(last_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    lag_seconds = round((now - last_ts).total_seconds(), 1)
                    if lag_seconds > STALE_FEED_MINUTES * 60:
                        feed_warning = f"STALE_FEED — last event {int(lag_seconds // 60)}m ago"
                        status = "DEGRADED"
                else:
                    feed_warning = "NO_EVENTS_RECEIVED"
                    status = "DEGRADED"

                store_healths.append(StoreHealth(
                    store_id=sid,
                    status=status,
                    last_event_at=last_ts_str,
                    lag_seconds=lag_seconds,
                    feed_warning=feed_warning,
                ))

    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_status = f"error: {e}"

    overall = "OK" if db_status == "ok" else "DEGRADED"
    if any(s.status == "DOWN" for s in store_healths):
        overall = "DOWN"

    return HealthResponse(
        service="store-intelligence-api",
        status=overall,
        uptime_seconds=round(uptime, 1),
        database=db_status,
        stores=store_healths,
        checked_at=now_str,
    )
