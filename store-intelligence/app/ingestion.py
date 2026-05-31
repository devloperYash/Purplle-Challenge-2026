import logging
from datetime import datetime, timezone

from database import db_conn
from models import IngestRequest, IngestResponse, StoreEventIn

logger = logging.getLogger(__name__)


def ingest_events(request: IngestRequest, trace_id: str) -> IngestResponse:
    accepted = 0
    rejected = 0
    duplicates = 0
    errors = []

    valid_events = []

    for i, evt in enumerate(request.events):
        try:
            valid_events.append(evt)
        except Exception as e:
            rejected += 1
            errors.append({"index": i, "event_id": getattr(evt, "event_id", None), "error": str(e)})

    if not valid_events:
        return IngestResponse(accepted=0, rejected=rejected, duplicates=duplicates, errors=errors)

    with db_conn() as conn:
        for evt in valid_events:
            try:
                existing = conn.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (evt.event_id,)
                ).fetchone()

                if existing:
                    duplicates += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, store_id, camera_id, visitor_id, event_type,
                        timestamp, zone_id, dwell_ms, is_staff, confidence,
                        queue_depth, sku_zone, session_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evt.event_id,
                        evt.store_id,
                        evt.camera_id,
                        evt.visitor_id,
                        evt.event_type,
                        evt.timestamp,
                        evt.zone_id,
                        evt.dwell_ms,
                        1 if evt.is_staff else 0,
                        evt.confidence,
                        evt.metadata.queue_depth,
                        evt.metadata.sku_zone,
                        evt.metadata.session_seq,
                    )
                )
                accepted += 1

            except Exception as e:
                logger.error(f"[{trace_id}] Failed to insert event {evt.event_id}: {e}")
                rejected += 1
                errors.append({"event_id": evt.event_id, "error": str(e)})

    logger.info(
        f"[{trace_id}] Ingest complete — accepted={accepted} rejected={rejected} "
        f"duplicates={duplicates} errors={len(errors)}"
    )
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicates,
        errors=errors,
    )


def load_pos_transactions_from_csv(csv_path: str, store_id: str = "STORE_BLR_002"):
    """
    Load the real POS data from the Brigade Bangalore CSV into the database.
    Converts the actual transaction format to match our schema.
    """
    import csv
    from datetime import datetime

    loaded = 0
    skipped = 0

    with db_conn() as conn:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            seen_invoices = set()

            for row in reader:
                invoice = row.get("invoice_number", "").strip()
                if not invoice or invoice in seen_invoices:
                    skipped += 1
                    continue
                seen_invoices.add(invoice)

                try:
                    order_date = row["order_date"].strip()
                    order_time = row["order_time"].strip()
                    dt = datetime.strptime(f"{order_date} {order_time}", "%d-%m-%Y %H:%M:%S")
                    ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, KeyError):
                    skipped += 1
                    continue

                try:
                    amount = float(row.get("total_amount", 0) or 0)
                except (ValueError, TypeError):
                    amount = 0.0

                existing = conn.execute(
                    "SELECT 1 FROM pos_transactions WHERE transaction_id = ?", (invoice,)
                ).fetchone()

                if existing:
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
                    VALUES (?, ?, ?, ?)
                    """,
                    (invoice, store_id, ts, amount)
                )
                loaded += 1

    logger.info(f"POS data loaded: {loaded} transactions, {skipped} skipped")
    return loaded
