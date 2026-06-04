"""
GET /health — service health, last event timestamp per store, STALE_FEED warning.
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")

STALE_THRESHOLD_MINUTES = 10


@router.get("/health")
async def health_check():
    """
    Returns: status, db connectivity, last event per store, STALE_FEED warnings.
    Used by on-call engineers — must be accurate.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(minutes=STALE_THRESHOLD_MINUTES)).isoformat()

    try:
        db = await get_db()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "DEGRADED",
                "error": "DATABASE_UNAVAILABLE",
                "message": "Cannot connect to database.",
                "checked_at": now.isoformat(),
            }
        )

    try:
        cur = await db.execute(
            """SELECT store_id, MAX(ingested_at) as last_ingested, MAX(event_time) as last_event
               FROM raw_events
               GROUP BY store_id
               ORDER BY last_event DESC"""
        )
        rows = await cur.fetchall()

        cur2 = await db.execute("SELECT COUNT(*) FROM raw_events")
        total_row = await cur2.fetchone()
        total_events = total_row[0] if total_row else 0

        stores = []
        warnings = []
        for r in rows:
            is_stale = r["last_ingested"] < stale_cutoff if r["last_ingested"] else True
            store_entry = {
                "store_id": r["store_id"],
                "last_event_time": r["last_event"],
                "last_ingested_at": r["last_ingested"],
                "stale": is_stale,
            }
            stores.append(store_entry)
            if is_stale:
                warnings.append({
                    "type": "STALE_FEED",
                    "store_id": r["store_id"],
                    "last_seen": r["last_ingested"],
                    "message": f"No events ingested from {r['store_id']} in the last {STALE_THRESHOLD_MINUTES} minutes.",
                })

        status = "DEGRADED" if warnings else "OK"
        return {
            "status": status,
            "checked_at": now.isoformat(),
            "total_events_ingested": total_events,
            "stores": stores,
            "warnings": warnings,
        }
    except Exception as e:
        logger.error(f'"health check error: {str(e)}"')
        return JSONResponse(
            status_code=503,
            content={
                "status": "DEGRADED",
                "error": "HEALTH_CHECK_FAILED",
                "message": str(e),
                "checked_at": now.isoformat(),
            }
        )
    finally:
        await db.close()
