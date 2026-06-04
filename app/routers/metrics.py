"""
GET /stores/{store_id}/metrics — real-time store metrics.
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")


@router.get("/{store_id}/metrics")
async def get_metrics(
    store_id: str,
    date: str = Query(None, description="YYYY-MM-DD. Defaults to today UTC.")
):
    """
    Returns: unique_visitors, conversion_rate, avg_dwell_per_zone,
             current_queue_depth, abandonment_rate.
    Excludes is_staff=1. Real-time — not cached.
    """
    try:
        db = await get_db()
    except Exception:
        raise HTTPException(status_code=503, detail={
            "error": "DATABASE_UNAVAILABLE",
            "message": "Storage backend unavailable."
        })

    date_filter = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        # Unique non-staff visitors (entry events)
        cur = await db.execute(
            """SELECT COUNT(DISTINCT COALESCE(id_token, CAST(track_id AS TEXT)))
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('entry','ENTRY')
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        unique_visitors = row[0] if row else 0

        # Conversion: visitors who completed a queue (queue_completed)
        cur = await db.execute(
            """SELECT COUNT(DISTINCT COALESCE(id_token, CAST(track_id AS TEXT)))
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('queue_completed', 'BILLING_QUEUE_JOIN')
                 AND abandoned=0
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        converters = row[0] if row else 0
        conversion_rate = round(converters / unique_visitors, 4) if unique_visitors > 0 else 0.0

        # Avg dwell per zone (zone_entered → zone_exited pairs)
        cur = await db.execute(
            """
            SELECT
                ze.zone_name,
                AVG(
                    CAST(
                        (julianday(zx.event_time) - julianday(ze.event_time)) * 86400
                    AS INTEGER)
                ) as avg_dwell_s,
                COUNT(*) as visits
            FROM raw_events ze
            JOIN raw_events zx ON
                zx.store_id = ze.store_id
                AND zx.event_type IN ('zone_exited','ZONE_EXIT')
                AND COALESCE(zx.id_token, CAST(zx.track_id AS TEXT))
                    = COALESCE(ze.id_token, CAST(ze.track_id AS TEXT))
                AND zx.zone_id = ze.zone_id
                AND zx.event_time > ze.event_time
                AND julianday(zx.event_time) - julianday(ze.event_time) < 0.1
            WHERE ze.store_id=?
              AND ze.event_type IN ('zone_entered','ZONE_ENTER')
              AND ze.is_staff=0
              AND date(ze.event_time)=?
            GROUP BY ze.zone_name
            """,
            (store_id, date_filter)
        )
        rows = await cur.fetchall()
        avg_dwell_per_zone = {
            r["zone_name"]: {"avg_dwell_seconds": round(r["avg_dwell_s"] or 0, 1),
                             "visit_count": r["visits"]}
            for r in rows
        }

        # Current queue depth (visitors in billing zone right now = joined - left)
        cur = await db.execute(
            """SELECT
                SUM(CASE WHEN event_type IN ('queue_completed','queue_abandoned') THEN -1 ELSE 1 END)
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('queue_completed','queue_abandoned','BILLING_QUEUE_JOIN')
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        queue_depth = max(0, row[0] or 0)

        # Abandonment rate
        cur = await db.execute(
            """SELECT
                COUNT(*) FILTER (WHERE abandoned=1) as abandoned,
                COUNT(*) as total
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('queue_abandoned','queue_completed','BILLING_QUEUE_ABANDON')
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        total_queue = row["total"] if row else 0
        abandonment_rate = round((row["abandoned"] or 0) / total_queue, 4) if total_queue > 0 else 0.0

        return {
            "store_id": store_id,
            "date": date_filter,
            "unique_visitors": unique_visitors,
            "conversion_rate": conversion_rate,
            "converters": converters,
            "avg_dwell_per_zone": avg_dwell_per_zone,
            "current_queue_depth": queue_depth,
            "abandonment_rate": abandonment_rate,
            "total_queue_joins": total_queue,
        }
    except Exception as e:
        logger.error(f'"metrics error store={store_id} err={str(e)}"')
        raise HTTPException(status_code=500, detail={"error": "COMPUTATION_ERROR", "message": str(e)})
    finally:
        await db.close()
