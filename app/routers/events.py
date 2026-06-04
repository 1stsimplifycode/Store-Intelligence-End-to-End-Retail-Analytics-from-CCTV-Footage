"""
POST /events/ingest  — idempotent batch ingest, up to 500 events per call.
POST /events/pos     — ingest POS transaction batch.
"""
import json
import logging
from fastapi import APIRouter, HTTPException, Request
from app.models import IngestRequest, IngestResponse, POSTransaction
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")

QUEUE_TYPES = {"queue_completed", "queue_abandoned", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"}


async def _upsert_event(db, ev) -> str:
    """Insert one normalised event. Returns 'ok', 'duplicate', or 'error:<msg>'."""
    try:
        is_staff_int = 1 if ev.is_staff else 0
        abandoned_int = 1 if ev.abandoned else 0

        cur = await db.execute(
            """
            INSERT OR IGNORE INTO raw_events (
                event_id, store_id, camera_id, track_id, id_token,
                event_type, event_time, zone_id, zone_name, zone_type,
                is_staff, is_revenue_zone, gender, age, age_bucket,
                group_id, group_size,
                queue_join_ts, queue_served_ts, queue_exit_ts,
                wait_seconds, queue_position, abandoned,
                zone_hotspot_x, zone_hotspot_y, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ev.event_id,
                ev.store_id,
                ev.camera_id,
                ev.track_id,
                ev.id_token,
                ev.event_type,
                ev.event_time,
                ev.zone_id,
                ev.zone_name,
                ev.zone_type,
                is_staff_int,
                ev.is_revenue_zone,
                ev.gender,
                ev.age,
                ev.age_bucket,
                ev.group_id,
                ev.group_size,
                ev.queue_join_ts,
                ev.queue_served_ts,
                ev.queue_exit_ts,
                ev.wait_seconds,
                ev.queue_position_at_join,
                abandoned_int,
                ev.zone_hotspot_x,
                ev.zone_hotspot_y,
                json.dumps(ev.model_dump()),
            ),
        )
        if cur.rowcount == 0:
            return "duplicate"
        return "ok"
    except Exception as e:
        return f"error:{str(e)}"


@router.post("/ingest", response_model=IngestResponse, status_code=202)
async def ingest_events(payload: IngestRequest, request: Request):
    """
    Accepts up to 500 events per call. Idempotent — duplicate event_ids are silently
    ignored and counted. Malformed events are rejected with structured errors.
    """
    accepted = duplicate = rejected = 0
    errors = []

    try:
        db = await get_db()
    except Exception:
        raise HTTPException(status_code=503, detail={
            "error": "DATABASE_UNAVAILABLE",
            "message": "Storage backend is unavailable. Retry shortly."
        })

    try:
        for ev in payload.events:
            if not ev.store_id:
                rejected += 1
                errors.append({"event_id": ev.event_id, "reason": "missing store_id"})
                continue
            if not ev.event_time:
                rejected += 1
                errors.append({"event_id": ev.event_id, "reason": "missing event_time"})
                continue

            result = await _upsert_event(db, ev)
            if result == "ok":
                accepted += 1
            elif result == "duplicate":
                duplicate += 1
            else:
                rejected += 1
                errors.append({"event_id": ev.event_id, "reason": result})

        await db.commit()
    finally:
        await db.close()

    trace_id = getattr(request.state, "trace_id", "")
    logger.info(
        f'{{"trace_id":"{trace_id}","event_count":{len(payload.events)},'
        f'"accepted":{accepted},"duplicate":{duplicate},"rejected":{rejected}}}'
    )
    return IngestResponse(accepted=accepted, rejected=rejected,
                          duplicate=duplicate, errors=errors)


@router.post("/pos/ingest", status_code=202)
async def ingest_pos(transactions: list[POSTransaction]):
    """Ingest POS transaction records for conversion correlation."""
    inserted = 0
    try:
        db = await get_db()
    except Exception:
        raise HTTPException(status_code=503, detail={
            "error": "DATABASE_UNAVAILABLE",
            "message": "Storage backend unavailable."
        })
    try:
        for txn in transactions:
            d = txn.order_date or "2026-01-01"
            t = txn.order_time or "00:00:00"
            # Parse DD-MM-YYYY if needed
            if d and "-" in d:
                parts = d.split("-")
                if len(parts[0]) == 2:  # DD-MM-YYYY
                    d = f"{parts[2]}-{parts[1]}-{parts[0]}"
            order_ts = f"{d}T{t}"
            await db.execute(
                """INSERT OR IGNORE INTO pos_transactions
                   (order_id, store_id, product_id, brand_name, total_amount, order_ts)
                   VALUES (?,?,?,?,?,?)""",
                (txn.order_id, txn.store_id, txn.product_id,
                 txn.brand_name, txn.total_amount, order_ts)
            )
            inserted += 1
        await db.commit()
    finally:
        await db.close()
    return {"inserted": inserted}
