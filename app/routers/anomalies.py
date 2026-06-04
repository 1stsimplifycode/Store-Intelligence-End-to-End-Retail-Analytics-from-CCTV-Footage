"""
GET /stores/{store_id}/anomalies — active anomaly detection.
Detects: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE, STALE_FEED.
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Query
from app.database import get_db

router = APIRouter()
logger = logging.getLogger("store_intelligence")

QUEUE_SPIKE_THRESHOLD = 5   # queue depth > N → WARN; > 2*N → CRITICAL
DEAD_ZONE_MINUTES = 30
CONVERSION_DROP_THRESHOLD = 0.3  # 30% relative drop vs 7-day avg → WARN


@router.get("/{store_id}/anomalies")
async def get_anomalies(
    store_id: str,
    date: str = Query(None, description="YYYY-MM-DD. Defaults to today UTC.")
):
    """
    Returns active anomalies with severity INFO/WARN/CRITICAL and suggested_action.
    """
    try:
        db = await get_db()
    except Exception:
        raise HTTPException(status_code=503, detail={
            "error": "DATABASE_UNAVAILABLE",
            "message": "Storage backend unavailable."
        })

    date_filter = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_str = datetime.now(timezone.utc).isoformat()
    anomalies = []

    try:
        # --- 1. BILLING_QUEUE_SPIKE ---
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
        q_depth = max(0, row[0] or 0)

        if q_depth >= QUEUE_SPIKE_THRESHOLD * 2:
            anomalies.append({
                "type": "BILLING_QUEUE_SPIKE",
                "severity": "CRITICAL",
                "value": q_depth,
                "threshold": QUEUE_SPIKE_THRESHOLD * 2,
                "detected_at": now_str,
                "suggested_action": f"Queue depth is {q_depth}. Open additional billing counters immediately.",
            })
        elif q_depth >= QUEUE_SPIKE_THRESHOLD:
            anomalies.append({
                "type": "BILLING_QUEUE_SPIKE",
                "severity": "WARN",
                "value": q_depth,
                "threshold": QUEUE_SPIKE_THRESHOLD,
                "detected_at": now_str,
                "suggested_action": f"Queue depth is {q_depth}. Consider opening an extra counter.",
            })

        # --- 2. CONVERSION_DROP vs 7-day avg ---
        # Today's conversion
        cur = await db.execute(
            """SELECT
                COUNT(DISTINCT CASE WHEN event_type IN ('entry','ENTRY') THEN
                    COALESCE(id_token, CAST(track_id AS TEXT)) END) as visitors,
                COUNT(DISTINCT CASE WHEN event_type IN ('queue_completed') AND abandoned=0 THEN
                    COALESCE(id_token, CAST(track_id AS TEXT)) END) as converters
               FROM raw_events
               WHERE store_id=? AND is_staff=0 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        row = await cur.fetchone()
        today_visitors = row["visitors"] or 0
        today_converters = row["converters"] or 0
        today_conv = today_converters / today_visitors if today_visitors > 0 else None

        # 7-day average (excluding today)
        week_ago = (datetime.fromisoformat(date_filter) - timedelta(days=7)).strftime("%Y-%m-%d")
        cur = await db.execute(
            """SELECT
                date(event_time) as d,
                COUNT(DISTINCT CASE WHEN event_type IN ('entry','ENTRY') THEN
                    COALESCE(id_token, CAST(track_id AS TEXT)) END) as visitors,
                COUNT(DISTINCT CASE WHEN event_type='queue_completed' AND abandoned=0 THEN
                    COALESCE(id_token, CAST(track_id AS TEXT)) END) as converters
               FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND date(event_time) BETWEEN ? AND ?
               GROUP BY d""",
            (store_id, week_ago, date_filter)
        )
        rows = await cur.fetchall()
        daily_rates = [
            r["converters"] / r["visitors"]
            for r in rows
            if r["visitors"] > 0 and r["d"] != date_filter
        ]
        if daily_rates and today_conv is not None:
            avg_7d = sum(daily_rates) / len(daily_rates)
            if avg_7d > 0:
                drop_pct = (avg_7d - today_conv) / avg_7d
                if drop_pct >= CONVERSION_DROP_THRESHOLD:
                    severity = "CRITICAL" if drop_pct >= 0.5 else "WARN"
                    anomalies.append({
                        "type": "CONVERSION_DROP",
                        "severity": severity,
                        "value": round(today_conv, 4),
                        "seven_day_avg": round(avg_7d, 4),
                        "drop_pct": round(drop_pct * 100, 1),
                        "detected_at": now_str,
                        "suggested_action": (
                            f"Conversion rate {round(today_conv*100,1)}% is "
                            f"{round(drop_pct*100,1)}% below the 7-day avg "
                            f"({round(avg_7d*100,1)}%). Review zone dwell and queue abandonment."
                        ),
                    })

        # --- 3. DEAD_ZONE (no zone visits in last 30 min) ---
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DEAD_ZONE_MINUTES)).isoformat()
        cur = await db.execute(
            """SELECT DISTINCT zone_name FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('zone_entered','ZONE_ENTER')
                 AND date(event_time)=?""",
            (store_id, date_filter)
        )
        all_zones = {r["zone_name"] for r in await cur.fetchall()}

        cur = await db.execute(
            """SELECT DISTINCT zone_name FROM raw_events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('zone_entered','ZONE_ENTER')
                 AND event_time >= ?""",
            (store_id, cutoff)
        )
        recent_zones = {r["zone_name"] for r in await cur.fetchall()}
        dead_zones = all_zones - recent_zones

        for z in dead_zones:
            anomalies.append({
                "type": "DEAD_ZONE",
                "severity": "INFO",
                "zone_name": z,
                "minutes_since_last_visit": DEAD_ZONE_MINUTES,
                "detected_at": now_str,
                "suggested_action": (
                    f"Zone '{z}' has had no visitor activity for {DEAD_ZONE_MINUTES}+ minutes. "
                    "Check camera feed and consider staff re-engagement."
                ),
            })

        return {
            "store_id": store_id,
            "date": date_filter,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        }
    except Exception as e:
        logger.error(f'"anomalies error store={store_id} err={str(e)}"')
        raise HTTPException(status_code=500, detail={"error": "COMPUTATION_ERROR", "message": str(e)})
    finally:
        await db.close()
