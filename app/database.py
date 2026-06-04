"""
Database layer — aiosqlite backed SQLite (swap to Postgres by changing the DSN).
"""
import os
import aiosqlite
import logging

logger = logging.getLogger("store_intelligence")

DB_PATH = os.environ.get("DB_PATH", "/data/store_intelligence.db")

# Test override: set this to force a specific DB path regardless of env var
_db_path_override: str | None = None


def _get_db_path() -> str:
    if _db_path_override is not None:
        return _db_path_override
    return os.environ.get("DB_PATH", "/data/store_intelligence.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(_get_db_path())
    db.row_factory = aiosqlite.Row
    return db


async def init_db(path: str | None = None):
    target = path or _get_db_path()
    os.makedirs(os.path.dirname(target) if os.path.dirname(target) else ".", exist_ok=True)
    async with aiosqlite.connect(target) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS raw_events (
                event_id        TEXT PRIMARY KEY,
                store_id        TEXT NOT NULL,
                camera_id       TEXT,
                track_id        INTEGER,
                id_token        TEXT,
                event_type      TEXT NOT NULL,
                event_time      TEXT NOT NULL,
                zone_id         TEXT,
                zone_name       TEXT,
                zone_type       TEXT,
                is_staff        INTEGER DEFAULT 0,
                is_revenue_zone TEXT,
                gender          TEXT,
                age             INTEGER,
                age_bucket      TEXT,
                group_id        TEXT,
                group_size      INTEGER,
                queue_join_ts   TEXT,
                queue_served_ts TEXT,
                queue_exit_ts   TEXT,
                wait_seconds    INTEGER,
                queue_position  INTEGER,
                abandoned       INTEGER DEFAULT 0,
                zone_hotspot_x  REAL,
                zone_hotspot_y  REAL,
                raw_payload     TEXT,
                ingested_at     TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_store_time
                ON raw_events(store_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_event_type
                ON raw_events(event_type, store_id);
            CREATE INDEX IF NOT EXISTS idx_track
                ON raw_events(store_id, track_id);
            CREATE INDEX IF NOT EXISTS idx_id_token
                ON raw_events(store_id, id_token);

            CREATE TABLE IF NOT EXISTS pos_transactions (
                order_id        TEXT PRIMARY KEY,
                store_id        TEXT NOT NULL,
                product_id      TEXT,
                brand_name      TEXT,
                total_amount    REAL,
                order_ts        TEXT NOT NULL,
                ingested_at     TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pos_store_time
                ON pos_transactions(store_id, order_ts);
        """)
        await db.commit()
    logger.info('"Database initialised"')
