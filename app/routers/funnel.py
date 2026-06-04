"""
GET /stores/{store_id}/funnel — conversion funnel with session deduplication.
Entry → Zone Visit → Billing Queue → Purchase
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")


@router.get("/{store_id}/funnel")
async def get_funnel(
    store_id: str,
    date: str = Query(None, description="YYYY-MM-DD. Defaults to today UTC.")
):
    """
    Conversion funnel. Session is the unit — re-entries do NOT
    double-count a visitor (counted once per unique id_token/track_id).
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
        def _id(row_alias: str) -> str:
            return f"COALESCE({row_alias}.id_token, CAST({row_alias}.track_id AS TEXT))"

        # Stage 1: unique entrants (non-staff, de-duped by visitor)
        cur = await db.execute(
            f"""SELECT COUNT(DISTINCT {_id('e')})
                FROM raw_events e
                WHERE e.store_id=? AND e.is_staff=0
                  AND e.event_type IN ('entry','ENTRY')
                  AND date(e.event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        stage_entry = row[0] if row else 0

        # Stage 2: visitors who entered at least one zone
        cur = await db.execute(
            f"""SELECT COUNT(DISTINCT {_id('z')})
                FROM raw_events z
                WHERE z.store_id=? AND z.is_staff=0
                  AND z.event_type IN ('zone_entered','ZONE_ENTER')
                  AND date(z.event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        stage_zone = row[0] if row else 0

        # Stage 3: visitors who joined billing queue (or entered billing zone)
        cur = await db.execute(
            f"""SELECT COUNT(DISTINCT {_id('q')})
                FROM raw_events q
                WHERE q.store_id=? AND q.is_staff=0
                  AND q.event_type IN ('queue_completed','queue_abandoned',
                                       'BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON')
                  AND date(q.event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        stage_billing = row[0] if row else 0

        # Stage 4: actual purchasers (queue_completed with abandoned=0)
        cur = await db.execute(
            f"""SELECT COUNT(DISTINCT {_id('p')})
                FROM raw_events p
                WHERE p.store_id=? AND p.is_staff=0
                  AND p.event_type IN ('queue_completed','BILLING_QUEUE_JOIN')
                  AND p.abandoned=0
                  AND date(p.event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        stage_purchase = row[0] if row else 0

        def _dropoff(a: int, b: int) -> float:
            if a == 0:
                return 0.0
            return round((a - b) / a * 100, 1)

        return {
            "store_id": store_id,
            "date": date_filter,
            "funnel": [
                {
                    "stage": "Entry",
                    "visitors": stage_entry,
                    "drop_off_pct": 0.0,
                },
                {
                    "stage": "Zone Visit",
                    "visitors": stage_zone,
                    "drop_off_pct": _dropoff(stage_entry, stage_zone),
                },
                {
                    "stage": "Billing Queue",
                    "visitors": stage_billing,
                    "drop_off_pct": _dropoff(stage_zone, stage_billing),
                },
                {
                    "stage": "Purchase",
                    "visitors": stage_purchase,
                    "drop_off_pct": _dropoff(stage_billing, stage_purchase),
                },
            ],
            "overall_conversion_pct": round(stage_purchase / stage_entry * 100, 1)
            if stage_entry > 0 else 0.0,
        }
    except Exception as e:
        logger.error(f'"funnel error store={store_id} err={str(e)}"')
        raise HTTPException(status_code=500, detail={"error": "COMPUTATION_ERROR", "message": str(e)})
    finally:
        await db.close()
