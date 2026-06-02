import sqlite3
import logging
import os
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/store_intelligence.db")

# Shared connection for in-memory DB (used by tests)
_shared_conn: sqlite3.Connection | None = None


def _is_memory_db() -> bool:
    return DB_PATH == ":memory:"


def _get_shared_conn() -> sqlite3.Connection:
    global _shared_conn
    if _shared_conn is None:
        _shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
        _shared_conn.row_factory = sqlite3.Row
        _shared_conn.execute("PRAGMA foreign_keys=ON")
    return _shared_conn


def reset_shared_conn():
    """Call this in test teardown to reset in-memory DB state."""
    global _shared_conn
    if _shared_conn is not None:
        _shared_conn.close()
        _shared_conn = None


def get_connection() -> sqlite3.Connection:
    if _is_memory_db():
        return _get_shared_conn()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if not _is_memory_db():
            conn.close()


def init_db():
    if not _is_memory_db():
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id     TEXT PRIMARY KEY,
                store_id     TEXT NOT NULL,
                camera_id    TEXT NOT NULL,
                visitor_id   TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                zone_id      TEXT,
                dwell_ms     INTEGER DEFAULT 0,
                is_staff     INTEGER NOT NULL,
                confidence   REAL NOT NULL,
                queue_depth  INTEGER,
                sku_zone     TEXT,
                session_seq  INTEGER DEFAULT 0,
                ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_store_ts
                ON events (store_id, timestamp);

            CREATE INDEX IF NOT EXISTS idx_events_visitor
                ON events (visitor_id);

            CREATE INDEX IF NOT EXISTS idx_events_type
                ON events (event_type);

            CREATE TABLE IF NOT EXISTS pos_transactions (
                transaction_id  TEXT PRIMARY KEY,
                store_id        TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                basket_value    REAL NOT NULL,
                visitor_id      TEXT,
                ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_pos_store_ts
                ON pos_transactions (store_id, timestamp);

            CREATE TABLE IF NOT EXISTS daily_baselines (
                store_id        TEXT NOT NULL,
                metric_date     TEXT NOT NULL,
                unique_visitors INTEGER,
                conversion_rate REAL,
                avg_dwell_sec   REAL,
                PRIMARY KEY (store_id, metric_date)
            );
        """)
    logger.info(f"Database initialized at {DB_PATH}")
