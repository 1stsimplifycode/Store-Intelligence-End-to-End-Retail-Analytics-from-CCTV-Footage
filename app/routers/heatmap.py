"""
GET /stores/{store_id}/heatmap — zone visit frequency + avg dwell, normalised 0–100.
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")


@router.get("/{store_id}/heatmap")
async def get_heatmap(
    store_id: str,
    date: str = Query(None, description="YYYY-MM-DD. Defaults to today UTC.")
):
    """
    Returns zone visit frequency and avg dwell, normalised 0–100.
    Includes data_confidence flag when session count < 20.
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
        # Session count
        cur = await db.execute(
            """SELECT COUNT(DISTINCT COALESCE(id_token, CAST(track_id AS TEXT)))
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('entry','ENTRY')
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        session_count = row[0] if row else 0
        low_confidence = session_count < 20

        # Zone dwell pairs
        cur = await db.execute(
            """
            SELECT
                ze.zone_id,
                ze.zone_name,
                ze.zone_type,
                COUNT(*) as visit_count,
                AVG(
                    CAST(
                        (julianday(zx.event_time) - julianday(ze.event_time)) * 86400
                    AS INTEGER)
                ) as avg_dwell_s,
                AVG(ze.zone_hotspot_x) as cx,
                AVG(ze.zone_hotspot_y) as cy
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
            GROUP BY ze.zone_id, ze.zone_name, ze.zone_type
            """,
            (store_id, date_filter)
        )
        rows = await cur.fetchall()

        if not rows:
            return {
                "store_id": store_id,
                "date": date_filter,
                "session_count": session_count,
                "data_confidence": "LOW" if low_confidence else "OK",
                "zones": [],
            }

        max_visits = max(r["visit_count"] for r in rows) or 1
        max_dwell = max(r["avg_dwell_s"] or 0 for r in rows) or 1

        zones = []
        for r in rows:
            visit_norm = round(r["visit_count"] / max_visits * 100)
            dwell_norm = round((r["avg_dwell_s"] or 0) / max_dwell * 100)
            zones.append({
                "zone_id": r["zone_id"],
                "zone_name": r["zone_name"],
                "zone_type": r["zone_type"],
                "visit_count": r["visit_count"],
                "avg_dwell_seconds": round(r["avg_dwell_s"] or 0, 1),
                "visit_frequency_score": visit_norm,
                "dwell_score": dwell_norm,
                "hotspot": {"x": round(r["cx"] or 0, 1), "y": round(r["cy"] or 0, 1)},
            })

        zones.sort(key=lambda z: z["visit_frequency_score"], reverse=True)

        return {
            "store_id": store_id,
            "date": date_filter,
            "session_count": session_count,
            "data_confidence": "LOW" if low_confidence else "OK",
            "zones": zones,
        }
    except Exception as e:
        logger.error(f'"heatmap error store={store_id} err={str(e)}"')
        raise HTTPException(status_code=500, detail={"error": "COMPUTATION_ERROR", "message": str(e)})
    finally:
        await db.close()
